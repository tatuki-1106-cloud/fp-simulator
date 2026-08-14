"""年金・退職金エンジンのゴールデンテスト.

日本年金機構のモデルケース・国税庁の退職金計算例で検証する。
"""

from __future__ import annotations

import datetime
import pathlib

import pytest

from fp_simulator.parameters.loader import get_store, reset_store
from fp_simulator.engine.pension import (
    PensionRecord,
    additional_pension_spouse,
    apply_early_deferral,
    basic_pension_amount,
    employee_pension_fixed_amount,
    employee_pension_report_proportional,
    total_pension,
    transitional_addition,
)
from fp_simulator.engine.retirement import (
    net_retirement_allowance,
    retirement_income,
    retirement_income_deduction,
    retirement_tax,
)


@pytest.fixture(scope="module")
def store():
    reset_store()
    root = pathlib.Path(__file__).resolve().parents[1]
    return get_store(root / "parameters")


D2025 = datetime.date(2025, 4, 1)


class TestBasicPension:
    """老齢基礎年金."""

    def test_full_40_years_2025(self, store) -> None:
        """40年(480月)加入で満額 831,700円(2025年度)."""
        assert basic_pension_amount(store, D2025, 480) == 831_700

    def test_30_years_2025(self, store) -> None:
        """30年(360月)加入 → 満額の3/4."""
        assert basic_pension_amount(store, D2025, 360) == 831_700 * 360 // 480

    def test_over_40_years_capped(self, store) -> None:
        """40年超加入でも満額で打ち止め."""
        assert basic_pension_amount(store, D2025, 500) == 831_700


class TestEmployeePension:
    """老齢厚生年金."""

    def test_report_proportional(self, store) -> None:
        """報酬比例部分: 平均標準報酬30万×乗率0.005481×360月.

        300,000 × 0.005481 × 360 = 591,948
        """
        amount = employee_pension_report_proportional(store, D2025, 300_000, 360)
        assert amount == 591_948

    def test_fixed_amount(self, store) -> None:
        """定額部分: 単価1,701円×加入月数."""
        assert employee_pension_fixed_amount(store, D2025, 240) == 1701 * 240

    def test_transitional_addition(self, store) -> None:
        """経過的加算: 定額部分 > 老齢基礎年金(厚生期間分)の場合に差額."""
        # 定額部分 240月 = 408,240円
        fixed = employee_pension_fixed_amount(store, D2025, 240)
        # 基礎年金(240月分) = 831,700 × 240/480 = 415,850円
        # この場合は基礎年金のほうが多いので経過的加算は0
        assert transitional_addition(store, D2025, fixed, 240) == 0


class TestEarlyDeferral:
    """繰上げ・繰下げ."""

    def test_early_12_months(self, store) -> None:
        """1年繰上げ → 4.8%減."""
        assert apply_early_deferral(store, D2025, 1_000_000, months_early=12) == 952_000

    def test_deferred_12_months(self, store) -> None:
        """1年繰下げ → 8.4%増."""
        assert apply_early_deferral(store, D2025, 1_000_000, months_deferred=12) == 1_084_000

    def test_deferred_5_years(self, store) -> None:
        """5年繰下げ → 42%増."""
        assert apply_early_deferral(store, D2025, 1_000_000, months_deferred=60) == 1_420_000


class TestAdditionalPension:
    """加給年金."""

    def test_spouse_with_20_years(self, store) -> None:
        """厚生年金20年以上加入で配偶者加給年金."""
        assert additional_pension_spouse(store, D2025, 240) == 408_100

    def test_spouse_under_20_years(self, store) -> None:
        """厚生年金20年未満では加給年金なし."""
        assert additional_pension_spouse(store, D2025, 239) == 0


class TestTotalPension:
    """合計年金額."""

    def test_standard_case(self, store) -> None:
        """標準的な会社員ケース: 国民年金480月、厚生年金360月(平均標準報酬30万)."""
        record = PensionRecord(
            kokumin_months=480,
            kousei_months=360,
            avg_standard_remuneration=300_000,
            kousei_months_before_2003_04=0,
            kousei_months_after_2003_04=360,
        )
        total = total_pension(store, D2025, record)
        # 基礎年金 831,700 + 報酬比例 591,948 = 1,423,648
        assert total == 831_700 + 591_948


class TestRetirementDeduction:
    """退職所得控除."""

    def test_10_years(self) -> None:
        """勤続10年 → 40万×10 = 400万."""
        assert retirement_income_deduction(10) == 4_000_000

    def test_2_years_minimum(self) -> None:
        """勤続2年 → 最低80万."""
        assert retirement_income_deduction(2) == 800_000

    def test_30_years(self) -> None:
        """勤続30年 → 800万 + 70万×10 = 1,500万."""
        assert retirement_income_deduction(30) == 15_000_000


class TestRetirementTax:
    """退職金の税額(国税庁の計算例)."""

    def test_20m_25_years(self, store) -> None:
        """退職金2,000万円・勤続25年の例(国税庁モデル).

        退職所得控除: 800万 + 70万×5 = 1,150万
        退職所得: (2,000万 - 1,150万) × 1/2 = 425万 → 425万(千円切捨)
        所得税: 425万×20% - 427,500 = 422,500
        復興特別: 422,500×2.1% = 8,872
        住民税: 425万×10% = 425,000
        手取り: 2,000万 - (422,500+8,872+425,000) = 19,143,628
        """
        taxable = retirement_income(store, D2025, 20_000_000, 25)
        assert taxable == 4_250_000

        tax = retirement_tax(store, D2025, 20_000_000, 25)
        assert tax.income_tax == 422_500 + 8_872
        assert tax.resident_tax == 425_000

        net = net_retirement_allowance(store, D2025, 20_000_000, 25)
        assert net == 20_000_000 - (422_500 + 8_872 + 425_000)
