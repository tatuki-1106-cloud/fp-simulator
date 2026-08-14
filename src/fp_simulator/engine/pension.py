"""公的年金(老齢基礎年金・老齢厚生年金)の計算(純粋関数).

設計:
- 加入月数・平均標準報酬額から年金額を計算
- 繰上げ/繰下げの増減額に対応
- 再評価率は最新値を将来分にも適用(日本年金機構と同方式)
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from fp_simulator.parameters.loader import ParameterStore


@dataclass(frozen=True)
class PensionRecord:
    """年金加入記録."""

    # 国民年金(第1号・第3号・任意)の加入月数
    kokumin_months: int
    # 厚生年金の加入月数
    kousei_months: int
    # 厚生年金期間の平均標準報酬額(再評価後、円)
    avg_standard_remuneration: int
    # 2003年3月以前の加入月数(定額部分計算用)
    kousei_months_before_2003_04: int = 0
    # 2003年4月以降の加入月数(報酬比例計算用)
    kousei_months_after_2003_04: int = 0


def basic_pension_amount(
    store: ParameterStore,
    date: datetime.date,
    kokumin_months: int,
) -> int:
    """老齢基礎年金の年額(繰上/繰下なし).

    満額 × 加入月数 / 480
    """
    full = store.get("年金.老齢基礎年金.満額", date)
    full_months = store.get("年金.老齢基礎年金.満額月数", date)
    return int(full * min(kokumin_months, full_months) / full_months)


def employee_pension_report_proportional(
    store: ParameterStore,
    date: datetime.date,
    avg_standard_remuneration: int,
    kousei_months_after_2003_04: int,
) -> int:
    """老齢厚生年金の報酬比例部分(年額).

    平均標準報酬額 × 乗率 × 加入月数(2003年4月以降)
    """
    rate = store.get("年金.老齢厚生年金.報酬比例乗率", date)
    return int(avg_standard_remuneration * rate * kousei_months_after_2003_04)


def employee_pension_fixed_amount(
    store: ParameterStore,
    date: datetime.date,
    kousei_months_before_2003_04: int,
) -> int:
    """老齢厚生年金の定額部分(年額)."""
    unit = store.get("年金.老齢厚生年金.定額部分単価", date)
    return unit * kousei_months_before_2003_04


def transitional_addition(
    store: ParameterStore,
    date: datetime.date,
    fixed_amount: int,
    kokumin_months: int,
) -> int:
    """経過的加算(定額部分と老齢基礎年金の差額調整).

    定額部分 - 老齢基礎年金(厚生年金加入期間分)
    """
    if fixed_amount <= 0:
        return 0
    full = store.get("年金.老齢基礎年金.満額", date)
    full_months = store.get("年金.老齢基礎年金.満額月数", date)
    # 老齢基礎年金のうち厚生年金加入期間に対応する部分
    basic_for_kousei = int(full * min(kokumin_months, full_months) / full_months)
    return max(0, fixed_amount - basic_for_kousei)


def apply_early_deferral(
    store: ParameterStore,
    date: datetime.date,
    annual_amount: int,
    months_early: int = 0,
    months_deferred: int = 0,
) -> int:
    """繰上げ/繰下げの増減額を適用した年金額を返す.

    Args:
        months_early: 繰上げ月数(65歳前倒し)
        months_deferred: 繰下げ月数(65歳後倒し)
    """
    if months_early > 0:
        rate = store.get("年金.繰上げ.減額率", date)
        return int(annual_amount * (1 - rate * months_early))
    if months_deferred > 0:
        rate = store.get("年金.繰下げ.増額率", date)
        return int(annual_amount * (1 + rate * months_deferred))
    return annual_amount


def additional_pension_spouse(
    store: ParameterStore,
    date: datetime.date,
    kousei_months: int,
) -> int:
    """配偶者の加給年金(要件: 厚生年金20年以上加入で配偶者が65歳未満等).

    MVPでは「厚生年金20年以上加入していれば満額」の簡易判定。
    """
    if kousei_months < 240:
        return 0
    return store.get("年金.加給年金.配偶者", date)


def total_pension(
    store: ParameterStore,
    date: datetime.date,
    record: PensionRecord,
    months_early: int = 0,
    months_deferred: int = 0,
) -> int:
    """老齢年金の合計年額(繰上/繰下適用後).

    老齢基礎年金 + 老齢厚生年金(報酬比例+定額部分+経過的加算)
    """
    basic = basic_pension_amount(store, date, record.kokumin_months)
    proportional = employee_pension_report_proportional(
        store, date, record.avg_standard_remuneration, record.kousei_months_after_2003_04
    )
    fixed = employee_pension_fixed_amount(
        store, date, record.kousei_months_before_2003_04
    )
    transitional = transitional_addition(store, date, fixed, record.kokumin_months)

    total = basic + proportional + fixed + transitional
    return apply_early_deferral(store, date, total, months_early, months_deferred)
