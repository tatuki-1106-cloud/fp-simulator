"""教育費の計算(純粋関数).

子ごとの学校種別・期間に応じた月次教育費を計算する。
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from fp_simulator.parameters.loader import ParameterStore


@dataclass(frozen=True)
class EducationPeriod:
    """教育期間の定義."""

    school_type: str  # 例: "小学校.公立"
    start_age: int  # 入学年齢
    years: int  # 年数


# 標準的な進学パス
STANDARD_PATHS: dict[str, list[EducationPeriod]] = {
    "公立": [
        EducationPeriod("幼稚園.公立", 3, 3),
        EducationPeriod("小学校.公立", 6, 6),
        EducationPeriod("中学校.公立", 12, 3),
        EducationPeriod("高校.公立", 15, 3),
        EducationPeriod("大学.国立", 18, 4),
    ],
    "私立": [
        EducationPeriod("幼稚園.私立", 3, 3),
        EducationPeriod("小学校.私立", 6, 6),
        EducationPeriod("中学校.私立", 12, 3),
        EducationPeriod("高校.私立", 15, 3),
        EducationPeriod("大学.私立文系", 18, 4),
    ],
}


def annual_education_cost(
    store: ParameterStore,
    date: datetime.date,
    school_type: str,
) -> int:
    """学校種別の年間費用を返す."""
    return store.get(f"教育費.{school_type}", date)


def monthly_education_costs(
    store: ParameterStore,
    date: datetime.date,
    child_age: int,
    path: str = "公立",
) -> tuple[int, list[str]]:
    """指定年齢の子の月額教育費と内訳を返す.

    Returns:
        (月額合計, 在学中の学校リスト)
    """
    periods = STANDARD_PATHS.get(path, STANDARD_PATHS["公立"])
    total = 0
    schools: list[str] = []
    for period in periods:
        if period.start_age <= child_age < period.start_age + period.years:
            annual = annual_education_cost(store, date, period.school_type)
            total += annual // 12
            schools.append(period.school_type)
    return total, schools
