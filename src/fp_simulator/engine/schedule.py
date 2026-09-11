"""年月・年齢ベースの共通スケジュール計算."""

from __future__ import annotations

import datetime
from calendar import monthrange

from fp_simulator.engine.models import (
    HousingCostSchedule,
    InvestmentSchedule,
    ScheduleEntry,
)


def _month_key(year: int, month: int) -> int:
    return year * 12 + month


def _in_date_range(
    current: datetime.date,
    start_year: int | None,
    start_month: int,
    end_year: int | None,
    end_month: int,
) -> bool:
    current_key = _month_key(current.year, current.month)
    if start_year is not None and current_key < _month_key(start_year, start_month):
        return False
    return end_year is None or current_key <= _month_key(end_year, end_month)


def schedule_is_active(
    *,
    current: datetime.date,
    age: int,
    start_age: int,
    end_age: int | None,
    start_year: int | None,
    start_month: int,
    end_year: int | None,
    end_month: int,
) -> bool:
    """年齢条件と年月条件を同時に満たすかを返す."""
    if age < start_age or (end_age is not None and age > end_age):
        return False
    return _in_date_range(current, start_year, start_month, end_year, end_month)


def periodic_amount(
    amount: int,
    *,
    cycle: str,
    current: datetime.date,
    payment_month: int = 1,
    payment_interval_years: int = 1,
    anchor_year: int | None = None,
) -> int:
    """周期指定された支払額を当月分へ変換する."""
    if amount <= 0:
        return 0
    if cycle == "monthly":
        return amount
    if cycle == "yearly":
        return amount if current.month == payment_month else 0
    if cycle == "every_n_years":
        if current.month != payment_month:
            return 0
        if anchor_year is None:
            raise ValueError("anchor_year is required for every_n_years schedules")
        return amount if (current.year - anchor_year) % payment_interval_years == 0 else 0
    if cycle == "once":
        return amount if current.month == payment_month else 0
    raise ValueError(f"unsupported schedule cycle: {cycle}")


def schedule_amount(
    schedule: ScheduleEntry,
    current: datetime.date,
    age: int,
    *,
    base_year: int,
) -> int:
    """汎用スケジュールの当月額を返す."""
    if not schedule_is_active(
        current=current,
        age=age,
        start_age=schedule.start_age,
        end_age=schedule.end_age,
        start_year=schedule.start_year,
        start_month=schedule.start_month,
        end_year=schedule.end_year,
        end_month=schedule.end_month,
    ):
        return 0
    years_elapsed = max(0, current.year - base_year)
    amount = int(schedule.amount * (1 + schedule.annual_raise_rate) ** years_elapsed)
    if schedule.cycle == "once":
        if schedule.start_year is None:
            return amount if age == schedule.start_age and current.month == schedule.start_month else 0
        return amount if (
            current.year == schedule.start_year and current.month == schedule.start_month
        ) else 0
    return periodic_amount(
        amount,
        cycle=schedule.cycle,
        current=current,
        payment_month=schedule.payment_month,
    )


def investment_schedule_amount(
    schedule: InvestmentSchedule,
    current: datetime.date,
    age: int,
    *,
    base_year: int,
) -> int:
    if not schedule_is_active(
        current=current,
        age=age,
        start_age=schedule.start_age,
        end_age=schedule.end_age,
        start_year=schedule.start_year,
        start_month=schedule.start_month,
        end_year=schedule.end_year,
        end_month=schedule.end_month,
    ):
        return 0
    years_elapsed = max(0, current.year - base_year)
    return int(schedule.monthly_amount * (1 + schedule.annual_raise_rate) ** years_elapsed)


def housing_schedule_amount(
    schedule: HousingCostSchedule,
    current: datetime.date,
    *,
    base_year: int,
) -> int:
    if not _in_date_range(
        current,
        schedule.start_year,
        schedule.start_month,
        schedule.end_year,
        schedule.end_month,
    ):
        return 0
    years_elapsed = max(0, current.year - base_year)
    amount = int(schedule.amount * (1 + schedule.annual_raise_rate) ** years_elapsed)
    return periodic_amount(
        amount,
        cycle=schedule.cycle,
        current=current,
        payment_month=schedule.payment_month,
    )


def insurance_payment_amount(
    *,
    monthly_premium: int,
    payment_frequency: str,
    payment_amount: int | None,
    payment_month: int,
    payment_interval_years: int,
    current: datetime.date,
    start_year: int,
    start_month: int,
) -> int:
    """保険料の支払周期を月次額へ変換する."""
    amount = payment_amount
    if amount is None:
        if payment_frequency == "monthly":
            amount = monthly_premium
        elif payment_frequency == "yearly":
            amount = monthly_premium * 12
        elif payment_frequency == "every_n_years":
            amount = monthly_premium * 12 * payment_interval_years
        else:
            amount = monthly_premium
    if payment_frequency == "monthly":
        return amount
    if payment_frequency == "yearly":
        return amount if current.month == payment_month else 0
    if payment_frequency == "every_n_years":
        elapsed_years = current.year - start_year
        if current.month != payment_month or elapsed_years < 0:
            return 0
        return amount if elapsed_years % payment_interval_years == 0 else 0
    if payment_frequency == "once":
        return amount if current.year == start_year and current.month == start_month else 0
    raise ValueError(f"unsupported insurance payment frequency: {payment_frequency}")


def valid_day(year: int, month: int, day: int) -> datetime.date:
    """不正な月末日を避けて日付を作る."""
    return datetime.date(year, month, min(day, monthrange(year, month)[1]))
