"""拡張機能(教育費・iDeCo/NISA・保険)のテスト."""

from __future__ import annotations

import datetime
import pathlib

import pytest

from fp_simulator.parameters.loader import get_store, reset_store
from fp_simulator.engine.education import monthly_education_costs
from fp_simulator.engine.investment import (
    IdecoAccount,
    NisaAccount,
    ideco_annual_deduction,
    ideco_contribution_limit,
    ideco_monthly_step,
    nisa_monthly_step,
)
from fp_simulator.engine.insurance import InsurancePolicy, monthly_premium_in_period, surrender_value
from fp_simulator.engine.childcare_leave import (
    childcare_benefit,
    is_social_insurance_exempt,
    maternity_allowance,
)


@pytest.fixture(scope="module")
def store():
    reset_store()
    root = pathlib.Path(__file__).resolve().parents[1]
    return get_store(root / "parameters")


D2025 = datetime.date(2025, 4, 1)


class TestEducation:
    """教育費."""

    def test_elementary_public(self, store) -> None:
        """小学校(公立)の月額."""
        monthly, schools = monthly_education_costs(store, D2025, 7, "公立")
        assert monthly == 321000 // 12
        assert "小学校.公立" in schools

    def test_high_school_private(self, store) -> None:
        """高校(私立)の月額."""
        monthly, schools = monthly_education_costs(store, D2025, 16, "私立")
        assert monthly == 969000 // 12
        assert "高校.私立" in schools

    def test_no_school_age(self, store) -> None:
        """2歳(幼稚園前)は0."""
        monthly, schools = monthly_education_costs(store, D2025, 2, "公立")
        assert monthly == 0


class TestIdeco:
    """iDeCo."""

    def test_contribution_limit_type2(self, store) -> None:
        """会社員(企業年金なし)の上限は23,000円."""
        assert ideco_contribution_limit(store, D2025, 2) == 23000

    def test_annual_deduction(self, store) -> None:
        """年間所得控除額."""
        assert ideco_annual_deduction(store, D2025, 20000, 2) == 240000
        # 上限超過は上限まで
        assert ideco_annual_deduction(store, D2025, 30000, 2) == 23000 * 12

    def test_monthly_step_with_return(self, store) -> None:
        """運用益ありの1ヶ月."""
        acc = IdecoAccount(balance=1_000_000, total_contributions=100_000)
        new = ideco_monthly_step(store, D2025, acc, 20000, 2, annual_return_rate=0.03)
        # 1,000,000 × (1+0.0025) + 20,000 = 1,022,500
        assert new.balance == 1_022_500
        assert new.total_contributions == 120_000


class TestNisa:
    """NISA."""

    def test_monthly_step(self, store) -> None:
        """月額投資と運用."""
        acc = NisaAccount(balance=500_000, total_invested=500_000)
        new = nisa_monthly_step(store, D2025, acc, 50000, annual_return_rate=0.05)
        assert new.balance > 500_000
        assert new.total_invested == 550_000


class TestInsurance:
    """保険."""

    def test_monthly_premium_in_period(self) -> None:
        """期間内は保険料が発生."""
        policy = InsurancePolicy(
            name="終身保険", insured_member_id="m1", payer_member_id="m2",
            monthly_premium=10000,
            start_date=datetime.date(2020, 1, 1),
            end_date=datetime.date(2060, 12, 31),
            death_benefit=10_000_000,
        )
        assert monthly_premium_in_period(policy, datetime.date(2025, 6, 1)) == 10000
        assert monthly_premium_in_period(policy, datetime.date(2019, 12, 1)) == 0

    def test_surrender_value(self) -> None:
        """解約返戻金."""
        policy = InsurancePolicy(
            name="終身保険", insured_member_id="m1", payer_member_id="m2",
            monthly_premium=10000,
            start_date=datetime.date(2020, 1, 1),
            end_date=datetime.date(2060, 12, 31),
            surrender_value_rate=0.8,
        )
        # 5年後(60ヶ月): 10,000×60×0.8 = 480,000
        assert surrender_value(policy, datetime.date(2025, 1, 1)) == 480_000


class TestChildcareLeave:
    """産休・育休."""

    def test_maternity_allowance(self, store) -> None:
        """出産手当金(標準報酬30万)."""
        assert maternity_allowance(store, D2025, 300000) == 200010  # 300000×2/3

    def test_childcare_benefit_first_180(self, store) -> None:
        """育児休業給付金(180日以内67%)."""
        assert childcare_benefit(store, D2025, 300000, 100) == 201000

    def test_childcare_benefit_after_180(self, store) -> None:
        """育児休業給付金(181日以降50%)."""
        assert childcare_benefit(store, D2025, 300000, 200) == 150000

    def test_social_insurance_exempt(self, store) -> None:
        """産休・育休中は社保免除."""
        assert is_social_insurance_exempt(store, D2025) is True
