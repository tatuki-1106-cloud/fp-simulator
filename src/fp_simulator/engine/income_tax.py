"""所得税(年末調整モデル)の月次計算(純粋関数).

設計方針:
- FP-UNIV同様、月次で源泉徴収相当額を計算し、12月に年末調整で還付/追徴する。
- 実際の源泉徴収税額表は「その月の給与額と扶養人数」に依存するが、
  本システムでは簡略化として「年間推定税額/12」を月次源泉徴収とし、
  12月に確定税額との差額を調整するモデルを採用する。
  (年末調整後の年間納税額は正確に一致する)
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from fp_simulator.parameters.loader import ParameterStore


@dataclass(frozen=True)
class Deductions:
    """所得控除の内訳."""

    basic: int  # 基礎控除
    social_insurance: int  # 社会保険料控除
    spouse: int  # 配偶者控除
    dependent: int  # 扶養控除
    # 将来: 生命保険料控除、地震保険料控除、医療費控除、iDeCo等

    @property
    def total(self) -> int:
        return self.basic + self.social_insurance + self.spouse + self.dependent


def calc_taxable_income(
    store: ParameterStore,
    date: datetime.date,
    salary_income_after_deduction: int,
    deductions: Deductions,
) -> int:
    """課税所得金額(1,000円未満切捨て)を返す."""
    taxable = salary_income_after_deduction - deductions.total
    # 1,000円未満切捨て
    return max(0, taxable // 1000 * 1000)


def calc_income_tax_before_credits(
    store: ParameterStore, date: datetime.date, taxable_income: int
) -> int:
    """課税所得に対する所得税(超過累進、税額控除前)を返す."""
    table = store.get("所得税.税率.速算表", date)
    for bracket in table["brackets"]:
        up_to = bracket["up_to"]
        if up_to is None or taxable_income <= up_to:
            return int(taxable_income * bracket["rate"]) - bracket["deduction"]
    return 0


def calc_reconstruction_tax(store: ParameterStore, date: datetime.date, income_tax: int) -> int:
    """復興特別所得税(所得税額×2.1%)を返す."""
    rate = store.get("所得税.復興特別所得税率", date)
    return int(income_tax * rate)


def calc_annual_income_tax(
    store: ParameterStore,
    date: datetime.date,
    salary_income_after_deduction: int,
    deductions: Deductions,
) -> int:
    """年間の所得税(復興特別所得税込み、確定値)を返す.

    これが年末調整後の年間納税額に相当する。
    """
    taxable = calc_taxable_income(store, date, salary_income_after_deduction, deductions)
    tax = calc_income_tax_before_credits(store, date, taxable)
    return tax + calc_reconstruction_tax(store, date, tax)


@dataclass(frozen=True)
class MonthlyTaxResult:
    """1ヶ月の所得税計算結果."""

    month: datetime.date  # 年月(1日付で表現)
    withholding: int  # 源泉徴収額(概算)
    year_end_adjustment: int  # 年末調整額(12月のみ、マイナス=還付)
    total: int  # 当月の納税額(withholding + adjustment)


def monthly_income_tax_schedule(
    store: ParameterStore,
    year: int,
    monthly_salary: list[int],
    deductions_provider: callable,
) -> list[MonthlyTaxResult]:
    """1年分(1〜12月)の所得税スケジュールを返す.

    Args:
        store: パラメータストア
        year: 対象年
        monthly_salary: 1〜12月の給与額面リスト(12要素)
        deductions_provider: 月(date)を受け取りDeductionsを返す関数

    Returns:
        12ヶ月分のMonthlyTaxResult
    """
    if len(monthly_salary) != 12:
        raise ValueError("monthly_salary は12要素必要です")

    annual_salary = sum(monthly_salary)
    dec31 = datetime.date(year, 12, 31)

    # 年間の給与所得・確定税額を計算
    from fp_simulator.engine.income import salary_income_after_deduction

    deductions = deductions_provider(dec31)
    income_after = salary_income_after_deduction(store, dec31, annual_salary)
    annual_tax = calc_annual_income_tax(store, dec31, income_after, deductions)

    # 簡略化モデル: 月次源泉徴収 = 年間推定税額 / 12 (1月〜11月)
    # 12月に確定税額との差額を年末調整として調整
    monthly_withholding = annual_tax // 12
    results: list[MonthlyTaxResult] = []
    for i, salary in enumerate(monthly_salary):
        month = datetime.date(year, i + 1, 1)
        if salary <= 0:
            results.append(MonthlyTaxResult(month, 0, 0, 0))
            continue
        if i < 11:
            results.append(MonthlyTaxResult(month, monthly_withholding, 0, monthly_withholding))
        else:
            # 12月: 年末調整
            withheld_so_far = monthly_withholding * 11
            adjustment = annual_tax - withheld_so_far
            results.append(MonthlyTaxResult(month, 0, adjustment, adjustment))
    return results
