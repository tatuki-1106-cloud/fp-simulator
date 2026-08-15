"""産休・育休の日付計算とキャッシュフロー統合のテスト."""

from __future__ import annotations

import datetime
import pathlib

import pytest

from fp_simulator.engine.cashflow import simulate
from fp_simulator.engine.childcare_leave import childcare_benefit_for_days
from fp_simulator.engine.models import (
    ChildcareLeave,
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
    return get_store(pathlib.Path(__file__).resolve().parents[1] / "parameters")


def _household(leave: ChildcareLeave) -> Household:
    member = Member(
        id="mother",
        name="母",
        relationship=Relationship.HOUSEHOLDER,
        birth_date=datetime.date(1990, 4, 1),
        gender="女",
        life_expectancy_age=90,
    )
    return Household(
        id="childcare-test",
        name="産休育休テスト",
        members=[member],
        incomes=[
            Income(
                id="salary",
                member_id=member.id,
                name="給与",
                social_insurance_type=SocialInsuranceType.KYOSAI_KOSEI,
                start_age=0,
                end_age=60,
                monthly_amount=300_000,
            )
        ],
        childcare_leaves=[leave],
        assumptions=PlanAssumptions(base_year=2026, base_month=1),
    )


def test_childcare_leave_requires_a_period() -> None:
    with pytest.raises(ValueError, match="at least one"):
        ChildcareLeave(
            id="leave",
            income_id="salary",
            member_id="mother",
            child_birth_date=datetime.date(2026, 1, 1),
        )


def test_childcare_leave_reads_legacy_member_level_json() -> None:
    leave = ChildcareLeave.model_validate(
        {
            "id": "legacy",
            "member_id": "mother",
            "child_birth_date": "2026-01-01",
            "maternity_leave_start": "2025-12-01",
            "maternity_leave_end": "2026-01-31",
            "childcare_leave_start": "2026-02-01",
            "childcare_leave_end": "2026-02-28",
        }
    )

    assert leave.income_id is None


def test_household_rejects_mismatched_childcare_income_member_link() -> None:
    leave = ChildcareLeave(
        id="leave",
        income_id="salary",
        member_id="mother",
        child_birth_date=datetime.date(2026, 1, 1),
        childcare_leave_start=datetime.date(2026, 1, 1),
        childcare_leave_end=datetime.date(2026, 1, 10),
    )
    household = _household(leave)
    household.members.append(
        Member(
            id="other",
            name="別の対象者",
            relationship=Relationship.SPOUSE,
            birth_date=datetime.date(1992, 1, 1),
            life_expectancy_age=90,
        )
    )
    household.childcare_leaves[0] = leave.model_copy(update={"member_id": "other"})

    with pytest.raises(ValueError, match="income and member do not match"):
        household.validate_childcare_leave_links()


def test_childcare_leave_rejects_overlapping_periods() -> None:
    with pytest.raises(ValueError, match="must not overlap"):
        ChildcareLeave(
            id="leave",
            income_id="salary",
            member_id="mother",
            child_birth_date=datetime.date(2026, 1, 1),
            paternity_leave_start=datetime.date(2026, 1, 1),
            paternity_leave_end=datetime.date(2026, 1, 10),
            childcare_leave_start=datetime.date(2026, 1, 10),
            childcare_leave_end=datetime.date(2026, 1, 20),
        )


def test_childcare_benefit_switches_rate_on_day_181(store) -> None:
    start = datetime.date(2026, 1, 1)
    first_180th = start + datetime.timedelta(days=179)
    one_hundred_eighty_first = start + datetime.timedelta(days=180)

    first_amount = childcare_benefit_for_days(store, 300_000, start, [first_180th])
    later_amount = childcare_benefit_for_days(
        store, 300_000, start, [one_hundred_eighty_first]
    )

    assert first_amount > later_amount


def test_simulation_prorates_salary_and_benefits_and_exempts_month_end_si(store) -> None:
    household = _household(
        ChildcareLeave(
            id="leave",
            income_id="salary",
            member_id="mother",
            child_birth_date=datetime.date(2026, 1, 20),
            maternity_leave_start=datetime.date(2026, 1, 15),
            maternity_leave_end=datetime.date(2026, 1, 31),
            paternity_leave_start=datetime.date(2026, 2, 1),
            paternity_leave_end=datetime.date(2026, 2, 5),
            childcare_leave_start=datetime.date(2026, 2, 6),
            childcare_leave_end=datetime.date(2026, 2, 20),
        )
    )

    result = simulate(store, household)
    january = next(month for month in result.monthly if month.date == datetime.date(2026, 1, 1))
    february = next(month for month in result.monthly if month.date == datetime.date(2026, 2, 1))

    assert january.salary_income == 300_000 * 14 // 31
    assert january.maternity_allowance > 0
    assert january.social_insurance == 0
    assert february.salary_income == 300_000 * 8 // 28
    assert february.paternity_leave_benefit > 0
    assert february.childcare_benefit > 0
    assert february.social_insurance > 0
    assert any(trace.item == "社会保険料免除" for trace in january.traces)


def test_leave_only_affects_the_selected_income(store) -> None:
    household = _household(
        ChildcareLeave(
            id="leave",
            income_id="salary",
            member_id="mother",
            child_birth_date=datetime.date(2026, 1, 20),
            maternity_leave_start=datetime.date(2026, 1, 15),
            maternity_leave_end=datetime.date(2026, 1, 31),
        )
    )
    household.incomes.append(
        Income(
            id="side-job",
            member_id="mother",
            name="副業",
            social_insurance_type=SocialInsuranceType.KYOSAI_KOKUMIN,
            start_age=0,
            end_age=60,
            monthly_amount=100_000,
        )
    )

    january = next(
        month
        for month in simulate(store, household).monthly
        if month.date == datetime.date(2026, 1, 1)
    )

    assert january.salary_income == 300_000 * 14 // 31 + 100_000


def test_each_leave_record_has_its_own_benefit_day_counter(store) -> None:
    household = _household(
        ChildcareLeave(
            id="first-child",
            income_id="salary",
            member_id="mother",
            child_birth_date=datetime.date(2026, 1, 1),
            childcare_leave_start=datetime.date(2026, 1, 1),
            childcare_leave_end=datetime.date(2026, 6, 30),
        )
    )
    household.childcare_leaves.append(
        ChildcareLeave(
            id="second-child",
            income_id="salary",
            member_id="mother",
            child_birth_date=datetime.date(2026, 12, 1),
            childcare_leave_start=datetime.date(2026, 12, 1),
            childcare_leave_end=datetime.date(2026, 12, 2),
        )
    )

    december = next(
        month
        for month in simulate(store, household).monthly
        if month.date == datetime.date(2026, 12, 1)
    )

    assert december.childcare_benefit == int(300_000 * 0.67 * 2 / 31)


def test_overlapping_leave_records_are_rejected_before_benefit_calculation(store) -> None:
    household = _household(
        ChildcareLeave(
            id="first",
            income_id="salary",
            member_id="mother",
            child_birth_date=datetime.date(2026, 1, 1),
            childcare_leave_start=datetime.date(2026, 1, 1),
            childcare_leave_end=datetime.date(2026, 1, 10),
        )
    )
    household.childcare_leaves.append(
        ChildcareLeave(
            id="second",
            income_id="salary",
            member_id="mother",
            child_birth_date=datetime.date(2026, 2, 1),
            childcare_leave_start=datetime.date(2026, 1, 10),
            childcare_leave_end=datetime.date(2026, 1, 20),
        )
    )

    with pytest.raises(ValueError, match="must not overlap"):
        simulate(store, household)


def test_benefits_are_not_paid_for_national_insurance_income(store) -> None:
    household = _household(
        ChildcareLeave(
            id="leave",
            income_id="salary",
            member_id="mother",
            child_birth_date=datetime.date(2026, 1, 1),
            childcare_leave_start=datetime.date(2026, 1, 1),
            childcare_leave_end=datetime.date(2026, 1, 31),
        )
    )
    household.incomes[0].social_insurance_type = SocialInsuranceType.KYOSAI_KOKUMIN

    january = next(
        month
        for month in simulate(store, household).monthly
        if month.date == datetime.date(2026, 1, 1)
    )

    assert january.childcare_benefit == 0
