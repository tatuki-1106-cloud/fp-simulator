"""産休・育休の計算(純粋関数).

出産手当金・育児休業給付金・社会保険料免除を月次で計算する。
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from fp_simulator.parameters.loader import ParameterStore


@dataclass(frozen=True)
class LeavePeriod:
    """休業期間."""

    start: datetime.date
    end: datetime.date
    leave_type: str  # "産前産後" or "育児"


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


def is_social_insurance_exempt(
    store: ParameterStore, date: datetime.date
) -> bool:
    """産休・育休中の社会保険料免除."""
    return store.get("産休育休.社会保険料免除", date)
