"""月次キャッシュフローの統合ゴールデンテスト.

現実的な世帯モデルで生涯CFを計算し、妥当性を検証する。
"""

from __future__ import annotations

import datetime
import pathlib

import pytest
from pydantic import ValidationError

from fp_simulator.engine.cashflow import DisasterScenario, simulate
from fp_simulator.engine.models import (
    Account,
    Expense,
    Household,
    IdecoPlan,
    Income,
    Insurance,
    Loan,
    Member,
    NisaPlan,
    OwnedHousingPlan,
    PensionRecordInput,
    PlanAssumptions,
    Relationship,
    SocialInsuranceType,
    Vehicle,
)
from fp_simulator.parameters.loader import get_store, reset_store


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
        # 2024年10月改正後の児童手当(0歳・第1子)
        assert m1.child_allowance == 15_000
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
        """明示した受取月額を開始年齢から反映する(旧データは年金受取へ移行)."""
        household.ideco_plans.append(
            IdecoPlan(
                id="ideco-withdrawal",
                member_id="husband",
                initial_balance=100_000,
                monthly_contribution=0,
                receive_start_age=30,
                monthly_withdrawal=10_000,
                withdrawal_tax_rate=0.1,  # 旧フィールド(無視される)
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
        # 旧既定値「一時金」+受取月額>0 は年金受取として移行される
        assert household.ideco_plans[0].receive_type == "年金"
        result = simulate(store, household)
        before = next(m for m in result.monthly if m.date == datetime.date(2026, 3, 1))
        receive_month = next(m for m in result.monthly if m.date == datetime.date(2026, 4, 1))
        assert before.ideco_withdrawal == 0
        assert receive_month.ideco_withdrawal == 10_000
        # 年金受取は雑所得として総合課税(年12万円は公的年金等控除内で税額0)
        assert receive_month.ideco_withdrawal_tax == 0
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

    def test_public_pension_is_taxed_as_misc_income(
        self, store, household: Household
    ) -> None:
        """老齢年金は公的年金等控除を適用した雑所得として課税対象になる."""
        # 課税が発生する水準へ年金額を引き上げる(平均標準報酬80万円)
        household.pension_records[0].avg_standard_remuneration = 800_000
        result = simulate(store, household)
        # 夫は65歳(2061年4月)から受給。給与は60歳で終了済み
        month = next(m for m in result.monthly if m.date == datetime.date(2062, 3, 1))
        assert month.pension_income > 0
        trace = next(t for t in month.traces if t.item == "所得税(源泉徴収)")
        misc = trace.basis["公的年金等雑所得"]
        annual_pension = month.pension_income * 12
        assert misc > 0
        # 65歳以上・年金収入330万円以下 → 控除110万円
        assert misc == annual_pension - 1_100_000
        # 雑所得への所得税が月次で源泉徴収される
        assert month.income_tax > 0
        # 前年(2061年)の年金雑所得に対する住民税が2062年6月以降に発生する
        july = next(m for m in result.monthly if m.date == datetime.date(2062, 7, 1))
        assert july.resident_tax > 0

    def test_ideco_contribution_stops_after_lump_sum_receipt(
        self, store, household: Household
    ) -> None:
        """一時金受取後は脱退扱いとなり、end_ageが後でも拠出を停止する."""
        household.ideco_plans.append(
            IdecoPlan(
                id="ideco-stop",
                member_id="husband",
                initial_balance=1_000_000,
                monthly_contribution=10_000,
                start_age=0,
                end_age=60,  # 受取開始(30歳)より後まで拠出可能な設定
                receive_start_age=30,
                receive_type="一時金",
            )
        )
        result = simulate(store, household)
        before = next(m for m in result.monthly if m.date == datetime.date(2026, 3, 1))
        receive_month = next(
            m for m in result.monthly if m.date == datetime.date(2026, 4, 1)
        )
        after = next(m for m in result.monthly if m.date == datetime.date(2026, 5, 1))
        assert before.ideco_contribution == 10_000
        # 受取開始月から拠出停止し、残高が全額一時金で支払われる
        assert receive_month.ideco_contribution == 0
        assert receive_month.ideco_withdrawal > 0
        assert after.ideco_contribution == 0
        assert after.ideco_balance == 0

    def test_pension_only_zero_tax_month_has_no_income_tax_trace(
        self, store, household: Household
    ) -> None:
        """給与がなく税額0の年金受取月は所得税トレースを出さない."""
        household.incomes = []
        household.ideco_plans.append(
            IdecoPlan(
                id="ideco-small-annuity",
                member_id="husband",
                initial_balance=1_000_000,
                monthly_contribution=0,
                receive_start_age=30,
                receive_type="年金",
                monthly_withdrawal=50_000,  # 年60万円: 公的年金等控除(60万)内
            )
        )
        result = simulate(store, household)
        receive_month = next(
            m for m in result.monthly if m.date == datetime.date(2026, 4, 1)
        )
        assert receive_month.ideco_withdrawal == 50_000
        assert receive_month.income_tax == 0
        assert not any(
            t.item.startswith("所得税") for t in receive_month.traces
        )

    def test_ideco_annuity_stops_after_annuity_years(
        self, store, household: Household
    ) -> None:
        """受取期間を指定した年金受取は期間経過後に停止し、残高が残る."""
        household.ideco_plans.append(
            IdecoPlan(
                id="ideco-limited-annuity",
                member_id="husband",
                initial_balance=10_000_000,
                monthly_contribution=0,
                receive_start_age=30,
                receive_type="年金",
                monthly_withdrawal=10_000,
                annuity_years=2,
            )
        )
        result = simulate(store, household)
        # 受取開始: 2026年4月(30歳)。2年後の2028年4月(32歳)以降は停止
        receiving = next(m for m in result.monthly if m.date == datetime.date(2027, 6, 1))
        stopped = next(m for m in result.monthly if m.date == datetime.date(2028, 4, 1))
        assert receiving.ideco_withdrawal == 10_000
        assert stopped.ideco_withdrawal == 0
        assert stopped.ideco_balance > 0

    def test_ideco_lump_sum_dedup_reduces_deduction_within_19_years(
        self, store, household: Household
    ) -> None:
        """退職金の後19年以内のiDeCo一時金は重複年数分の控除を減らす(19年ルール簡易)."""
        # 退職金: 60歳・勤続31年(29〜60)。iDeCo一時金: 65歳受取(差5年)
        household.ideco_plans.append(
            IdecoPlan(
                id="ideco-dedup",
                member_id="husband",
                initial_balance=10_000_000,
                monthly_contribution=0,
                prior_contribution_years=10,
                start_age=0,
                end_age=60,
                receive_start_age=65,
                receive_type="一時金",
            )
        )
        result = simulate(store, household)
        receive_month = next(
            m for m in result.monthly if m.date == datetime.date(2061, 4, 1)
        )
        trace = next(t for t in receive_month.traces if t.item == "iDeCo一時金受取")
        # iDeCo加入期間(近似): 60歳終端で10年 → 50〜60歳。勤続29〜60歳と10年重複
        assert trace.basis["重複調整(19年ルール簡易)"] == 10
        assert trace.basis["調整後控除年数"] == 0
        # 調整後0年 → 控除は最低保障80万円のみ(簡易モデル)
        assert trace.basis["退職所得控除"] == 800_000

    def test_retirement_allowance_dedup_within_5_years(
        self, store, household: Household
    ) -> None:
        """iDeCo一時金の後4年以内の退職金は重複年数分の控除を減らす(5年ルール簡易)."""
        from fp_simulator.engine.retirement import net_retirement_allowance

        # iDeCo一時金: 58歳受取(退職60歳との差2年)。加入20年(38〜58歳の近似)
        household.ideco_plans.append(
            IdecoPlan(
                id="ideco-first",
                member_id="husband",
                initial_balance=5_000_000,
                monthly_contribution=0,
                prior_contribution_years=20,
                start_age=0,
                end_age=60,
                receive_start_age=58,
                receive_type="一時金",
            )
        )
        result = simulate(store, household)
        retire_month = next(
            m for m in result.monthly if m.date == datetime.date(2056, 4, 1)
        )
        trace = next(t for t in retire_month.traces if t.item == "退職金(手取り)")
        # 勤続29〜60歳(31年)とiDeCo加入38〜58歳が20年重複 → 調整後11年
        assert trace.basis["重複調整(5年ルール簡易)"] == 20
        assert trace.basis["調整後控除年数"] == 11
        expected_net = net_retirement_allowance(
            store, datetime.date(2056, 4, 1), 20_000_000, 11
        )
        assert retire_month.retirement_income == expected_net

    def test_no_dedup_when_receipts_are_far_apart(
        self, store, household: Household
    ) -> None:
        """退職金とiDeCo一時金が19年超離れていれば控除調整しない."""
        # iDeCo一時金: 30歳受取(退職60歳より30年前) → 両ルールとも対象外
        household.ideco_plans.append(
            IdecoPlan(
                id="ideco-early",
                member_id="husband",
                initial_balance=1_000_000,
                monthly_contribution=0,
                prior_contribution_years=5,
                receive_start_age=30,
                receive_type="一時金",
            )
        )
        result = simulate(store, household)
        lump_month = next(
            m for m in result.monthly if m.date == datetime.date(2026, 4, 1)
        )
        lump_trace = next(t for t in lump_month.traces if t.item == "iDeCo一時金受取")
        assert "重複調整(19年ルール簡易)" not in lump_trace.basis
        retire_month = next(
            m for m in result.monthly if m.date == datetime.date(2056, 4, 1)
        )
        retire_trace = next(
            t for t in retire_month.traces if t.item == "退職金(手取り)"
        )
        assert "重複調整(5年ルール簡易)" not in retire_trace.basis

    def test_ideco_lump_sum_taxed_as_retirement_income(
        self, store, household: Household
    ) -> None:
        """一時金受取は受取開始年齢の到達月に退職所得として分離課税する."""
        household.ideco_plans.append(
            IdecoPlan(
                id="ideco-lump",
                member_id="husband",
                initial_balance=10_000_000,
                monthly_contribution=0,
                receive_start_age=30,
                receive_type="一時金",
                prior_contribution_years=10,
            )
        )
        result = simulate(store, household)
        receive_month = next(
            m for m in result.monthly if m.date == datetime.date(2026, 4, 1)
        )
        assert receive_month.ideco_withdrawal == 10_000_000
        # 退職所得控除 40万×10年=400万 → 課税退職所得 (1000万-400万)/2=300万
        # 所得税 300万×10%-9.75万=20.25万 → 復興税込み 206,752円、住民税 30万
        assert receive_month.ideco_withdrawal_tax == 206_752 + 300_000
        assert receive_month.ideco_balance == 0
        # 一時金は一度だけ支払われる
        after = next(m for m in result.monthly if m.date == datetime.date(2026, 5, 1))
        assert after.ideco_withdrawal == 0

    def test_ideco_annuity_is_taxed_as_public_pension_income(
        self, store, household: Household
    ) -> None:
        """iDeCoの年金受取は公的年金等の雑所得として所得税へ合算する."""
        household.ideco_plans.append(
            IdecoPlan(
                id="ideco-annuity",
                member_id="husband",
                initial_balance=50_000_000,
                monthly_contribution=0,
                receive_start_age=30,
                receive_type="年金",
                monthly_withdrawal=200_000,
            )
        )
        result = simulate(store, household)
        receive_month = next(
            m for m in result.monthly if m.date == datetime.date(2026, 4, 1)
        )
        assert receive_month.ideco_withdrawal == 200_000
        trace = next(
            t for t in receive_month.traces if t.item == "所得税(源泉徴収)"
        )
        # 4-12月の9ヶ月分受取 年180万(65歳未満) → 控除 180万×25%+27.5万=72.5万
        assert trace.basis["公的年金等雑所得"] == 1_800_000 - 725_000

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

    def test_vehicle_distance_taxes_and_residual_sale_are_calculated(
        self, store, household: Household
    ) -> None:
        """走行距離、減税率、残価率から車両費を計算する."""
        household.vehicles.append(
            Vehicle(
                id="efficient-car",
                vehicle_type="中古車",
                ownership_start_year=2026,
                ownership_end_year=2030,
                purchase_price=2_000_000,
                energy_type="ガソリン",
                monthly_distance_km=1_000,
                fuel_efficiency_km_per_liter=10,
                fuel_price_per_liter=180,
                annual_automobile_tax=30_000,
                automobile_tax_reduction_rate=0.5,
                weight_tax_per_inspection=10_000,
                weight_tax_reduction_rate=0.2,
                replacement_cycle_years=3,
                sale_price_mode="残価率",
                residual_value_rate=0.2,
                inspection_cost=100_000,
                inspection_cycle_years=2,
            )
        )
        result = simulate(store, household)
        initial = next(m for m in result.monthly if m.date == datetime.date(2026, 1, 1))
        next_year = next(m for m in result.monthly if m.date == datetime.date(2027, 1, 1))
        replacement = next(m for m in result.monthly if m.date == datetime.date(2029, 1, 1))

        assert initial.vehicle_fuel_expense == 18_000
        assert initial.vehicle_automobile_tax == 15_000
        assert initial.vehicle_inspection_expense == 100_000
        assert initial.vehicle_weight_tax == 8_000
        assert next_year.vehicle_automobile_tax == 15_000
        assert replacement.vehicle_purchase_expense == 2_000_000
        assert replacement.vehicle_sale_income == 400_000

    def test_vehicle_electricity_cost_is_distance_based(
        self, store, household: Household
    ) -> None:
        """電気自動車の電気代を走行距離と電費から計算する."""
        household.vehicles.append(
            Vehicle(
                id="ev",
                ownership_start_year=2026,
                ownership_end_year=2026,
                purchase_price=3_000_000,
                energy_type="電気",
                monthly_distance_km=500,
                electricity_consumption_kwh_per_100km=15,
                electricity_price_per_kwh=30,
            )
        )
        result = simulate(store, household)
        january = next(m for m in result.monthly if m.date == datetime.date(2026, 1, 1))
        assert january.vehicle_electricity_expense == 2_250

    def test_vehicle_taxes_can_be_derived_from_displacement_and_weight(
        self, store, household: Household
    ) -> None:
        """税額未入力時に排気量・重量の標準区分から概算する."""
        household.vehicles.append(
            Vehicle(
                id="tax-calculated-car",
                ownership_start_year=2026,
                ownership_end_year=2030,
                purchase_price=2_000_000,
                engine_displacement_cc=1_500,
                vehicle_weight_kg=1_000,
                inspection_cost=100_000,
                inspection_cycle_years=2,
            )
        )
        result = simulate(store, household)
        january = next(m for m in result.monthly if m.date == datetime.date(2026, 1, 1))
        first_inspection = next(
            m for m in result.monthly if m.date == datetime.date(2029, 1, 1)
        )

        assert january.vehicle_automobile_tax == 30_500
        assert first_inspection.vehicle_weight_tax == 24_600

    def test_vehicle_sale_at_replacement_boundary_uses_elapsed_cycle(
        self, store, household: Household
    ) -> None:
        """所有終了月が買替周期の境界でも、経過期間を0年扱いしない."""
        household.vehicles.append(
            Vehicle(
                id="boundary-sale-car",
                ownership_start_year=2026,
                ownership_end_year=2029,
                ownership_end_month=1,
                purchase_price=3_000_000,
                replacement_cycle_years=3,
                sale_price_mode="定額法",
                depreciation_years=6,
            )
        )

        result = simulate(store, household)
        ending = next(m for m in result.monthly if m.date == datetime.date(2029, 1, 1))

        assert ending.vehicle_purchase_expense == 0
        assert ending.vehicle_sale_income == 1_500_000

    def test_vehicle_weight_tax_uses_category_and_inspection_period(
        self, store, household: Household
    ) -> None:
        """重量税の自動計算が車両区分・車検期間・新車初回期間を反映する."""
        household.vehicles.extend(
            [
                Vehicle(
                    id="light-tax-car",
                    vehicle_category="軽自動車",
                    vehicle_type="中古車",
                    ownership_start_year=2026,
                    ownership_end_year=2026,
                    purchase_price=1_000_000,
                    inspection_cycle_years=1,
                    vehicle_weight_kg=1_000,
                ),
                Vehicle(
                    id="new-tax-car",
                    vehicle_type="新車",
                    ownership_start_year=2026,
                    ownership_end_year=2029,
                    purchase_price=2_000_000,
                    inspection_cycle_years=2,
                    vehicle_weight_kg=1_000,
                ),
            ]
        )

        result = simulate(store, household)
        used_inspection = next(
            m for m in result.monthly if m.date == datetime.date(2026, 1, 1)
        )
        new_first_inspection = next(
            m for m in result.monthly if m.date == datetime.date(2029, 1, 1)
        )

        assert used_inspection.vehicle_weight_tax == 3_300
        assert new_first_inspection.vehicle_weight_tax == 24_600

    def test_motorcycle_automobile_tax_uses_motorcycle_displacement_band(
        self, store, household: Household
    ) -> None:
        """二輪車の自動車税は乗用車ではなく二輪車の排気量区分で概算する."""
        household.vehicles.append(
            Vehicle(
                id="motorcycle",
                vehicle_category="二輪車",
                ownership_start_year=2026,
                ownership_end_year=2026,
                purchase_price=500_000,
                engine_displacement_cc=250,
                vehicle_weight_kg=200,
            )
        )

        result = simulate(store, household)
        january = next(m for m in result.monthly if m.date == datetime.date(2026, 1, 1))

        assert january.vehicle_automobile_tax == 3_600
        assert january.vehicle_weight_tax == 0

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

    def test_vehicle_replacement_financing_settles_old_loan(
        self, store, household: Household
    ) -> None:
        """買替月に旧ローン残債を精算し、買替ローンを開始する."""
        household.loans.append(
            Loan(
                id="long-car-loan",
                member_id="husband",
                principal=500_000,
                annual_rate=0.01,
                years=5,
                start_year=2026,
                start_month=1,
            )
        )
        household.vehicles.append(
            Vehicle(
                id="financed-car",
                name="買替カー",
                ownership_start_year=2026,
                ownership_end_year=2032,
                purchase_price=2_000_000,
                replacement_cycle_years=3,
                sale_price=500_000,
                loan_id="long-car-loan",
                replacement_loan_principal=1_000_000,
                replacement_loan_years=2,
                replacement_loan_fee=20_000,
            )
        )
        result = simulate(store, household)
        replacement = next(m for m in result.monthly if m.date == datetime.date(2029, 1, 1))

        assert replacement.vehicle_purchase_expense == 1_000_000
        assert replacement.vehicle_sale_income == 500_000
        assert replacement.loan_payment > 20_000
        assert any(trace.item == "車両ローン残債精算" for trace in replacement.traces)
        assert any(trace.item == "車両ローン手数料" for trace in replacement.traces)
        assert any(
            trace.basis.get("loan") == "買替カー買替ローン"
            for trace in replacement.traces
            if trace.item == "ローン返済"
        )

    def test_life_event_uses_event_expense_and_disaster_amount(
        self, store, household: Household
    ) -> None:
        """Q8イベントの一時支出と万が一時金額を計上する."""
        household.expenses.append(
            Expense(
                id="marriage-support",
                name="結婚援助",
                event_type="結婚援助",
                monthly_amount=1_000_000,
                cycle="once",
                start_age=35,
                start_month=4,
                disaster_amount=300_000,
            )
        )
        normal = simulate(store, household)
        event_month = next(m for m in normal.monthly if m.date == datetime.date(2031, 4, 1))
        assert event_month.event_expense == 1_000_000
        assert event_month.living_expense == 200_000

        disaster = simulate(store, household, DisasterScenario("husband", 35))
        disaster_month = next(
            m for m in disaster.monthly if m.date == datetime.date(2031, 4, 1)
        )
        assert disaster_month.event_expense == 300_000

    def test_family_event_supports_date_range_and_event_raise_rate(
        self, store, household: Household
    ) -> None:
        """家族別イベントを日付範囲とイベント固有上昇率で計上する."""
        household.expenses.append(
            Expense(
                id="family-event",
                name="子ども支援",
                event_type="汎用",
                member_id="husband",
                monthly_amount=100_000,
                cycle="monthly",
                start_date=datetime.date(2028, 1, 1),
                end_date=datetime.date(2029, 12, 1),
                annual_raise_rate=0.1,
            )
        )
        result = simulate(store, household)
        before = next(m for m in result.monthly if m.date == datetime.date(2027, 12, 1))
        first_year = next(m for m in result.monthly if m.date == datetime.date(2028, 1, 1))
        second_year = next(m for m in result.monthly if m.date == datetime.date(2029, 1, 1))
        after = next(m for m in result.monthly if m.date == datetime.date(2030, 1, 1))

        assert before.event_expense == 0
        assert first_year.event_expense == 100_000
        assert second_year.event_expense == 110_000
        assert after.event_expense == 0

    def test_insurance_premium_and_death_benefit_are_integrated(
        self, store, household: Household
    ) -> None:
        """保険料を計上し、万が一時に死亡保険金を受け取る."""
        household.insurances.append(
            Insurance(
                id="life",
                name="定期生命保険",
                insured_member_id="husband",
                payer_member_id="husband",
                monthly_premium=10_000,
                start_year=2026,
                start_month=1,
                end_year=2060,
                end_month=12,
                death_benefit=10_000_000,
            )
        )
        result = simulate(store, household, DisasterScenario("husband", 40))
        first = next(m for m in result.monthly if m.date == datetime.date(2026, 1, 1))
        death = next(m for m in result.monthly if m.date == datetime.date(2036, 4, 1))
        assert first.insurance_premium == 10_000
        assert death.death_benefit == 10_000_000

    def test_death_benefit_not_paid_after_policy_end(
        self, store, household: Household
    ) -> None:
        """保障期間終了後の死亡では死亡保険金を支払わない."""
        household.insurances.append(
            Insurance(
                id="term-life",
                name="定期生命保険",
                insured_member_id="husband",
                payer_member_id="husband",
                monthly_premium=5_000,
                start_year=2026,
                start_month=1,
                end_year=2030,
                end_month=12,
                death_benefit=10_000_000,
            )
        )
        result = simulate(store, household, DisasterScenario("husband", 40))
        death = next(m for m in result.monthly if m.date == datetime.date(2036, 4, 1))
        after_end = next(m for m in result.monthly if m.date == datetime.date(2031, 1, 1))
        assert death.death_benefit == 0
        assert after_end.insurance_premium == 0

    def test_premium_stops_when_insured_dies(
        self, store, household: Household
    ) -> None:
        """被保険者の死亡で契約が消滅し、以後の保険料は計上しない."""
        household.insurances.append(
            Insurance(
                id="life",
                name="終身保険",
                insured_member_id="husband",
                payer_member_id="wife",
                monthly_premium=10_000,
                start_year=2026,
                start_month=1,
                end_year=2060,
                end_month=12,
                death_benefit=5_000_000,
            )
        )
        result = simulate(store, household, DisasterScenario("husband", 40))
        before = next(m for m in result.monthly if m.date == datetime.date(2036, 3, 1))
        at_death = next(m for m in result.monthly if m.date == datetime.date(2036, 4, 1))
        after = next(m for m in result.monthly if m.date == datetime.date(2036, 5, 1))
        assert before.insurance_premium == 10_000
        assert at_death.insurance_premium == 0
        assert at_death.death_benefit == 5_000_000
        assert after.insurance_premium == 0

    def test_once_event_uses_target_member_age(
        self, store, household: Household
    ) -> None:
        """対象者を指定した1回イベントは対象者の年齢で発火する."""
        household.expenses.append(
            Expense(
                id="child-wedding",
                name="子の結婚援助",
                event_type="結婚援助",
                member_id="child1",
                monthly_amount=1_000_000,
                cycle="once",
                start_age=30,
                start_month=1,
            )
        )
        result = simulate(store, household)
        # 子(2026年1月生まれ)が30歳になるのは2056年1月
        fired = next(m for m in result.monthly if m.date == datetime.date(2056, 1, 1))
        not_fired = next(m for m in result.monthly if m.date == datetime.date(2055, 12, 1))
        assert fired.event_expense == 1_000_000
        assert not_fired.event_expense == 0
        assert any(
            trace.item == "ライフイベント" and trace.basis.get("name") == "子の結婚援助"
            for trace in fired.traces
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
