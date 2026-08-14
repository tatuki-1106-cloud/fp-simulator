"""月次キャッシュフローの統合ゴールデンテスト.

現実的な世帯モデルで生涯CFを計算し、妥当性を検証する。
"""

from __future__ import annotations

import datetime
import pathlib

from pydantic import ValidationError
import pytest

from fp_simulator.parameters.loader import get_store, reset_store
from fp_simulator.engine.models import (
    Account,
    Expense,
    Household,
    Income,
    IdecoPlan,
    Member,
    NisaPlan,
    OwnedHousingPlan,
    PensionRecordInput,
    PlanAssumptions,
    Relationship,
    SocialInsuranceType,
    Vehicle,
    Loan,
)
from fp_simulator.engine.cashflow import DisasterScenario, simulate


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

    def test_disaster_scenario_stops_deceased_member_income(
        self, store, household: Household
    ) -> None:
        """指定年齢の死亡後は対象者の給与と年金を計上しない."""
        result = simulate(store, household, DisasterScenario("husband", 40))
        before = next(m for m in result.monthly if m.date == datetime.date(2036, 3, 1))
        death_month = next(m for m in result.monthly if m.date == datetime.date(2036, 4, 1))
        assert before.salary_income == 300_000
        assert death_month.salary_income == 0
        assert death_month.pension_income == 0

    def test_disaster_supports_survivor_benefits_and_expense_reduction(
        self, store, household: Household
    ) -> None:
        """万が一後の追加収入と生活費調整を死亡月から反映する."""
        result = simulate(
            store,
            household,
            DisasterScenario(
                "husband",
                40,
                survivor_pension_monthly=50_000,
                child_allowance_monthly=15_000,
                living_expense_reduction_rate=0.1,
            ),
        )
        death_month = next(m for m in result.monthly if m.date == datetime.date(2036, 4, 1))
        after = next(m for m in result.monthly if m.date == datetime.date(2036, 5, 1))
        assert death_month.survivor_pension == 50_000
        assert death_month.child_allowance == 15_000
        assert death_month.living_expense == 180_000
        assert after.survivor_pension == 50_000
        assert after.child_allowance == 15_000
        assert after.living_expense == 180_000
        before_allowance_end = next(
            m for m in result.monthly if m.date == datetime.date(2043, 12, 1)
        )
        allowance_end = next(
            m for m in result.monthly if m.date == datetime.date(2044, 1, 1)
        )
        assert before_allowance_end.child_allowance == 15_000
        assert allowance_end.child_allowance == 0

    def test_investment_balances_are_tracked_separately(
        self, store, household: Household
    ) -> None:
        """iDeCo/NISAの掛金を現金残高と別の運用残高として追跡する."""
        household.ideco_plans.append(
            IdecoPlan(
                id="ideco",
                member_id="husband",
                initial_balance=1_000_000,
                monthly_contribution=10_000,
                annual_return_rate=0.03,
            )
        )
        household.nisa_plans.append(
            NisaPlan(
                id="nisa",
                member_id="husband",
                initial_balance=500_000,
                monthly_investment=10_000,
                annual_return_rate=0.03,
            )
        )
        result = simulate(store, household)
        first = result.monthly[0]
        assert first.ideco_balance > 1_000_000
        assert first.nisa_balance > 500_000
        assert first.total_assets == first.balance + first.ideco_balance + first.nisa_balance

    def test_investment_withdrawals_reduce_accounts_and_add_net_cashflow(
        self, store, household: Household
    ) -> None:
        """明示した受取月額を開始年齢から反映し、iDeCo税を控除する."""
        household.ideco_plans.append(
            IdecoPlan(
                id="ideco-withdrawal",
                member_id="husband",
                initial_balance=100_000,
                monthly_contribution=0,
                receive_start_age=30,
                monthly_withdrawal=10_000,
                withdrawal_tax_rate=0.1,
            )
        )
        household.nisa_plans.append(
            NisaPlan(
                id="nisa-withdrawal",
                member_id="husband",
                initial_balance=100_000,
                monthly_investment=0,
                receive_start_age=30,
                monthly_withdrawal=5_000,
            )
        )
        result = simulate(store, household)
        before = next(m for m in result.monthly if m.date == datetime.date(2026, 3, 1))
        receive_month = next(m for m in result.monthly if m.date == datetime.date(2026, 4, 1))
        assert before.ideco_withdrawal == 0
        assert receive_month.ideco_withdrawal == 10_000
        assert receive_month.ideco_withdrawal_tax == 1_000
        assert receive_month.nisa_withdrawal == 5_000
        assert receive_month.ideco_balance == 90_000
        assert receive_month.nisa_balance == 95_000
        assert receive_month.net == (
            receive_month.total_income
            - receive_month.total_expense
            - receive_month.social_insurance
            - receive_month.income_tax
            - receive_month.resident_tax
            - receive_month.ideco_withdrawal_tax
        )

    def test_investment_plan_validation_rejects_invalid_input(
        self, store, household: Household
    ) -> None:
        """iDeCo/NISAのモデルが不正な値を拒否する."""
        with pytest.raises(Exception):  # Pydantic ValidationError
            IdecoPlan(
                id="bad-ideco",
                member_id="husband",
                initial_balance=-1,
                monthly_contribution=0,
            )
        with pytest.raises(Exception):  # Pydantic ValidationError
            NisaPlan(
                id="bad-nisa",
                member_id="husband",
                initial_balance=0,
                monthly_investment=-1000,
            )

    def test_owned_housing_costs_are_recorded_from_purchase_month(
        self, store, household: Household
    ) -> None:
        """所有住宅の頭金・固定資産税・修繕費を購入月から計上する."""
        household.owned_housing = OwnedHousingPlan(
            property_price=40_000_000,
            down_payment=5_000_000,
            purchase_year=2026,
            purchase_month=4,
            annual_property_tax=120_000,
            annual_repair_cost=60_000,
        )
        result = simulate(store, household)
        purchase = next(m for m in result.monthly if m.date == datetime.date(2026, 4, 1))
        next_year = next(m for m in result.monthly if m.date == datetime.date(2027, 4, 1))
        other_month = next(m for m in result.monthly if m.date == datetime.date(2026, 5, 1))

        assert purchase.housing_down_payment == 5_000_000
        assert purchase.property_tax == 120_000
        assert purchase.repair_expense == 60_000
        assert next_year.property_tax == 120_000
        assert next_year.repair_expense == 60_000
        assert other_month.housing_down_payment == 0
        assert other_month.property_tax == 0
        assert other_month.repair_expense == 0
        assert purchase.total_expense >= 5_180_000

    def test_owned_housing_rejects_down_payment_above_price(
        self, store, household: Household
    ) -> None:
        """頭金が物件価格を超える所有住宅設定を拒否する."""
        with pytest.raises(ValidationError):
            OwnedHousingPlan(
                property_price=10_000_000,
                down_payment=10_000_001,
                purchase_year=2026,
            )

    def test_vehicle_purchase_replacement_maintenance_and_sale(
        self, store, household: Household
    ) -> None:
        """乗り物の取得・買替・維持費・車検・売却を計上する."""
        household.loans.append(
            Loan(
                id="car-loan",
                member_id="husband",
                principal=500_000,
                annual_rate=0.01,
                years=1,
                start_year=2026,
                start_month=1,
            )
        )
        household.vehicles.append(
            Vehicle(
                id="car",
                name="ファミリーカー",
                vehicle_type="中古車",
                ownership_start_year=2026,
                ownership_start_month=1,
                ownership_end_year=2030,
                ownership_end_month=12,
                purchase_price=2_000_000,
                monthly_maintenance=20_000,
                annual_tax_repair=120_000,
                replacement_cycle_years=3,
                sale_price=500_000,
                inspection_cost=100_000,
                inspection_cycle_years=2,
                loan_id="car-loan",
            )
        )
        result = simulate(store, household)
        initial = next(m for m in result.monthly if m.date == datetime.date(2026, 1, 1))
        replacement = next(m for m in result.monthly if m.date == datetime.date(2029, 1, 1))
        ending = next(m for m in result.monthly if m.date == datetime.date(2030, 12, 1))

        assert initial.vehicle_purchase_expense == 1_500_000
        assert initial.vehicle_maintenance == 20_000
        assert initial.vehicle_tax_repair == 120_000
        assert initial.vehicle_inspection_expense == 100_000
        assert initial.vehicle_sale_income == 0
        assert replacement.vehicle_purchase_expense == 2_000_000
        assert replacement.vehicle_sale_income == 500_000
        assert replacement.vehicle_inspection_expense == 100_000
        assert ending.vehicle_sale_income == 500_000

    def test_vehicle_rejects_invalid_ownership_period(
        self, store, household: Household
    ) -> None:
        """所有終了年月が開始年月より前の乗り物設定を拒否する."""
        with pytest.raises(ValueError):
            Vehicle(
                id="invalid-car",
                purchase_price=1_000_000,
                ownership_start_year=2030,
                ownership_end_year=2029,
            )

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
