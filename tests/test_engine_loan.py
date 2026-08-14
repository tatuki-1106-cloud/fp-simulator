"""住宅ローンエンジンのゴールデンテスト."""

from __future__ import annotations

import datetime
import pathlib

import pytest

from fp_simulator.parameters.loader import get_store, reset_store
from fp_simulator.engine.loan import (
    LoanTerms,
    equal_payment_monthly,
    equal_principal_payment,
    loan_schedule,
    mortgage_deduction,
    total_interest,
)


@pytest.fixture(scope="module")
def store():
    reset_store()
    root = pathlib.Path(__file__).resolve().parents[1]
    return get_store(root / "parameters")


class TestEqualPayment:
    """元利均等."""

    def test_30m_1pct_35y(self) -> None:
        """3,000万・1%・35年の月額(概算84,685円)."""
        monthly = equal_payment_monthly(30_000_000, 0.01, 420)
        assert abs(monthly - 84_685) < 100

    def test_zero_rate(self) -> None:
        """金利0%なら単純分割."""
        assert equal_payment_monthly(12_000_000, 0.0, 120) == 100_000


class TestEqualPrincipal:
    """元金均等."""

    def test_first_month(self) -> None:
        """初月: 元金部分は一定、利息は残高×月率."""
        payment, principal, interest = equal_principal_payment(12_000_000, 0.012, 120, 0)
        assert principal == 100_000
        assert interest == 12_000_000 * 0.012 // 12
        assert payment == principal + interest


class TestLoanSchedule:
    """返済スケジュール."""

    def test_full_schedule(self) -> None:
        """35年ローンが420ヶ月で完済される."""
        terms = LoanTerms(principal=30_000_000, annual_rate=0.01, years=35)
        schedule = loan_schedule(terms)
        assert len(schedule) == 420
        assert schedule[-1].balance == 0

    def test_bonus_payment(self) -> None:
        """ボーナス払い月は返済額が増える."""
        terms = LoanTerms(
            principal=30_000_000, annual_rate=0.01, years=35,
            bonus_amount=100_000, bonus_months=[6, 12],
        )
        schedule = loan_schedule(terms)
        june = next(m for m in schedule if m.date.month == 6)
        july = next(m for m in schedule if m.date.month == 7)
        assert june.payment > july.payment

    def test_early_repayment_shorten(self) -> None:
        """繰上返済(期間短縮)で総利息が減る."""
        terms = LoanTerms(principal=30_000_000, annual_rate=0.01, years=35)
        normal = loan_schedule(terms)
        early = loan_schedule(terms, [(datetime.date(2030, 1, 1), 5_000_000, "期間短縮")])
        assert total_interest(early) < total_interest(normal)

    def test_deferment(self) -> None:
        """据置期間は利息のみ."""
        terms = LoanTerms(principal=10_000_000, annual_rate=0.02, years=10, deferment_months=6)
        schedule = loan_schedule(terms)
        for i in range(6):
            assert schedule[i].principal_part == 0
            assert schedule[i].interest_part > 0


class TestMortgageDeduction:
    """住宅ローン控除."""

    def test_basic_deduction(self, store) -> None:
        """年末残高3,000万・所得税10万・住民税15万の場合.

        控除額: 3,000万×0.7% = 21万
        所得税から10万、住民税から(課税所得300万×5%=15万だが法定上限97,500) → 合計197,500
        """
        deduction = mortgage_deduction(
            store, datetime.date(2025, 12, 31),
            year_end_balance=30_000_000,
            income_tax_before_credit=100_000,
            resident_tax_before_credit=150_000,
            years_elapsed=0,
            taxable_income=3_000_000,
        )
        assert deduction == 197_500

    def test_deduction_capped_by_tax(self, store) -> None:
        """税額より控除額が大きい場合は税額が上限."""
        deduction = mortgage_deduction(
            store, datetime.date(2025, 12, 31),
            year_end_balance=30_000_000,
            income_tax_before_credit=50_000,
            resident_tax_before_credit=80_000,
            years_elapsed=0,
            taxable_income=2_000_000,
        )
        # 21万の控除に対し、所得税5万+住民税(課税所得200万×5%=10万、上限97,500)= 130,000
        assert deduction == 130_000

    def test_after_period_ends(self, store) -> None:
        """控除期間(13年)終了後は0."""
        deduction = mortgage_deduction(
            store, datetime.date(2025, 12, 31),
            year_end_balance=30_000_000,
            income_tax_before_credit=100_000,
            resident_tax_before_credit=150_000,
            years_elapsed=13,
            taxable_income=3_000_000,
        )
        assert deduction == 0
