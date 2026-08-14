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
)
from fp_simulator.engine.models import (
    Household,
    Income,
    Member,
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
