"""保険の計算(純粋関数).

生命保険料・死亡保険金・解約返戻金を計算する。
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass


@dataclass(frozen=True)
class InsurancePolicy:
    """保険契約."""

    name: str
    insured_member_id: str  # 被保険者
    payer_member_id: str  # 支払者
    monthly_premium: int  # 月額保険料
    start_date: datetime.date
    end_date: datetime.date  # 保障期間終了
    death_benefit: int = 0  # 死亡保険金
    surrender_value_rate: float = 0.0  # 解約返戻率(年率、簡易)


def monthly_premium_in_period(
    policy: InsurancePolicy, date: datetime.date
) -> int:
    """指定月の保険料を返す(期間内なら月額、期間外なら0)."""
    if policy.start_date <= date <= policy.end_date:
        return policy.monthly_premium
    return 0


def death_benefit_if_died(
    policy: InsurancePolicy, date: datetime.date
) -> int:
    """指定月に被保険者が死亡した場合の死亡保険金."""
    if policy.start_date <= date <= policy.end_date:
        return policy.death_benefit
    return 0


def surrender_value(
    policy: InsurancePolicy, date: datetime.date
) -> int:
    """指定月時点の解約返戻金(簡易計算).

    支払保険料累計 × 解約返戻率
    """
    if date < policy.start_date:
        return 0
    months = (date.year - policy.start_date.year) * 12 + (date.month - policy.start_date.month)
    total_paid = policy.monthly_premium * min(months, (policy.end_date.year - policy.start_date.year) * 12 + (policy.end_date.month - policy.start_date.month))
    return int(total_paid * policy.surrender_value_rate)
