"""公式モデルに近い代表世帯の統合ベンチマーク.

税・年金の公式値そのものは test_engine_tax.py / test_engine_pension.py で検証し、
ここでは現実的な世帯入力を組み合わせたときの月次CF連携を検証する。
"""

from __future__ import annotations

import datetime
import pathlib

import pytest

from fp_simulator.engine.cashflow import simulate
from fp_simulator.engine.loan import LoanTerms, loan_schedule
from fp_simulator.engine.models import (
    Account,
    Expense,
    Household,
    Income,
    Insurance,
    Loan,
    Member,
    PensionRecordInput,
    PlanAssumptions,
    Relationship,
    SocialInsuranceType,
    Vehicle,
)
from fp_simulator.engine.pension import PensionRecord, total_pension
from fp_simulator.parameters.loader import get_store, reset_store


@pytest.fixture(scope="module")
def store():
    reset_store()
    root = pathlib.Path(__file__).resolve().parents[1]
    return get_store(root / "parameters")


@pytest.fixture()
def representative_household() -> Household:
    """会社員・専業配偶者・子1人の現実的な入力例."""
    return Household(
        id="benchmark-household",
        name="代表世帯ベンチマーク",
        members=[
            Member(
                id="worker",
                name="会社員",
                relationship=Relationship.HOUSEHOLDER,
                birth_date=datetime.date(1985, 4, 1),
                gender="男",
                life_expectancy_age=90,
                prefecture="東京都",
            ),
            Member(
                id="spouse",
                name="配偶者",
                relationship=Relationship.SPOUSE,
                birth_date=datetime.date(1987, 9, 1),
                gender="女",
                life_expectancy_age=90,
                prefecture="東京都",
            ),
            Member(
                id="child",
                name="子",
                relationship=Relationship.CHILD,
                birth_date=datetime.date(2015, 6, 1),
                gender="男",
            ),
        ],
        incomes=[
            Income(
                id="worker-income",
                member_id="worker",
                name="会社員給与",
                social_insurance_type=SocialInsuranceType.KYOSAI_KOSEI,
                start_age=25,
                end_age=60,
                monthly_amount=400_000,
                bonus_months=[6, 12],
                bonus_amount=600_000,
            )
        ],
        pension_records=[
            PensionRecordInput(
                member_id="worker",
                kokumin_months=480,
                kousei_months=360,
                avg_standard_remuneration=300_000,
                kousei_months_after_2003_04=360,
                start_age=65,
            )
        ],
        expenses=[
            Expense(
                id="living",
                name="生活費",
                monthly_amount=250_000,
                cycle="monthly",
            ),
            Expense(
                id="annual-event",
                name="帰省・大型支出",
                event_type="汎用",
                monthly_amount=120_000,
                cycle="yearly",
                yearly_month=10,
            ),
        ],
        accounts=[
            Account(
                id="bank",
                name="普通預金",
                account_type="預金",
                balance=5_000_000,
                interest_rate=0.0,
            )
        ],
        loans=[
            Loan(
                id="housing-loan",
                member_id="worker",
                name="住宅ローン",
                principal=30_000_000,
                annual_rate=0.01,
                years=35,
                start_year=2025,
                start_month=1,
            )
        ],
        vehicles=[
            Vehicle(
                id="family-car",
                name="ファミリーカー",
                ownership_start_year=2025,
                ownership_start_month=1,
                ownership_end_year=2030,
                ownership_end_month=12,
                purchase_price=2_000_000,
                monthly_maintenance=10_000,
                annual_tax_repair=60_000,
                sale_price=500_000,
                inspection_cost=100_000,
                inspection_cycle_years=2,
            )
        ],
        insurances=[
            Insurance(
                id="life-insurance",
                name="定期生命保険",
                insurance_type="死亡保障",
                insured_member_id="worker",
                payer_member_id="worker",
                monthly_premium=15_000,
                start_year=2025,
                start_month=1,
                end_year=2055,
                end_month=12,
                death_benefit=20_000_000,
            )
        ],
        assumptions=PlanAssumptions(base_year=2025, base_month=1),
    )


def test_representative_household_cashflow_benchmark(
    store, representative_household: Household
) -> None:
    """公式制度値を含む代表世帯入力が各月次項目へ連携される."""
    result = simulate(store, representative_household)

    january = next(month for month in result.monthly if month.date == datetime.date(2025, 1, 1))
    june = next(month for month in result.monthly if month.date == datetime.date(2025, 6, 1))
    october = next(month for month in result.monthly if month.date == datetime.date(2025, 10, 1))
    inspection = next(month for month in result.monthly if month.date == datetime.date(2028, 1, 1))
    pension_start = next(
        month for month in result.monthly if month.date == datetime.date(2050, 4, 1)
    )

    expected_first_loan = loan_schedule(
        LoanTerms(
            principal=30_000_000,
            annual_rate=0.01,
            years=35,
            start_date=datetime.date(2025, 1, 1),
        )
    )[0]
    expected_pension = total_pension(
        store,
        datetime.date(2050, 4, 1),
        PensionRecord(
            kokumin_months=480,
            kousei_months=360,
            avg_standard_remuneration=300_000,
            kousei_months_after_2003_04=360,
        ),
    ) // 12

    assert january.salary_income == 400_000
    assert june.salary_income == 1_000_000
    assert january.insurance_premium == 15_000
    assert january.loan_payment == expected_first_loan.payment
    assert january.vehicle_purchase_expense == 2_000_000
    assert january.vehicle_tax_repair == 60_000
    assert october.event_expense == 120_000
    assert inspection.vehicle_inspection_expense == 100_000
    assert pension_start.pension_income == expected_pension
    assert sum(
        month.salary_income for month in result.monthly if month.date.year == 2025
    ) == 6_000_000
    assert sum(
        month.insurance_premium for month in result.monthly if month.date.year == 2025
    ) == 180_000
    assert january.balance == 5_000_000 + january.net
