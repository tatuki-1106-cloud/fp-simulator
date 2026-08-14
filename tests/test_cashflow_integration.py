"""月次キャッシュフローの統合ゴールデンテスト.

現実的な世帯モデルで生涯CFを計算し、妥当性を検証する。
"""

from __future__ import annotations

import datetime
import pathlib

import pytest

from fp_simulator.parameters.loader import get_store, reset_store
from fp_simulator.engine.models import (
    Account,
    Expense,
    Household,
    Income,
    Member,
    PensionRecordInput,
    PlanAssumptions,
    Relationship,
    SocialInsuranceType,
)
from fp_simulator.engine.cashflow import simulate


@pytest.fixture(scope="module")
def store():
    reset_store()
    root = pathlib.Path(__file__).resolve().parents[1]
    return get_store(root / "parameters")


@pytest.fixture()
def household() -> Household:
    """テスト世帯: 30歳会社員(月収30万、賞与年2回各50万)、妻30歳(専業主婦)、子0歳.

    - 生活費: 月20万
    - 預金: 300万(金利0%)
    - 年金: 夫は国民年金480月+厚生年金456月(平均標準報酬30万)
    """
    return Household(
        id="test1",
        name="テスト世帯",
        members=[
            Member(
                id="husband",
                name="たろう",
                relationship=Relationship.HOUSEHOLDER,
                birth_date=datetime.date(1996, 4, 1),
                gender="男",
                life_expectancy_age=90,
            ),
            Member(
                id="wife",
                name="はなこ",
                relationship=Relationship.SPOUSE,
                birth_date=datetime.date(1996, 7, 1),
                gender="女",
                life_expectancy_age=95,
            ),
            Member(
                id="child1",
                name="いちろう",
                relationship=Relationship.CHILD,
                birth_date=datetime.date(2026, 1, 1),
                gender="男",
            ),
        ],
        incomes=[
            Income(
                id="husband_salary",
                member_id="husband",
                name="会社員",
                social_insurance_type=SocialInsuranceType.KYOSAI_KOSEI,
                start_age=29,
                start_month=1,
                end_age=60,
                end_month=3,
                monthly_amount=300_000,
                bonus_months=[6, 12],
                bonus_amount=500_000,
                annual_raise_rate=0.0,
                retirement_allowance=20_000_000,
                retirement_age=60,
            )
        ],
        pension_records=[
            PensionRecordInput(
                member_id="husband",
                kokumin_months=480,
                kousei_months=456,
                avg_standard_remuneration=300_000,
                kousei_months_before_2003_04=0,
                kousei_months_after_2003_04=456,
                start_age=65,
            )
        ],
        expenses=[
            Expense(
                id="living",
                name="生活費",
                monthly_amount=200_000,
                cycle="monthly",
                start_age=0,
                end_age=None,
            )
        ],
        accounts=[
            Account(id="bank", name="普通預金", account_type="預金", balance=3_000_000, interest_rate=0.0)
        ],
        assumptions=PlanAssumptions(base_year=2026, base_month=1, inflation_rate=0.0),
    )


class TestCashflowIntegration:
    """統合テスト."""

    def test_simulation_runs_and_produces_monthly_data(
        self, store, household: Household
    ) -> None:
        """シミュレーションが実行でき、月次データが生成される."""
        result = simulate(store, household)
        assert len(result.monthly) > 0
        # 30歳〜90歳 = 61年 × 12ヶ月 = 732ヶ月弱(誕生月による)
        assert len(result.monthly) > 700

    def test_first_month_values(self, store, household: Household) -> None:
        """2026年1月(30歳)の値が妥当."""
        result = simulate(store, household)
        m1 = result.monthly[0]
        assert m1.date == datetime.date(2026, 1, 1)
        assert m1.age == 29 or m1.age == 30  # 誕生月による
        # 給与30万
        assert m1.salary_income == 300_000
        # 社保(東京・30歳): 厚生年金27,450 + 健保14,865 + 雇用保険1,650 = 43,965
        assert m1.social_insurance == 43_965
        # 生活費20万
        assert m1.living_expense == 200_000
        # 残高 = 300万 + 収支
        assert m1.balance == 3_000_000 + m1.net

    def test_bonus_months(self, store, household: Household) -> None:
        """6月と12月は賞与が入る."""
        result = simulate(store, household)
        june = next(m for m in result.monthly if m.date == datetime.date(2026, 6, 1))
        dec = next(m for m in result.monthly if m.date == datetime.date(2026, 12, 1))
        assert june.salary_income == 300_000 + 500_000
        assert dec.salary_income == 300_000 + 500_000

    def test_pension_starts_at_65(self, store, household: Household) -> None:
        """年金は65歳(2061年4月)から支給される."""
        result = simulate(store, household)
        # 65歳になる年(2061年)の4月以降
        pension_month = next(
            (m for m in result.monthly if m.date == datetime.date(2061, 4, 1)), None
        )
        assert pension_month is not None
        # 老齢基礎年金(480月) + 厚生年金報酬比例(456月, 平均30万)
        # 831,700 + (300,000×0.005481×456) = 831,700 + 749,887 = 1,581,587/年
        # 月額 ≈ 131,798
        assert pension_month.pension_income > 120_000

    def test_retirement_at_60(self, store, household: Household) -> None:
        """60歳(2056年4月以降)に退職金が入る."""
        result = simulate(store, household)
        # 1996年4月生まれが60歳になるのは2056年4月。end_month=3(3月末退職)
        # 退職金は退職月(3月)に計上されるが、60歳になるのは4月なので4月に計上
        retirement_month = next(
            (m for m in result.monthly if m.date == datetime.date(2056, 4, 1)), None
        )
        assert retirement_month is not None
        # 退職金2,000万・勤続30年 → 手取りは約1,914万(税金差引後)
        assert retirement_month.retirement_income > 19_000_000

    def test_no_balance_depletion(self, store, household: Household) -> None:
        """このモデルでは生涯で残高が枯渇しない."""
        result = simulate(store, household)
        min_balance = min(m.balance for m in result.monthly)
        assert min_balance > 0

    def test_traces_exist(self, store, household: Household) -> None:
        """トレーサビリティ情報が付与されている."""
        result = simulate(store, household)
        jan = result.monthly[0]
        assert len(jan.traces) > 0
        items = [t.item for t in jan.traces]
        assert "社会保険料" in items

    def test_parameter_snapshot_recorded(self, store, household: Household) -> None:
        """計算時のパラメータスナップショットが記録されている(再現性)."""
        result = simulate(store, household)
        assert "所得税.基礎控除.控除額" in result.parameter_snapshot
        assert "source" in result.parameter_snapshot["所得税.基礎控除.控除額"]
