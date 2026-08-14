"""扶養控除・配偶者控除の自動判定(純粋関数).

家族の年齢・収入・生計関係から、所得税・住民税の控除額を計算する。
"""

from __future__ import annotations

import datetime

from fp_simulator.parameters.loader import ParameterStore
from fp_simulator.engine.models import Household, Member, Relationship


def age_at_year_end(birth_date: datetime.date, year: int) -> int:
    """その年の12月31日時点の年齢."""
    return year - birth_date.year - (
        (12, 31) < (birth_date.month, birth_date.day)
    )


def age_at(birth_date: datetime.date, date: datetime.date) -> int:
    """指定日時点の年齢(満年齢)."""
    age = date.year - birth_date.year
    if (date.month, date.day) < (birth_date.month, birth_date.day):
        age -= 1
    return age


def is_eligible_for_spouse_deduction(
    store: ParameterStore,
    year: int,
    householder_income: int,
    spouse_income: int,
) -> bool:
    """配偶者控除の対象か.

    MVPでは簡易判定: 本人合計所得900万円以下 かつ 配偶者合計所得48万円以下
    (配偶者特別控除は将来拡張)
    """
    # 収入→所得の変換は呼び出し側で行う想定。ここでは所得を受け取る
    return householder_income <= 9_000_000 and spouse_income <= 480_000


def is_eligible_for_dependent_deduction(
    store: ParameterStore,
    year: int,
    member: Member,
) -> bool:
    """扶養控除の対象か(一般の控除対象扶養親族: 16歳以上).

    MVPでは年齢のみで判定(収入要件は将来拡張)。
    """
    age = age_at_year_end(member.birth_date, year)
    return age >= 16


def calc_deductions_for_household(
    store: ParameterStore,
    year: int,
    household: Household,
    householder_salary_income: int,
    spouse_salary_income: int,
) -> tuple[int, int]:
    """世帯主の配偶者控除・扶養控除の合計額を返す.

    Returns:
        (配偶者控除額, 扶養控除額合計)
    """
    year_end = datetime.date(year, 12, 31)
    spouse_deduction = 0
    dependent_deduction = 0

    # 配偶者控除
    spouse = next(
        (m for m in household.members if m.relationship == Relationship.SPOUSE), None
    )
    if spouse and is_eligible_for_spouse_deduction(
        store, year, householder_salary_income, spouse_salary_income
    ):
        spouse_deduction = store.get("所得税.配偶者控除.本人所得900万以下", year_end)

    # 扶養控除(16歳以上の子・その他)
    for m in household.members:
        if m.relationship in (Relationship.CHILD, Relationship.OTHER):
            if is_eligible_for_dependent_deduction(store, year, m):
                dependent_deduction += store.get("所得税.扶養控除.一般", year_end)

    return spouse_deduction, dependent_deduction
