"""FP UNIV改善機能の単体テスト."""

from __future__ import annotations

import datetime

import pytest

from fp_simulator.engine.cashflow import MonthlyCashflow, _apply_account_schedules
from fp_simulator.engine.fp_univ_import import (
    apply_fp_univ_import,
    parse_import_json,
    preview_fp_univ_import,
)
from fp_simulator.engine.loan import LoanTerms, loan_schedule
from fp_simulator.engine.models import (
    Account,
    Household,
    HousingCostSchedule,
    InvestmentSchedule,
    Member,
    Relationship,
    ScheduleEntry,
)
from fp_simulator.engine.schedule import (
    housing_schedule_amount,
    insurance_payment_amount,
    investment_schedule_amount,
    schedule_amount,
)


def test_common_schedule_resolves_age_and_year_boundaries() -> None:
    from fp_simulator.engine.models import ScheduleEntry

    schedule = ScheduleEntry(
        id="s1",
        name="副業",
        kind="income",
        amount=100_000,
        start_age=30,
        end_age=35,
        start_year=2026,
        end_year=2031,
        cycle="yearly",
        payment_month=6,
    )
    assert schedule_amount(schedule, datetime.date(2030, 6, 1), 34, base_year=2026) == 100_000
    assert schedule_amount(schedule, datetime.date(2030, 7, 1), 34, base_year=2026) == 0
    assert schedule_amount(schedule, datetime.date(2032, 6, 1), 36, base_year=2026) == 0


def test_variable_rate_loan_uses_rate_changes() -> None:
    fixed = loan_schedule(
        LoanTerms(principal=10_000_000, annual_rate=0.01, years=10)
    )
    variable = loan_schedule(
        LoanTerms(
            principal=10_000_000,
            annual_rate=0.01,
            years=10,
            is_variable_rate=True,
            rate_schedule=[(datetime.date(2030, 1, 1), 0.04)],
        )
    )
    fixed_interest = sum(item.interest_part for item in fixed)
    variable_interest = sum(item.interest_part for item in variable)
    assert variable_interest > fixed_interest
    assert variable[-1].balance == 0


def test_variable_rate_loan_with_bonus_keeps_original_term() -> None:
    schedule = loan_schedule(
        LoanTerms(
            principal=10_000_000,
            annual_rate=0.01,
            years=10,
            bonus_amount=100_000,
            bonus_months=[6, 12],
            rate_schedule=[(datetime.date(2030, 1, 1), 0.04)],
        )
    )
    assert len(schedule) == 120
    assert schedule[-1].date == datetime.date(2035, 12, 1)
    assert schedule[-1].balance == 0


def test_generic_account_contribution_accumulates_in_target_account() -> None:
    member = Member(
        id="m1",
        name="たろう",
        relationship=Relationship.HOUSEHOLDER,
        birth_date=datetime.date(1990, 1, 1),
    )
    household = Household(
        id="h1",
        name="テスト",
        members=[member],
        accounts=[Account(id="a1", name="積立口座", balance=0)],
        schedules=[
            ScheduleEntry(
                id="s1",
                name="積立",
                kind="account_contribution",
                account_id="a1",
                amount=10_000,
            )
        ],
    )
    balances = {"a1": 0}
    scheduled_balances: dict[str, int] = {}

    for month, expected in ((1, 10_000), (2, 20_000)):
        cashflow = MonthlyCashflow(date=datetime.date(2026, month, 1), age=36)
        investment_balance = _apply_account_schedules(
            household,
            cashflow.date,
            cashflow.age,
            2026,
            balances,
            scheduled_balances,
            cashflow,
        )
        assert balances["a1"] == expected
        assert investment_balance == expected


def test_investment_and_housing_schedules() -> None:
    investment = InvestmentSchedule(
        start_age=30,
        end_age=31,
        monthly_amount=30_000,
        annual_raise_rate=0.1,
    )
    assert investment_schedule_amount(
        investment, datetime.date(2027, 1, 1), 30, base_year=2026
    ) == 33_000
    housing = HousingCostSchedule(
        cost_type="管理費",
        amount=20_000,
        start_year=2026,
        cycle="monthly",
    )
    assert housing_schedule_amount(housing, datetime.date(2026, 5, 1), base_year=2026) == 20_000


def test_insurance_payment_cycles() -> None:
    kwargs = {
        "monthly_premium": 10_000,
        "payment_frequency": "every_n_years",
        "payment_amount": None,
        "payment_month": 4,
        "payment_interval_years": 5,
        "start_year": 2026,
        "start_month": 4,
    }
    assert insurance_payment_amount(current=datetime.date(2031, 4, 1), **kwargs) == 600_000
    assert insurance_payment_amount(current=datetime.date(2030, 4, 1), **kwargs) == 0


def test_fp_univ_import_preview_and_apply() -> None:
    household = Household(
        id="h1",
        name="テスト",
        members=[
            Member(
                id="m1",
                name="たろう",
                relationship=Relationship.HOUSEHOLDER,
                birth_date=datetime.date(1990, 1, 1),
            )
        ],
    )
    payload = {
        "members": [{"name": "たろう", "birth_date": "1990-01-01"}],
        "incomes": [{"member": "たろう", "monthly_amount": 30, "unit": "10k_yen"}],
        "unknown": [{"value": 1}],
    }
    preview = preview_fp_univ_import(payload, household)
    assert preview.has_warnings
    apply_fp_univ_import(payload, household)
    assert household.incomes[0].monthly_amount == 300_000


def test_fp_univ_import_is_idempotent_and_preserves_existing_member_fields() -> None:
    household = Household(
        id="h1",
        name="テスト",
        members=[
            Member(
                id="m1",
                name="たろう",
                relationship=Relationship.HOUSEHOLDER,
                birth_date=datetime.date(1990, 1, 1),
                gender="男",
                life_expectancy_age=100,
            )
        ],
    )
    payload = {
        "members": [{"name": "たろう", "birth_date": "1990-01-01"}],
        "incomes": [{"member": "たろう", "name": "給与", "monthly_amount": 300_000}],
    }

    apply_fp_univ_import(payload, household)
    apply_fp_univ_import(payload, household)

    assert household.members[0].gender == "男"
    assert household.members[0].life_expectancy_age == 100
    assert len(household.incomes) == 1


def test_fp_univ_import_keeps_detailed_schedules_and_insurance_fields() -> None:
    household = Household(
        id="h1",
        name="テスト",
        members=[
            Member(
                id="m1",
                name="たろう",
                relationship=Relationship.HOUSEHOLDER,
                birth_date=datetime.date(1990, 1, 1),
            )
        ],
    )
    payload = {
        "housing": {
            "property_price": 3_000,
            "unit": "10k_yen",
            "purchase_year": 2026,
            "management_fee": 2,
        },
        "accounts": [
            {
                "member": "たろう",
                "name": "証券",
                "contribution_schedule": {
                    "amount": 30_000,
                    "start_age": 30,
                    "end_age": 60,
                    "annual_return_rate": 0.03,
                },
            }
        ],
        "insurance": [
            {
                "member": "たろう",
                "name": "医療保険",
                "insurance_type": "医療",
                "monthly_premium": 5_000,
                "surrender_value_rate": 0.5,
            }
        ],
    }

    apply_fp_univ_import(payload, household)

    assert household.owned_housing is not None
    assert household.owned_housing.cost_schedules[0].amount == 20_000
    assert household.accounts[0].investment_schedules[0].monthly_amount == 30_000
    assert household.insurances[0].insurance_type == "医療"
    assert household.insurances[0].surrender_value_rate == 0.5


def test_fp_univ_import_rejects_non_object_section_items() -> None:
    with pytest.raises(TypeError, match="各要素"):
        parse_import_json('{"incomes": [1]}')
