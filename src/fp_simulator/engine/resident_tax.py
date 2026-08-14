"""住民税(個人住民税)の月次計算(純粋関数).

設計:
- 前年の所得に対して課税(所得割10% + 均等割 + 森林環境税)
- 徴収は翌年6月〜翌々年5月の12ヶ月(特別徴収モデル)
"""

from __future__ import annotations

import datetime

from fp_simulator.parameters.loader import ParameterStore
from fp_simulator.engine.income_tax import Deductions


def calc_annual_resident_tax(
    store: ParameterStore,
    income_year: int,
    taxable_income_for_income_tax: int,
    deductions: Deductions,
) -> int:
    """前年の所得に対する住民税の年額を返す.

    Args:
        store: パラメータストア
        income_year: 所得が発生した年
        taxable_income_for_income_tax: 所得税計算上の課税所得(参考値。
            住民税は基礎控除等の額が異なるため、正確には別計算するが、
            MVPでは所得税の課税所得をベースに住民税の基礎控除差額を調整する簡易モデル)
        deductions: 所得税の控除(参考)

    Returns:
        住民税年額(所得割+均等割+森林環境税)
    """
    # 課税標準: 所得税の課税所得に、所得税と住民税の基礎控除差額を加算
    # (住民税の基礎控除は所得税より小さいため、その分課税標準が大きい)
    year_end = datetime.date(income_year, 12, 31)
    it_basic = store.get("所得税.基礎控除.控除額", year_end)
    rt_basic = store.get("住民税.基礎控除", year_end)

    # 住民税の課税標準 = 所得税の課税所得 + (所得税基礎控除 - 住民税基礎控除)
    taxable_rt = taxable_income_for_income_tax + max(0, it_basic - rt_basic)
    taxable_rt = max(0, taxable_rt // 1000 * 1000)

    rate = store.get("住民税.所得割.税率", year_end)
    income_levy = int(taxable_rt * rate)

    # 均等割+森林環境税(翌年度課税なので翌年の値)
    next_year = datetime.date(income_year + 1, 1, 1)
    per_capita = store.get("住民税.均等割.標準", next_year)
    forest_tax = store.get("住民税.森林環境税", next_year)

    return income_levy + per_capita + forest_tax


def monthly_resident_tax_schedule(
    store: ParameterStore,
    income_year: int,
    taxable_income_for_income_tax: int,
    deductions: Deductions,
) -> dict[datetime.date, int]:
    """前年所得に対する住民税の月次徴収スケジュールを返す.

    徴収期間: income_year+1年の6月 〜 income_year+2年の5月(12ヶ月)
    """
    annual = calc_annual_resident_tax(
        store, income_year, taxable_income_for_income_tax, deductions
    )
    if annual <= 0:
        return {}

    monthly = annual // 12
    remainder = annual - monthly * 12  # 端数は初月(6月)に加算

    schedule: dict[datetime.date, int] = {}
    start = datetime.date(income_year + 1, 6, 1)
    for i in range(12):
        year = start.year + (start.month - 1 + i) // 12
        month = (start.month - 1 + i) % 12 + 1
        d = datetime.date(year, month, 1)
        amount = monthly + (remainder if i == 0 else 0)
        schedule[d] = amount
    return schedule
