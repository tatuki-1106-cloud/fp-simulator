"""税・社会保険エンジンのゴールデンテスト.

国税庁の計算例・モデルケースで検証する。
"""

from __future__ import annotations

import datetime
import pathlib

import pytest

from fp_simulator.parameters.loader import get_store, reset_store
from fp_simulator.engine.income import salary_deduction, salary_income_after_deduction
from fp_simulator.engine.income_tax import (
    Deductions,
    calc_annual_income_tax,
    calc_taxable_income,
    monthly_income_tax_schedule,
)
from fp_simulator.engine.resident_tax import (
    calc_annual_resident_tax,
    monthly_resident_tax_schedule,
)
from fp_simulator.engine.social_insurance import (
    monthly_social_insurance,
    standard_remuneration,
)


@pytest.fixture(scope="module")
def store():
    reset_store()
    root = pathlib.Path(__file__).resolve().parents[1]
    return get_store(root / "parameters")


D2024 = datetime.date(2024, 12, 31)
D2025 = datetime.date(2025, 12, 31)


class TestSalaryDeduction:
    """給与所得控除の検証(国税庁の例)."""

    def test_5_million_2024(self, store) -> None:
        """給与収入500万円(2024年) → 控除144万円."""
        # 500万×20% + 44万 = 144万
        assert salary_deduction(store, D2024, 5_000_000) == 1_440_000

    def test_10_million_2024(self, store) -> None:
        """給与収入1,000万円(2024年) → 控除195万円(上限)."""
        assert salary_deduction(store, D2024, 10_000_000) == 1_950_000

    def test_1_6_million_2024(self, store) -> None:
        """給与収入160万円(2024年) → 控除55万円(最低保障)."""
        assert salary_deduction(store, D2024, 1_600_000) == 550_000

    def test_1_6_million_2025(self, store) -> None:
        """給与収入160万円(2025年・改正後) → 控除65万円."""
        assert salary_deduction(store, D2025, 1_600_000) == 650_000

    def test_income_after_deduction(self, store) -> None:
        """給与所得 = 収入 − 控除."""
        assert salary_income_after_deduction(store, D2024, 5_000_000) == 3_560_000


class TestIncomeTax:
    """所得税の検証(国税庁モデルケース)."""

    def test_annual_tax_single_5m_2024(self, store) -> None:
        """年収500万・独身・社保控除70万の場合(2024年).

        給与所得: 500万 - 144万 = 356万
        課税所得: 356万 - (基礎48万 + 社保70万) = 238万 → 238万(千円切捨)
        所得税: 238万×10% - 97,500 = 140,500
        復興特別: 140,500×2.1% = 2,950
        合計: 143,450
        """
        deductions = Deductions(basic=480000, social_insurance=700000, spouse=0, dependent=0)
        income_after = salary_income_after_deduction(store, D2024, 5_000_000)
        tax = calc_annual_income_tax(store, D2024, income_after, deductions)
        assert tax == 143_450

    def test_taxable_income_floor_1000(self, store) -> None:
        """課税所得は1,000円未満切捨て."""
        deductions = Deductions(basic=480000, social_insurance=0, spouse=0, dependent=0)
        # 給与所得 1,000,500 → 課税所得 520,500 → 520,000
        taxable = calc_taxable_income(store, D2024, 1_000_500, deductions)
        assert taxable == 520_000

    def test_monthly_schedule_sums_to_annual(self, store) -> None:
        """月次スケジュールの合計が年間確定税額と一致する."""
        deductions = Deductions(basic=480000, social_insurance=700000, spouse=0, dependent=0)
        monthly_salary = [350_000] * 12  # 月35万
        # 賞与込みで年500万に調整
        monthly_salary[5] = 350_000 + 400_000  # 6月賞与
        monthly_salary[11] = 350_000 + 400_000  # 12月賞与

        results = monthly_income_tax_schedule(
            store, 2024, monthly_salary, lambda d: deductions
        )
        total_withholding = sum(r.withholding + r.year_end_adjustment for r in results)

        annual_salary = sum(monthly_salary)
        income_after = salary_income_after_deduction(store, D2024, annual_salary)
        expected = calc_annual_income_tax(store, D2024, income_after, deductions)
        assert total_withholding == expected

    def test_year_end_adjustment_in_december(self, store) -> None:
        """年末調整は12月に発生する."""
        deductions = Deductions(basic=480000, social_insurance=700000, spouse=0, dependent=0)
        results = monthly_income_tax_schedule(
            store, 2024, [400_000] * 12, lambda d: deductions
        )
        assert all(r.year_end_adjustment == 0 for r in results[:11])
        assert results[11].year_end_adjustment != 0  # 12月に調整あり


class TestResidentTax:
    """住民税の検証."""

    def test_annual_resident_tax_5m(self, store) -> None:
        """年収500万・独身(2024年所得)の住民税.

        課税標準(所得税ベース): 238万
        住民税課税標準: 238万 + (48万-43万) = 243万
        所得割: 243万×10% = 243,000
        均等割+森林環境税: 4,000+1,000 = 5,000
        合計: 248,000
        """
        deductions = Deductions(basic=480000, social_insurance=700000, spouse=0, dependent=0)
        income_after = salary_income_after_deduction(store, D2024, 5_000_000)
        taxable_it = calc_taxable_income(store, D2024, income_after, deductions)
        tax = calc_annual_resident_tax(store, 2024, taxable_it, deductions)
        assert tax == 248_000

    def test_collection_period(self, store) -> None:
        """徴収は翌年6月〜翌々年5月の12ヶ月."""
        deductions = Deductions(basic=480000, social_insurance=700000, spouse=0, dependent=0)
        schedule = monthly_resident_tax_schedule(store, 2024, 2_380_000, deductions)
        assert len(schedule) == 12
        assert datetime.date(2025, 6, 1) in schedule
        assert datetime.date(2026, 5, 1) in schedule
        assert datetime.date(2026, 6, 1) not in schedule
        assert sum(schedule.values()) == calc_annual_resident_tax(
            store, 2024, 2_380_000, deductions
        )


class TestSocialInsurance:
    """社会保険料の検証."""

    def test_standard_remuneration(self) -> None:
        """標準報酬月額の等級."""
        assert standard_remuneration(300_000) == 300_000
        assert standard_remuneration(309_999) == 300_000
        assert standard_remuneration(310_000) == 320_000
        assert standard_remuneration(0) == 0

    def test_monthly_premium_300k_tokyo(self, store) -> None:
        """月給30万・東京・30歳(介護保険なし)の2025年社保料.

        標準報酬30万
        厚生年金: 300,000×18.3%×0.5 = 27,450
        健康保険(東京2025): 300,000×9.91%×0.5 = 14,865
        介護保険: 0 (30歳)
        雇用保険: 300,000×0.55% = 1,650
        合計: 43,965
        """
        p = monthly_social_insurance(
            store, datetime.date(2025, 4, 1), 300_000, "東京都", 30
        )
        assert p.pension == 27_450
        assert p.health == 14_865
        assert p.nursing == 0
        assert p.employment == 1_650
        assert p.total == 43_965

    def test_nursing_insurance_age40(self, store) -> None:
        """40歳は介護保険がかかる(東京・2025年).

        標準報酬30万、介護保険1.59%×0.5 = 2,385
        """
        p = monthly_social_insurance(
            store, datetime.date(2025, 4, 1), 300_000, "東京都", 40
        )
        assert p.nursing == 2_385

    def test_prefecture_difference(self, store) -> None:
        """都道府県で健康保険料が異なる."""
        tokyo = monthly_social_insurance(
            store, datetime.date(2025, 4, 1), 300_000, "東京都", 30
        )
        osaka = monthly_social_insurance(
            store, datetime.date(2025, 4, 1), 300_000, "大阪府", 30
        )
        assert tokyo.health != osaka.health
