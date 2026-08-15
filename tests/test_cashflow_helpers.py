"""キャッシュフロー責務分割後のヘルパー単体テスト."""

from __future__ import annotations

import datetime
import pathlib

import pytest

from fp_simulator.engine.cashflow import (
    DisasterScenario,
    MonthlyCashflow,
    _apply_income_tax,
    _apply_pension_and_disaster_income,
    _apply_work_income,
    _automatic_child_allowance,
    _automatic_survivor_pension,
)
from fp_simulator.engine.models import (
    ChildcareLeave,
    Household,
    Income,
    Member,
    PensionRecordInput,
    PlanAssumptions,
    Relationship,
    SocialInsuranceType,
)
from fp_simulator.parameters.loader import get_store, reset_store


@pytest.fixture(scope="module")
def store():
    reset_store()
    root = pathlib.Path(__file__).resolve().parents[1]
    return get_store(root / "parameters")


def _householder(birth_date: datetime.date) -> Member:
    return Member(
        id="owner",
        name="世帯主",
        relationship=Relationship.HOUSEHOLDER,
        birth_date=birth_date,
        gender="男",
        life_expectancy_age=90,
    )


def test_apply_work_income_uses_age_at_month_start(store):
    member = _householder(datetime.date(1996, 4, 1))
    household = Household(
        id="helper-income",
        name="収入テスト",
        members=[member],
        incomes=[
            Income(
                id="salary",
                member_id=member.id,
                start_age=30,
                start_month=4,
                monthly_amount=300_000,
                social_insurance_type=SocialInsuranceType.KYOSAI_KOKUMIN,
            )
        ],
        assumptions=PlanAssumptions(base_year=2026, base_month=1),
    )
    alive = lambda _member, _date: True

    before_birthday = MonthlyCashflow(
        date=datetime.date(2026, 3, 1), age=29
    )
    _apply_work_income(
        store,
        household,
        before_birthday.date,
        2026,
        3,
        household.assumptions,
        before_birthday,
        alive,
    )
    assert before_birthday.salary_income == 0

    birthday_month = MonthlyCashflow(
        date=datetime.date(2026, 4, 1), age=30
    )
    _apply_work_income(
        store,
        household,
        birthday_month.date,
        2026,
        4,
        household.assumptions,
        birthday_month,
        alive,
    )
    assert birthday_month.salary_income == 300_000


def test_apply_pension_and_disaster_income_applies_survivor_support(store):
    deceased = _householder(datetime.date(1996, 4, 1))
    child = Member(
        id="child",
        name="子",
        relationship=Relationship.CHILD,
        birth_date=datetime.date(2020, 1, 1),
        gender="女",
    )
    household = Household(
        id="helper-disaster",
        name="万が一テスト",
        members=[deceased, child],
        assumptions=PlanAssumptions(base_year=2026, base_month=1),
    )
    scenario = DisasterScenario(
        name="死亡時",
        deceased_member_id=deceased.id,
        death_age=30,
        survivor_pension_monthly=50_000,
        child_allowance_monthly=10_000,
        child_allowance_end_age=18,
    )
    death_date = datetime.date(2026, 4, 1)
    alive = lambda member, date: member.id != deceased.id or date < death_date
    cf = MonthlyCashflow(date=datetime.date(2026, 5, 1), age=30)

    _apply_pension_and_disaster_income(
        store, household, cf.date, scenario, death_date, cf, alive
    )

    assert cf.survivor_pension == 50_000
    assert cf.child_allowance == 10_000
    assert {trace.item for trace in cf.traces} == {"遺族年金", "児童手当"}


def test_automatic_child_allowance_uses_age_and_birth_order(store):
    household = Household(
        id="helper-child-allowance",
        name="児童手当テスト",
        members=[
            _householder(datetime.date(1990, 1, 1)),
            Member(
                id="child1",
                name="上の子",
                relationship=Relationship.CHILD,
                birth_date=datetime.date(2018, 1, 1),
            ),
            Member(
                id="child2",
                name="真ん中の子",
                relationship=Relationship.CHILD,
                birth_date=datetime.date(2020, 4, 1),
            ),
            Member(
                id="child3",
                name="下の子",
                relationship=Relationship.CHILD,
                birth_date=datetime.date(2024, 4, 1),
            ),
        ],
        assumptions=PlanAssumptions(base_year=2026, base_month=1),
    )

    allowance, basis = _automatic_child_allowance(
        store, household, datetime.date(2026, 1, 1), lambda _member, _date: True
    )

    assert allowance == 50_000
    assert basis["対象児童数"] == 3
    assert basis["第3子算定対象数"] == 3
    assert basis["内訳"] == {"上の子": 10_000, "真ん中の子": 10_000, "下の子": 30_000}


def test_automatic_child_allowance_uses_fiscal_year_end(store):
    household = Household(
        id="helper-child-allowance-end",
        name="児童手当終了テスト",
        members=[
            _householder(datetime.date(1990, 1, 1)),
            Member(
                id="child",
                name="子",
                relationship=Relationship.CHILD,
                birth_date=datetime.date(2006, 4, 1),
            ),
        ],
        assumptions=PlanAssumptions(base_year=2026, base_month=1),
    )

    march_allowance, _ = _automatic_child_allowance(
        store, household, datetime.date(2025, 3, 1), lambda _member, _date: True
    )
    april_allowance, _ = _automatic_child_allowance(
        store, household, datetime.date(2025, 4, 1), lambda _member, _date: True
    )

    assert march_allowance == 10_000
    assert april_allowance == 0


def test_automatic_survivor_pension_uses_joining_record_and_children(store):
    deceased = _householder(datetime.date(1990, 1, 1))
    spouse = Member(
        id="spouse",
        name="配偶者",
        relationship=Relationship.SPOUSE,
        birth_date=datetime.date(1992, 1, 1),
        gender="女",
    )
    child = Member(
        id="child",
        name="子",
        relationship=Relationship.CHILD,
        birth_date=datetime.date(2020, 1, 1),
    )
    household = Household(
        id="helper-survivor-pension",
        name="遺族年金テスト",
        members=[deceased, spouse, child],
        pension_records=[
            PensionRecordInput(
                member_id=deceased.id,
                kokumin_months=120,
                kousei_months=120,
                avg_standard_remuneration=300_000,
                kousei_months_after_2003_04=120,
            )
        ],
        assumptions=PlanAssumptions(base_year=2026, base_month=1),
    )

    monthly, basis = _automatic_survivor_pension(
        store,
        household,
        deceased,
        datetime.date(2026, 4, 1),
        lambda member, _date: member.id != deceased.id,
    )

    assert monthly > 0
    assert basis["自動計算"] is True
    assert basis["遺族基礎年金年額"] > 0
    assert basis["遺族厚生年金年額"] > 0
    assert basis["対象児童"] == ["子"]


def test_apply_income_tax_records_monthly_withholding(store):
    member = _householder(datetime.date(1996, 4, 1))
    household = Household(
        id="helper-tax",
        name="税テスト",
        members=[member],
        incomes=[
            Income(
                id="salary",
                member_id=member.id,
                start_age=0,
                monthly_amount=300_000,
                social_insurance_type=SocialInsuranceType.KYOSAI_KOKUMIN,
            )
        ],
        assumptions=PlanAssumptions(base_year=2026, base_month=1),
    )
    cf = MonthlyCashflow(date=datetime.date(2026, 1, 1), age=29)

    _apply_income_tax(
        store,
        household,
        cf.date,
        2026,
        household.assumptions,
        300_000,
        cf,
        lambda _member, _date: True,
    )

    assert cf.income_tax >= 0
    assert any(trace.item == "所得税(源泉徴収)" for trace in cf.traces)


def test_apply_income_tax_excludes_income_not_started_in_birth_month(store):
    """誕生月の途中(日が2日以降)で開始年齢に達する収入は、その月の推定年収に含めない.

    勤労収入(_apply_work_income)は月次判定を月の1日時点の満年齢で行うため、
    誕生月(誕生日が2日以降)にはまだ給与を支払わない。所得税の推定年収も
    同じ基準で判定し、支払われない収入を課税しないことを確認する。
    """
    member = _householder(datetime.date(1996, 6, 15))
    household = Household(
        id="helper-tax-birthday",
        name="誕生月税テスト",
        members=[member],
        incomes=[
            Income(
                id="salary",
                member_id=member.id,
                start_age=30,
                monthly_amount=300_000,
                social_insurance_type=SocialInsuranceType.KYOSAI_KOKUMIN,
            )
        ],
        assumptions=PlanAssumptions(base_year=2026, base_month=1),
    )
    # 2026年6月: 世帯主は1996-06-15生まれで30歳に達するが、月1日時点では29歳。
    cf = MonthlyCashflow(date=datetime.date(2026, 6, 1), age=29)

    _apply_income_tax(
        store,
        household,
        cf.date,
        2026,
        household.assumptions,
        300_000,  # 他の収入が存在し月次課税ブロックが動作している状況を再現
        cf,
        lambda _member, _date: True,
    )

    # 誕生月はまだ支給されていないため、推定年収0として非課税になる
    assert cf.income_tax == 0
    assert any(trace.item == "所得税(源泉徴収)" for trace in cf.traces)

    # 翌月(2026年7月)は月1日時点で30歳のため、推定年収に含まれ課税される
    cf_next = MonthlyCashflow(date=datetime.date(2026, 7, 1), age=30)
    _apply_income_tax(
        store,
        household,
        cf_next.date,
        2026,
        household.assumptions,
        300_000,
        cf_next,
        lambda _member, _date: True,
    )
    assert cf_next.income_tax > 0


def test_apply_income_tax_does_not_include_future_income(store):
    """現在月に未開始の別収入を当年の源泉徴収推定へ混ぜない."""
    member = _householder(datetime.date(1996, 1, 1))
    assumptions = PlanAssumptions(base_year=2026, base_month=1)
    current_income = Income(
        id="current",
        member_id=member.id,
        start_age=0,
        monthly_amount=300_000,
        social_insurance_type=SocialInsuranceType.KYOSAI_KOKUMIN,
    )
    household = Household(
        id="future-income-tax",
        name="将来収入税テスト",
        members=[member],
        incomes=[current_income],
        assumptions=assumptions,
    )
    baseline = MonthlyCashflow(date=datetime.date(2026, 6, 1), age=30)
    _apply_income_tax(
        store,
        household,
        baseline.date,
        2026,
        assumptions,
        300_000,
        baseline,
        lambda _member, _date: True,
    )

    household.incomes.append(
        Income(
            id="future",
            member_id=member.id,
            start_age=31,
            monthly_amount=1_000_000,
            social_insurance_type=SocialInsuranceType.KYOSAI_KOKUMIN,
        )
    )
    with_future = MonthlyCashflow(date=datetime.date(2026, 6, 1), age=30)
    _apply_income_tax(
        store,
        household,
        with_future.date,
        2026,
        assumptions,
        300_000,
        with_future,
        lambda _member, _date: True,
    )

    assert with_future.income_tax == baseline.income_tax


def test_income_tax_uses_annual_social_insurance_after_leave_exemption(store):
    """休業月に社保が0円でも、他月分の社保控除を所得税推定へ反映する."""
    member = _householder(datetime.date(1990, 1, 1))
    household = Household(
        id="helper-tax-leave",
        name="休業月税テスト",
        members=[member],
        incomes=[
            Income(
                id="salary",
                member_id=member.id,
                start_age=0,
                monthly_amount=300_000,
                social_insurance_type=SocialInsuranceType.KYOSAI_KOSEI,
            )
        ],
        childcare_leaves=[
            ChildcareLeave(
                id="leave",
                income_id="salary",
                member_id=member.id,
                child_birth_date=datetime.date(2026, 1, 20),
                maternity_leave_start=datetime.date(2026, 1, 15),
                maternity_leave_end=datetime.date(2026, 1, 31),
            )
        ],
        assumptions=PlanAssumptions(base_year=2026, base_month=1),
    )
    cf = MonthlyCashflow(date=datetime.date(2026, 1, 1), age=36)
    salary_total = _apply_work_income(
        store,
        household,
        cf.date,
        2026,
        1,
        household.assumptions,
        cf,
        lambda _member, _date: True,
    )
    assert salary_total > 0
    assert cf.social_insurance == 0

    _apply_income_tax(
        store,
        household,
        cf.date,
        2026,
        household.assumptions,
        salary_total,
        cf,
        lambda _member, _date: True,
    )

    tax_trace = next(trace for trace in cf.traces if trace.item == "所得税(源泉徴収)")
    assert tax_trace.basis["年間社会保険料控除"] > 0
