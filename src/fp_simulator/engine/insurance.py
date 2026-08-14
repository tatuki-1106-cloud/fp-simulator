"""保険の計算(純粋関数).

生命保険料・死亡保険金・解約返戻金を計算する。
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from collections.abc import Iterable


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
    surrender_value_rate: float = 0.0  # 解約返戻率(累計保険料に対する割合、簡易)
    insurance_type: str = "死亡保障"


@dataclass(frozen=True)
class InsuranceCoverageSummary:
    """基準日時点の保険保障集計."""

    active_policy_count: int
    monthly_premium: int
    death_benefit: int
    surrender_value: int
    by_type: dict[str, int]


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


def analyze_coverage(
    policies: Iterable[InsurancePolicy], date: datetime.date
) -> InsuranceCoverageSummary:
    """基準日時点で有効な保険の保障・保険料・返戻金を集計する."""
    active = [
        policy
        for policy in policies
        if policy.start_date <= date <= policy.end_date
    ]
    by_type: dict[str, int] = {}
    for policy in active:
        by_type[policy.insurance_type] = by_type.get(policy.insurance_type, 0) + policy.death_benefit
    return InsuranceCoverageSummary(
        active_policy_count=len(active),
        monthly_premium=sum(policy.monthly_premium for policy in active),
        death_benefit=sum(policy.death_benefit for policy in active),
        surrender_value=sum(surrender_value(policy, date) for policy in active),
        by_type=by_type,
    )
