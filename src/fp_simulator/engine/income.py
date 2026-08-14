"""給与所得控除・所得計算(純粋関数).

全ての金額は円(整数)。端数処理は法令通り。
"""

from __future__ import annotations

import datetime
from typing import Any

from fp_simulator.parameters.loader import ParameterStore


def salary_deduction(store: ParameterStore, date: datetime.date, salary_income: int) -> int:
    """給与所得控除額を返す.

    Args:
        store: パラメータストア
        date: 適用日(その年の制度で計算。通常は所得の年の12/31)
        salary_income: 給与収入額(年額、円)

    Returns:
        給与所得控除額(円)
    """
    table = store.get("所得税.給与所得控除.速算表", date)
    min_deduction = store.get("所得税.給与所得控除.最低保障額", date)

    if salary_income <= 0:
        return 0

    for bracket in table["brackets"]:
        up_to = bracket["up_to"]
        if up_to is None or salary_income <= up_to:
            if bracket["type"] == "fixed":
                return bracket["amount"]
            # rate_plus: 収入×率 + 加算(subtractは負の値で加算)
            deduction = int(salary_income * bracket["rate"]) - bracket["subtract"]
            return max(deduction, min_deduction)

    # ここには到達しない(最後のbracketはup_to=Noneのはず)
    return min_deduction


def salary_income_after_deduction(
    store: ParameterStore, date: datetime.date, salary_income: int
) -> int:
    """給与所得(給与収入−給与所得控除)を返す."""
    deduction = salary_deduction(store, date, salary_income)
    return max(0, salary_income - deduction)
