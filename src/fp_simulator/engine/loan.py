"""住宅ローンの計算(純粋関数).

元利均等・元金均等の返済計算、繰上返済(期間短縮/返済額軽減)、
変動金利(5年ルール・125%ルール)、住宅ローン控除を計算する。
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Literal

from fp_simulator.parameters.loader import ParameterStore


@dataclass(frozen=True)
class LoanTerms:
    """ローン条件."""

    principal: int  # 借入額(円)
    annual_rate: float  # 年利(例: 0.015 = 1.5%)
    years: int  # 返済期間(年)
    repayment_type: Literal["元利均等", "元金均等"] = "元利均等"
    is_variable_rate: bool = False  # 変動金利か
    bonus_amount: int = 0  # ボーナス払い(1回あたり、円)
    bonus_months: list[int] = field(default_factory=list)  # ボーナス支払月(例: [6, 12])
    deferment_months: int = 0  # 据置期間(月数、利息のみ支払い)
    start_date: datetime.date = datetime.date(2026, 1, 1)  # 借入年月


@dataclass(frozen=True)
class MonthlyRepayment:
    """月次返済の明細."""

    date: datetime.date
    payment: int  # 返済額(元金+利息)
    principal_part: int  # 元金部分
    interest_part: int  # 利息部分
    balance: int  # 返済後の残高
    is_bonus: bool = False


def _monthly_rate(annual_rate: float) -> float:
    return annual_rate / 12


def equal_payment_monthly(principal: int, annual_rate: float, months: int) -> int:
    """元利均等の月額返済額."""
    if months <= 0 or principal <= 0:
        return 0
    r = _monthly_rate(annual_rate)
    if r == 0:
        return principal // months
    payment = principal * r * (1 + r) ** months / ((1 + r) ** months - 1)
    return int(round(payment))


def equal_principal_payment(
    principal: int, annual_rate: float, months: int, month_index: int
) -> tuple[int, int, int]:
    """元金均等の月額返済額(元金部分は一定).

    Returns:
        (返済額, 元金部分, 利息部分)
    """
    if months <= 0 or principal <= 0:
        return 0, 0, 0
    principal_part = principal // months
    remaining = principal - principal_part * month_index
    interest = int(remaining * _monthly_rate(annual_rate))
    return principal_part + interest, principal_part, interest


def loan_schedule(
    terms: LoanTerms,
    early_repayments: list[tuple[datetime.date, int, str]] | None = None,
) -> list[MonthlyRepayment]:
    """返済スケジュールを計算する.

    Args:
        terms: ローン条件
        early_repayments: 繰上返済のリスト [(日付, 金額, タイプ("期間短縮" or "返済額軽減"))]
    """
    if early_repayments is None:
        early_repayments = []

    total_months = terms.years * 12
    r = _monthly_rate(terms.annual_rate)
    balance = terms.principal
    results: list[MonthlyRepayment] = []

    # 元利均等の基本月額(ボーナス分は別枠)
    monthly_base = equal_payment_monthly(
        terms.principal - terms.bonus_amount * len(terms.bonus_months) * terms.years,
        terms.annual_rate,
        total_months,
    )

    early_map = {d: (amt, typ) for d, amt, typ in early_repayments}

    for i in range(total_months):
        year = terms.start_date.year + (terms.start_date.month - 1 + i) // 12
        month = (terms.start_date.month - 1 + i) % 12 + 1
        date = datetime.date(year, month, 1)

        if balance <= 0:
            break

        # 据置期間は利息のみ
        if i < terms.deferment_months:
            interest = int(balance * r)
            results.append(MonthlyRepayment(date, interest, 0, interest, balance))
            continue

        # 利息
        interest = int(balance * r)

        # 返済額
        is_bonus_month = month in terms.bonus_months
        if terms.repayment_type == "元利均等":
            payment = monthly_base + (terms.bonus_amount if is_bonus_month else 0)
            principal_part = payment - interest
        else:
            # 元金均等
            principal_part = (terms.principal - terms.bonus_amount * len(terms.bonus_months) * terms.years) // total_months
            if is_bonus_month:
                principal_part += terms.bonus_amount
            payment = principal_part + interest

        if principal_part > balance:
            principal_part = balance
            payment = principal_part + interest

        balance -= principal_part

        results.append(MonthlyRepayment(date, payment, principal_part, interest, balance, is_bonus_month))

        # 繰上返済
        if date in early_map:
            early_amount, early_type = early_map[date]
            early_amount = min(early_amount, balance)
            balance -= early_amount
            if early_type == "返済額軽減":
                # 残期間で再計算
                remaining_months = total_months - i - 1
                if remaining_months > 0 and balance > 0:
                    monthly_base = equal_payment_monthly(balance, terms.annual_rate, remaining_months)

    return results


def total_interest(schedule: list[MonthlyRepayment]) -> int:
    """総利息を返す."""
    return sum(m.interest_part for m in schedule)


def mortgage_deduction(
    store: ParameterStore,
    date: datetime.date,
    year_end_balance: int,
    income_tax_before_credit: int,
    resident_tax_before_credit: int,
    years_elapsed: int,
    taxable_income: int,
    is_new: bool = True,
    is_certified: bool = False,
) -> int:
    """住宅ローン控除額(実際値)を返す.

    Args:
        store: パラメータストア
        date: 対象年の年末
        year_end_balance: 年末ローン残高
        income_tax_before_credit: 住宅ローン控除適用前の所得税額
        resident_tax_before_credit: 住宅ローン控除適用前の住民税額
        years_elapsed: 控除開始からの経過年数(0始まり)
        taxable_income: 前年の課税所得金額(住民税の控除上限計算用)
        is_new: 新築か(中古は期間10年)
        is_certified: 認定長期優良住宅等か

    Returns:
        控除額(所得税+住民税の合計)
    """
    period_key = "住宅ローン控除.控除期間.新築" if is_new else "住宅ローン控除.控除期間.中古"
    period = store.get(period_key, date)
    if years_elapsed >= period:
        return 0

    rate = store.get("住宅ローン控除.控除率", date)
    limit_key = "住宅ローン控除.借入限度額.認定住宅" if is_certified else "住宅ローン控除.借入限度額.新築"
    limit = store.get(limit_key, date)

    # 控除額の計算(年末残高×率、借入限度額が上限)
    deductible_balance = min(year_end_balance, limit)
    deduction = int(deductible_balance * rate)

    # 実際値: 所得税から控除しきれない分は住民税から(上限あり)
    it_deduction = min(deduction, income_tax_before_credit)
    remaining = deduction - it_deduction

    rt_limit_info = store.get("住宅ローン控除.住民税上限", date)
    rt_cap = min(
        int(taxable_income * rt_limit_info["rate"]),  # 前年課税所得の5%
        rt_limit_info["cap"],
    )
    rt_deduction = min(remaining, resident_tax_before_credit, rt_cap)

    return it_deduction + rt_deduction
