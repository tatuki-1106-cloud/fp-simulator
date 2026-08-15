"""産休・育休の計算(純粋関数).

出産手当金・育児休業給付金・社会保険料免除を月次で計算する。
"""

from __future__ import annotations

import datetime
from collections.abc import Iterable
from dataclasses import dataclass

from fp_simulator.engine.models import ChildcareLeave
from fp_simulator.parameters.loader import ParameterStore


@dataclass(frozen=True)
class LeavePeriod:
    """休業期間."""

    start: datetime.date
    end: datetime.date
    leave_type: str  # "産前産後"、"産後パパ育休" or "育児"


def _days_in_month(year: int, month: int) -> int:
    """指定年月の日数を返す."""
    if month == 12:
        next_month = datetime.date(year + 1, 1, 1)
    else:
        next_month = datetime.date(year, month + 1, 1)
    return (next_month - datetime.timedelta(days=1)).day


def leave_periods(leave: ChildcareLeave) -> list[LeavePeriod]:
    """モデルの休業期間を計算用の共通形式へ変換する."""
    periods: list[LeavePeriod] = []
    if leave.maternity_leave_start and leave.maternity_leave_end:
        periods.append(
            LeavePeriod(leave.maternity_leave_start, leave.maternity_leave_end, "産前産後休業")
        )
    if leave.paternity_leave_start and leave.paternity_leave_end:
        periods.append(
            LeavePeriod(leave.paternity_leave_start, leave.paternity_leave_end, "産後パパ育休")
        )
    if leave.childcare_leave_start and leave.childcare_leave_end:
        periods.append(
            LeavePeriod(leave.childcare_leave_start, leave.childcare_leave_end, "育児休業")
        )
    return periods


def matching_periods(
    leaves: Iterable[ChildcareLeave],
    income_id: str,
    member_id: str,
) -> list[LeavePeriod]:
    """対象収入に紐づく休業期間を返す(旧member単位データにも対応)."""
    periods: list[LeavePeriod] = []
    for leave in leaves:
        if leave.member_id != member_id:
            continue
        if leave.income_id is not None and leave.income_id != income_id:
            continue
        periods.extend(leave_periods(leave))
    return sorted(periods, key=lambda period: (period.start, period.end, period.leave_type))


def leave_period_on_date(
    leaves: Iterable[ChildcareLeave],
    income_id: str,
    member_id: str,
    date: datetime.date,
) -> LeavePeriod | None:
    """指定収入の指定日に適用される休業期間を返す."""
    for period in matching_periods(leaves, income_id, member_id):
        if period.start <= date <= period.end:
            return period
    return None


def leave_days_by_type(
    leaves: Iterable[ChildcareLeave],
    income_id: str,
    member_id: str,
    year: int,
    month: int,
) -> dict[str, list[datetime.date]]:
    """指定月の休業日を種別ごとに返す."""
    days_in_month = _days_in_month(year, month)
    result: dict[str, list[datetime.date]] = {}
    for day_number in range(1, days_in_month + 1):
        date = datetime.date(year, month, day_number)
        period = leave_period_on_date(leaves, income_id, member_id, date)
        if period is not None:
            result.setdefault(period.leave_type, []).append(date)
    return result


def leave_includes_month_end(
    leaves: Iterable[ChildcareLeave],
    income_id: str,
    member_id: str,
    year: int,
    month: int,
) -> bool:
    """指定月の末日が休業期間に含まれるかを返す."""
    if month == 12:
        next_month = datetime.date(year + 1, 1, 1)
    else:
        next_month = datetime.date(year, month + 1, 1)
    month_end = next_month - datetime.timedelta(days=1)
    return leave_period_on_date(leaves, income_id, member_id, month_end) is not None


def is_in_leave(periods: list[LeavePeriod], date: datetime.date) -> LeavePeriod | None:
    """指定日が休業期間内かを判定."""
    for p in periods:
        if p.start <= date <= p.end:
            return p
    return None


def maternity_allowance(
    store: ParameterStore, date: datetime.date, standard_remuneration: int
) -> int:
    """出産手当金の月額(産前産後休業中).

    標準報酬日額 = 標準報酬月額 / 30
    給付率 = 2/3
    """
    rate = store.get("産休育休.出産手当金.給付率", date)
    daily = standard_remuneration / 30
    return int(daily * 30 * rate)  # 月額


def maternity_allowance_for_days(
    store: ParameterStore,
    date: datetime.date,
    standard_remuneration: int,
    leave_days: int,
    days_in_month: int,
) -> int:
    """出産手当金を対象日数で日割りした金額."""
    if leave_days <= 0 or days_in_month <= 0:
        return 0
    rate = store.get("産休育休.出産手当金.給付率", date)
    return int(standard_remuneration * rate * leave_days / days_in_month)


def childcare_benefit(
    store: ParameterStore,
    date: datetime.date,
    standard_remuneration: int,
    days_since_leave_start: int,
) -> int:
    """育児休業給付金の月額.

    180日以内: 67%、181日以降: 50%
    """
    if days_since_leave_start <= 180:
        rate = store.get("産休育休.育児休業給付金.給付率.最初の180日", date)
    else:
        rate = store.get("産休育休.育児休業給付金.給付率.181日以降", date)
    return int(standard_remuneration * rate)


def childcare_benefit_for_days(
    store: ParameterStore,
    standard_remuneration: int,
    leave_start: datetime.date,
    dates: Iterable[datetime.date],
) -> int:
    """育児休業給付金を指定日の暦日比率で積算する."""
    total = 0.0
    for date in dates:
        days_in_month = _days_in_month(date.year, date.month)
        elapsed_days = (date - leave_start).days + 1
        if elapsed_days <= 180:
            rate = store.get("産休育休.育児休業給付金.給付率.最初の180日", date)
        else:
            rate = store.get("産休育休.育児休業給付金.給付率.181日以降", date)
        total += standard_remuneration * rate / days_in_month
    return int(total)


def is_social_insurance_exempt(
    store: ParameterStore, date: datetime.date
) -> bool:
    """産休・育休中の社会保険料免除."""
    return store.get("産休育休.社会保険料免除", date)
