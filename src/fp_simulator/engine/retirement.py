"""退職金(退職所得)の計算(純粋関数).

分離課税: 退職所得 = (退職金額 - 退職所得控除) × 1/2
所得税・住民税をそれぞれ計算。
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from fp_simulator.parameters.loader import ParameterStore
from fp_simulator.engine.income_tax import (
    calc_income_tax_before_credits,
    calc_reconstruction_tax,
)


def retirement_income_deduction(years_of_service: int) -> int:
    """退職所得控除額を返す.

    - 20年以下: 40万円 × 年数(最低80万円)
    - 20年超: 800万円 + 70万円 × (年数 - 20年)
    """
    if years_of_service <= 20:
        return max(800_000, 400_000 * years_of_service)
    return 8_000_000 + 700_000 * (years_of_service - 20)


def retirement_income(
    store: ParameterStore,
    date: datetime.date,
    retirement_allowance: int,
    years_of_service: int,
) -> int:
    """退職所得金額(課税標準)を返す.

    (退職金額 - 退職所得控除) × 1/2 (1,000円未満切捨て)
    """
    deduction = retirement_income_deduction(years_of_service)
    taxable = max(0, retirement_allowance - deduction) // 2
    return taxable // 1000 * 1000


@dataclass(frozen=True)
class RetirementTax:
    """退職金にかかる税額."""

    income_tax: int  # 所得税(復興特別所得税込み)
    resident_tax: int  # 住民税(所得割)

    @property
    def total(self) -> int:
        return self.income_tax + self.resident_tax


def retirement_tax(
    store: ParameterStore,
    date: datetime.date,
    retirement_allowance: int,
    years_of_service: int,
) -> RetirementTax:
    """退職金にかかる所得税・住民税を返す."""
    taxable = retirement_income(store, date, retirement_allowance, years_of_service)

    # 所得税(分離課税: 他の所得と合算しない)
    it = calc_income_tax_before_credits(store, date, taxable)
    it += calc_reconstruction_tax(store, date, it)

    # 住民税(所得割10%)
    rt_rate = store.get("住民税.所得割.税率", date)
    rt = int(taxable * rt_rate)

    return RetirementTax(income_tax=it, resident_tax=rt)


def net_retirement_allowance(
    store: ParameterStore,
    date: datetime.date,
    retirement_allowance: int,
    years_of_service: int,
) -> int:
    """退職金の手取り額を返す."""
    tax = retirement_tax(store, date, retirement_allowance, years_of_service)
    return retirement_allowance - tax.total
