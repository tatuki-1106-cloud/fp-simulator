"""拡張機能(教育費・iDeCo/NISA・保険)のテスト."""

from __future__ import annotations

import datetime
import pathlib

import pytest

from fp_simulator.engine.childcare_leave import (
    childcare_benefit,
    is_social_insurance_exempt,
    maternity_allowance,
)
from fp_simulator.engine.education import monthly_education_costs
from fp_simulator.engine.insurance import (
    InsurancePolicy,
    analyze_coverage,
    monthly_premium_in_period,
    surrender_value,
)
from fp_simulator.engine.investment import (
    IdecoAccount,
    NisaAccount,
    ideco_annual_deduction,
    ideco_contribution_limit,
    ideco_contribution_years,
    ideco_monthly_step,
    nisa_monthly_step,
    nisa_withdraw,
    withdrawal_amount,
)
from fp_simulator.engine.models import EducationPlan, EducationStage
from fp_simulator.parameters.loader import get_store, reset_store


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

    def test_nursery_public_toddler(self, store) -> None:
        """2歳は保育園費を計上する."""
        monthly, schools = monthly_education_costs(store, D2025, 2, "公立")
        assert monthly == 300000 // 12
        assert "保育園.認可" in schools

    def test_kindergarten_starts_after_nursery(self, store) -> None:
        """3歳になると保育園ではなく幼稚園の標準費用へ切り替わる."""
        monthly, schools = monthly_education_costs(store, D2025, 3, "公立")
        assert monthly == 223000 // 12
        assert "幼稚園.公立" in schools

    def test_stage_specific_custom_costs_and_support(self, store) -> None:
        """段階別の個別費用・一時費用・支援・一人暮らしを反映する."""
        plan = EducationPlan(
            id="education-test",
            member_id="child",
            stages=[
                EducationStage(
                    stage="大学",
                    school_type="私立理系",
                    cost_mode="個別",
                    annual_cost=1_200_000,
                    admission_fee=300_000,
                    annual_material_cost=60_000,
                    annual_transport_cost=60_000,
                    annual_support=120_000,
                    living_arrangement="一人暮らし",
                    monthly_living_cost=50_000,
                )
            ],
        )
        monthly, schools = monthly_education_costs(
            store,
            datetime.date(2025, 4, 1),
            18,
            plan=plan,
            base_year=2025,
        )
        assert monthly == 450_000
        assert "大学.私立理系" in schools

    def test_lessons_are_limited_to_configured_ages(self, store) -> None:
        """習い事は設定した年齢範囲だけ加算する."""
        plan = EducationPlan(
            id="lessons-test",
            member_id="child",
            include_lessons=True,
            lessons_start_age=6,
            lessons_end_age=10,
        )
        monthly, _ = monthly_education_costs(store, D2025, 11, plan=plan)
        assert monthly == 321000 // 12


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
        assert new.contribution_months == 1

    def test_contribution_years_rounds_up(self) -> None:
        """退職所得控除の加入年数は1年未満切上げ・最低1年."""
        assert ideco_contribution_years(IdecoAccount(contribution_months=0), 0) == 1
        assert ideco_contribution_years(IdecoAccount(contribution_months=12), 0) == 1
        assert ideco_contribution_years(IdecoAccount(contribution_months=13), 0) == 2
        # 初期残高分の加入済み年数を加算
        assert ideco_contribution_years(IdecoAccount(contribution_months=24), 10) == 12


class TestNisa:
    """NISA."""

    def test_monthly_step(self, store) -> None:
        """月額投資と運用."""
        acc = NisaAccount(
            tsumitate_balance=500_000, tsumitate_cost=500_000, tracking_year=2025
        )
        new = nisa_monthly_step(store, D2025, acc, 50000, annual_return_rate=0.05)
        assert new.balance > 500_000
        assert new.total_invested == 550_000

    def test_withdrawal_does_not_exceed_balance(self) -> None:
        """取崩額は残高を超えない."""
        assert withdrawal_amount(100_000, 30_000) == 30_000
        assert withdrawal_amount(100_000, 200_000) == 100_000
        assert withdrawal_amount(0, 10_000) == 0

    def test_frame_split_tracks_tsumitate_and_growth(self, store) -> None:
        """つみたて枠と成長投資枠を分離して管理する."""
        acc = NisaAccount(tracking_year=2025)
        new = nisa_monthly_step(store, D2025, acc, 50_000, 100_000)
        assert new.tsumitate_balance == 50_000
        assert new.growth_balance == 100_000
        assert new.year_invested_tsumitate == 50_000
        assert new.year_invested_growth == 100_000

    def test_tsumitate_overflow_spills_to_growth(self, store) -> None:
        """つみたて枠の年間上限(120万円)超過分は成長投資枠へ振り替える."""
        acc = NisaAccount(year_invested_tsumitate=1_150_000, tracking_year=2025)
        new = nisa_monthly_step(store, D2025, acc, 100_000)
        # つみたて枠の残り5万円のみつみたて枠、超過5万円は成長枠へ
        assert new.year_invested_tsumitate == 1_200_000
        assert new.year_invested_growth == 50_000
        assert new.balance == acc.balance + 100_000

    def test_growth_annual_limit_caps_investment(self, store) -> None:
        """成長投資枠は年間240万円まで."""
        acc = NisaAccount(year_invested_growth=2_350_000, tracking_year=2025)
        new = nisa_monthly_step(store, D2025, acc, 0, 100_000)
        assert new.year_invested_growth == 2_400_000
        assert new.growth_balance == 50_000

    def test_growth_lifetime_limit_is_12m(self, store) -> None:
        """成長投資枠の生涯上限は1,200万円."""
        acc = NisaAccount(
            growth_balance=11_950_000, growth_cost=11_950_000, tracking_year=2025
        )
        new = nisa_monthly_step(store, D2025, acc, 0, 100_000)
        assert new.growth_cost == 12_000_000

    def test_lifetime_limit_is_18m(self, store) -> None:
        """生涯非課税保有限度額は1,800万円(簿価ベース)."""
        acc = NisaAccount(
            tsumitate_balance=17_950_000, tsumitate_cost=17_950_000, tracking_year=2025
        )
        new = nisa_monthly_step(store, D2025, acc, 100_000)
        assert new.tsumitate_cost == 18_000_000
        assert new.total_invested == 18_000_000

    def test_withdraw_reduces_cost_and_restores_next_year(self, store) -> None:
        """売却で簿価按分の生涯枠を消費解除し、翌年に枠が復活する."""
        acc = NisaAccount(
            tsumitate_balance=18_000_000 * 2,  # 評価額は簿価の2倍(含み益100%)
            tsumitate_cost=18_000_000,
            tracking_year=2025,
        )
        acc, withdrawal = nisa_withdraw(acc, 3_600_000)
        assert withdrawal == 3_600_000
        # 簿価は取崩額の半分(按分)だけ減る
        assert acc.tsumitate_cost == 16_200_000
        assert acc.pending_restore_tsumitate == 1_800_000
        # 当年は売却分の簿価も生涯枠を消費したまま(復活は翌年)
        assert acc.total_invested == 18_000_000
        new = nisa_monthly_step(store, D2025, acc, 100_000)
        assert new.year_invested_tsumitate == 0
        # 翌年になると簿価分の枠が復活して投資可能になる
        next_year = nisa_monthly_step(
            store, datetime.date(2026, 1, 1), acc, 100_000
        )
        assert next_year.pending_restore_tsumitate == 0
        assert next_year.year_invested_tsumitate == 100_000

    def test_withdraw_is_proportional_across_frames(self, store) -> None:
        """取崩はつみたて枠・成長枠から評価額按分で行う."""
        acc = NisaAccount(
            tsumitate_balance=600_000,
            growth_balance=400_000,
            tsumitate_cost=600_000,
            growth_cost=400_000,
            tracking_year=2025,
        )
        acc, withdrawal = nisa_withdraw(acc, 100_000)
        assert withdrawal == 100_000
        assert acc.tsumitate_balance == 540_000
        assert acc.growth_balance == 360_000
        assert acc.pending_restore_tsumitate == 60_000
        assert acc.pending_restore_growth == 40_000


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

    def test_analyze_coverage(self) -> None:
        """有効契約の保険料・保障額・種類別集計を返す."""
        policies = [
            InsurancePolicy(
                name="生命保険",
                insurance_type="死亡保障",
                insured_member_id="m1",
                payer_member_id="m1",
                monthly_premium=10_000,
                start_date=datetime.date(2020, 1, 1),
                end_date=datetime.date(2060, 12, 1),
                death_benefit=10_000_000,
            ),
            InsurancePolicy(
                name="医療保険",
                insurance_type="医療",
                insured_member_id="m1",
                payer_member_id="m1",
                monthly_premium=5_000,
                start_date=datetime.date(2020, 1, 1),
                end_date=datetime.date(2024, 12, 1),
                death_benefit=0,
            ),
        ]
        summary = analyze_coverage(policies, datetime.date(2025, 1, 1))
        assert summary.active_policy_count == 1
        assert summary.monthly_premium == 10_000
        assert summary.death_benefit == 10_000_000
        assert summary.by_type == {"死亡保障": 10_000_000}

    def test_insurance_model_rejects_invalid_period(self) -> None:
        """終了が開始より前の保険はモデル段階で拒否する."""
        from fp_simulator.engine.models import Insurance

        with pytest.raises(ValueError, match="must not precede"):
            Insurance(
                id="invalid",
                name="不正な保険",
                insured_member_id="m1",
                payer_member_id="m1",
                monthly_premium=1_000,
                start_year=2030,
                end_year=2029,
            )
        with pytest.raises(ValueError):
            Insurance(
                id="negative",
                name="負の保険料",
                insured_member_id="m1",
                payer_member_id="m1",
                monthly_premium=-1,
            )


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
