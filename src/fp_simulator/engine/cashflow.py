"""月次キャッシュフローエンジン(純粋関数).

世帯の全収入・支出・税・社会保険・年金を月単位で積算し、
口座残高の生涯推移を計算する。

設計:
- 月次ループ(基準年月〜世帯主の想定寿命)
- 各月で「収入→税・社保(月次)→手取り→支出→口座変動」を計算
- 所得税は月次源泉徴収+12月年末調整、住民税は前年所得課税で翌年6月〜
- 年金は受給開始年齢から月額(年額/12)を計上
- 退職金は退職年齢の月に手取りを計上
- 全ての金額に根拠トレースを付与(§6.2 トレーサビリティ)
"""

from __future__ import annotations

import calendar
import datetime
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from fp_simulator.engine.childcare_leave import (
    childcare_benefit_for_days,
    leave_days_by_type,
    leave_includes_month_end,
    leave_periods,
    maternity_allowance_for_days,
)
from fp_simulator.engine.dependency import (
    age_at,
    age_at_year_end,
    calc_deductions_for_household,
)
from fp_simulator.engine.education import education_cost_breakdown
from fp_simulator.engine.income import income_is_active, salary_income_after_deduction
from fp_simulator.engine.income_tax import (
    Deductions,
    calc_annual_income_tax,
    calc_taxable_income,
    pension_miscellaneous_income,
    public_pension_deduction,
)
from fp_simulator.engine.insurance import (
    InsurancePolicy,
    death_benefit_if_died,
)
from fp_simulator.engine.investment import (
    IdecoAccount,
    NisaAccount,
    ideco_contribution_limit,
    ideco_contribution_years,
    ideco_monthly_step,
    nisa_annual_limit,
    nisa_monthly_step,
    nisa_withdraw,
    withdrawal_amount,
)
from fp_simulator.engine.loan import LoanTerms, MonthlyRepayment, loan_schedule
from fp_simulator.engine.models import (
    Expense,
    Household,
    Income,
    Member,
    OwnedHousingPlan,
    PlanAssumptions,
    Relationship,
    SocialInsuranceType,
)
from fp_simulator.engine.pension import (
    PensionRecord,
    employee_pension_fixed_amount,
    employee_pension_report_proportional,
    total_pension,
)
from fp_simulator.engine.resident_tax import monthly_resident_tax_schedule
from fp_simulator.engine.retirement import (
    net_retirement_allowance,
    retirement_income,
    retirement_income_deduction,
    retirement_tax,
)
from fp_simulator.engine.schedule import (
    housing_schedule_amount,
    insurance_payment_amount,
    investment_schedule_amount,
    schedule_amount,
)
from fp_simulator.engine.social_insurance import monthly_social_insurance, standard_remuneration
from fp_simulator.parameters.loader import ParameterStore

TraceValue = str | int | float | bool | None | list[str] | dict[str, object]
MemberAlive = Callable[[Member, datetime.date], bool]


@dataclass
class TraceEntry:
    """計算根拠の1エントリ."""

    item: str  # 項目名(例: "所得税")
    amount: int  # 金額
    basis: dict[str, TraceValue]  # 根拠(パラメータパス、式、中間値等)


@dataclass
class MonthlyCashflow:
    """1ヶ月のキャッシュフロー."""

    date: datetime.date  # 年月(1日)
    age: int  # 世帯主の当月末年齢
    # 収入
    salary_income: int = 0
    maternity_allowance: int = 0
    paternity_leave_benefit: int = 0
    childcare_benefit: int = 0
    pension_income: int = 0
    retirement_income: int = 0
    other_income: int = 0
    death_benefit: int = 0
    survivor_pension: int = 0
    child_allowance: int = 0
    ideco_withdrawal: int = 0
    nisa_withdrawal: int = 0
    vehicle_sale_income: int = 0
    # 控除・税
    social_insurance: int = 0
    income_tax: int = 0
    resident_tax: int = 0
    ideco_withdrawal_tax: int = 0
    # 支出
    living_expense: int = 0
    event_expense: int = 0
    housing_down_payment: int = 0
    property_tax: int = 0
    repair_expense: int = 0
    vehicle_purchase_expense: int = 0
    vehicle_maintenance: int = 0
    vehicle_fuel_expense: int = 0
    vehicle_electricity_expense: int = 0
    vehicle_tax_repair: int = 0
    vehicle_automobile_tax: int = 0
    vehicle_weight_tax: int = 0
    vehicle_inspection_expense: int = 0
    loan_payment: int = 0  # ローン返済
    loan_interest: int = 0  # うち利息
    education_expense: int = 0  # 教育費
    insurance_premium: int = 0  # 保険料
    ideco_contribution: int = 0  # iDeCo掛金(所得控除対象)
    nisa_investment: int = 0  # NISA投資
    account_investment: int = 0  # 一般口座の積立
    scheduled_income: int = 0  # 汎用スケジュール収入
    scheduled_expense: int = 0  # 汎用スケジュール支出
    account_investment_balance: int = 0
    scheduled_investment_balance: int = 0
    ideco_balance: int = 0  # iDeCo運用残高
    nisa_balance: int = 0  # NISA運用残高
    # 収支
    @property
    def total_income(self) -> int:
        return (
            self.salary_income
            + self.maternity_allowance
            + self.paternity_leave_benefit
            + self.childcare_benefit
            + self.pension_income
            + self.retirement_income
            + self.other_income
            + self.death_benefit
            + self.survivor_pension
            + self.child_allowance
            + self.ideco_withdrawal
            + self.nisa_withdrawal
            + self.vehicle_sale_income
            + self.scheduled_income
        )

    @property
    def total_expense(self) -> int:
        return (
            self.living_expense
            + self.event_expense
            + self.housing_down_payment
            + self.property_tax
            + self.repair_expense
            + self.vehicle_purchase_expense
            + self.vehicle_maintenance
            + self.vehicle_fuel_expense
            + self.vehicle_electricity_expense
            + self.vehicle_tax_repair
            + self.vehicle_automobile_tax
            + self.vehicle_weight_tax
            + self.vehicle_inspection_expense
            + self.loan_payment
            + self.education_expense
            + self.insurance_premium
            + self.ideco_contribution
            + self.nisa_investment
            + self.account_investment
            + self.scheduled_expense
        )

    @property
    def total_tax_si(self) -> int:
        return (
            self.social_insurance
            + self.income_tax
            + self.resident_tax
            + self.ideco_withdrawal_tax
        )

    @property
    def net(self) -> int:
        """手取り−支出."""
        return self.total_income - self.total_tax_si - self.total_expense

    @property
    def total_assets(self) -> int:
        """現金・預金とiDeCo/NISAを合算した金融資産残高."""
        return (
            self.balance
            + self.ideco_balance
            + self.nisa_balance
            + self.account_investment_balance
            + self.scheduled_investment_balance
        )

    # 残高
    balance: int = 0  # 当月末の世帯総残高
    traces: list[TraceEntry] = field(default_factory=list)


@dataclass
class SimulationResult:
    """シミュレーション結果."""

    monthly: list[MonthlyCashflow]
    parameter_snapshot: dict[str, Any]  # 計算時のパラメータ版(再現性)


@dataclass
class FinancingContext:
    """ローン・車両買替の事前計算結果."""

    loan_repayments_by_date: defaultdict[
        datetime.date, list[tuple[str, MonthlyRepayment]]
    ]
    vehicle_loan_settlements_by_date: defaultdict[
        datetime.date, list[tuple[str, int]]
    ]
    vehicle_loan_fees_by_date: defaultdict[datetime.date, list[tuple[str, int]]]
    vehicle_replacement_principal: dict[tuple[str, datetime.date], int]
    vehicle_replacement_dates: dict[str, list[datetime.date]]


@dataclass(frozen=True)
class IncomeMonthCompensation:
    """収入1件の当月支給額と休業日数."""

    base_salary: int
    base_bonus: int
    salary: int
    bonus: int
    days_in_month: int
    work_days: int
    leave_days: dict[str, list[datetime.date]]


@dataclass(frozen=True)
class DisasterScenario:
    """万が一シナリオ。指定メンバーが指定年齢で死亡した前提.

    遺族年金は原則として加入記録と家族情報から簡易自動計算し、
    ``survivor_pension_monthly`` を指定した場合だけ手入力額を上書きする。
    児童手当は原則として世帯の子ども情報から自動計算し、
    ``child_allowance_monthly`` を指定した場合だけ従来の手入力額を上書きする。
    """

    deceased_member_id: str
    death_age: int
    name: str = "万が一"
    survivor_pension_monthly: int | None = None
    child_allowance_monthly: int | None = None
    child_allowance_end_age: int = 18
    living_expense_reduction_rate: float = 0.0


def _add_months(date: datetime.date, months: int) -> datetime.date:
    """date に months ヶ月を加算(日は1日固定)."""
    total = date.year * 12 + date.month - 1 + months
    return datetime.date(total // 12, total % 12 + 1, 1)


def _fiscal_year_end_after_age(birth_date: datetime.date, age: int) -> datetime.date:
    """指定年齢に達した後の最初の3月31日を返す."""
    birthday_year = birth_date.year + age
    birthday_day = min(
        birth_date.day, calendar.monthrange(birthday_year, birth_date.month)[1]
    )
    birthday = datetime.date(birthday_year, birth_date.month, birthday_day)
    end_year = birthday.year if birthday.month <= 3 else birthday.year + 1
    return datetime.date(end_year, 3, 31)


def _automatic_child_allowance(
    store: ParameterStore,
    household: Household,
    current: datetime.date,
    member_alive: MemberAlive,
) -> tuple[int, dict[str, TraceValue]]:
    """児童手当を2024年10月改正後の簡易モデルで計算する."""
    eligible_age = int(store.get("児童手当.支給対象.年齢上限", current))
    countable_age = int(store.get("児童手当.第3子算定.年齢上限", current))
    children = [
        member
        for member in household.members
        if (
            member.relationship == Relationship.CHILD
            and member_alive(member, current)
            and current <= _fiscal_year_end_after_age(member.birth_date, countable_age)
        )
    ]
    children.sort(key=lambda member: (member.birth_date, member.id))
    ranks = {member.id: index + 1 for index, member in enumerate(children)}
    eligible_children = [
        member
        for member in children
        if current <= _fiscal_year_end_after_age(member.birth_date, eligible_age)
    ]

    total = 0
    breakdown: dict[str, int] = {}
    eligible_names: list[str] = []
    for member in eligible_children:
        rank = ranks[member.id]
        age = age_at(member.birth_date, current)
        if rank >= 3:
            amount = int(store.get("児童手当.第3子以降.月額", current))
        elif age < 3:
            amount = int(store.get("児童手当.第1子第2子.3歳未満月額", current))
        else:
            amount = int(store.get("児童手当.第1子第2子.3歳以上月額", current))
        total += amount
        breakdown[member.name] = amount
        eligible_names.append(f"{member.name}（{age}歳・第{rank}子）")

    return total, {
        "自動計算": True,
        "対象児童": eligible_names,
        "対象児童数": len(eligible_children),
        "第3子算定対象数": len(children),
        "内訳": breakdown,
        "パラメータ": [
            "児童手当.支給対象.年齢上限",
            "児童手当.第3子算定.年齢上限",
            "児童手当.第1子第2子.3歳未満月額",
            "児童手当.第1子第2子.3歳以上月額",
            "児童手当.第3子以降.月額",
        ],
    }


def _survivor_pension_record(
    household: Household, deceased: Member, current: datetime.date
) -> PensionRecord | None:
    """遺族年金の簡易算定に使う加入記録を解決する."""
    pension_input = next(
        (record for record in household.pension_records if record.member_id == deceased.id),
        None,
    )
    if pension_input is not None and (
        pension_input.kokumin_months > 0 or pension_input.kousei_months > 0
    ):
        after = pension_input.kousei_months_after_2003_04 or max(
            0, pension_input.kousei_months - pension_input.kousei_months_before_2003_04
        )
        return PensionRecord(
            kokumin_months=pension_input.kokumin_months,
            kousei_months=pension_input.kousei_months,
            avg_standard_remuneration=pension_input.avg_standard_remuneration,
            kousei_months_before_2003_04=pension_input.kousei_months_before_2003_04,
            kousei_months_after_2003_04=after,
        )

    kousei_income = next(
        (
            income
            for income in household.incomes
            if income.member_id == deceased.id
            and income.social_insurance_type
            in (SocialInsuranceType.KYOSAI_KOSEI, SocialInsuranceType.YAKUIN_KOSEI)
            and age_at(deceased.birth_date, current) >= income.start_age
            and (
                income.end_age is None
                or age_at(deceased.birth_date, current) <= income.end_age
            )
            and not (
                age_at(deceased.birth_date, current) == income.start_age
                and current.month < income.start_month
            )
            and not (
                income.end_age is not None
                and age_at(deceased.birth_date, current) == income.end_age
                and current.month > income.end_month
            )
        ),
        None,
    )
    if kousei_income is None:
        return None

    start_year = deceased.birth_date.year + kousei_income.start_age
    start_date = datetime.date(start_year, kousei_income.start_month, 1)
    months = max(
        1,
        (current.year - start_date.year) * 12 + current.month - start_date.month + 1,
    )
    before = 0
    after = months
    if start_date < datetime.date(2003, 4, 1):
        before = min(months, (2003 - start_date.year) * 12 + 3 - start_date.month)
        after = months - before
    return PensionRecord(
        kokumin_months=min(months, 480),
        kousei_months=months,
        avg_standard_remuneration=kousei_income.monthly_amount,
        kousei_months_before_2003_04=before,
        kousei_months_after_2003_04=after,
    )


def _eligible_survivor_children(
    store: ParameterStore,
    household: Household,
    current: datetime.date,
    member_alive: MemberAlive,
) -> list[Member]:
    """遺族基礎年金の対象となる子を簡易判定する."""
    age_limit = int(store.get("遺族基礎年金.子.年齢上限", current))
    disability_age_limit = int(store.get("遺族基礎年金.障害児.年齢上限", current))
    return [
        member
        for member in household.members
        if (
            member.relationship == Relationship.CHILD
            and member_alive(member, current)
            and (
                current <= _fiscal_year_end_after_age(member.birth_date, age_limit)
                or (
                    member.disability_grade in {"1級", "2級"}
                    and age_at(member.birth_date, current) < disability_age_limit
                )
            )
        )
    ]


def _automatic_survivor_pension(
    store: ParameterStore,
    household: Household,
    deceased: Member,
    current: datetime.date,
    member_alive: MemberAlive,
) -> tuple[int, dict[str, TraceValue]]:
    """既存の加入記録と家族情報による遺族年金の簡易自動判定."""
    children = _eligible_survivor_children(store, household, current, member_alive)
    spouse = next(
        (
            member
            for member in household.members
            if member.relationship == Relationship.SPOUSE
            and member_alive(member, current)
        ),
        None,
    )
    record = _survivor_pension_record(household, deceased, current)
    has_basic_coverage = record is not None
    has_employee_coverage = record is not None and record.kousei_months > 0
    spouse_can_receive_employee = spouse is not None and (
        spouse.gender != "男"
        or age_at(spouse.birth_date, current) >= 55
        or bool(children)
    )
    has_recipient = bool(children) or spouse_can_receive_employee

    basic_annual = 0
    employee_annual = 0
    pension_months = 0
    if has_recipient and has_basic_coverage and children:
        basic_annual = int(store.get("遺族基礎年金.本体.年額", current))
        first_two = int(
            store.get("遺族基礎年金.子の加算.第1子第2子.年額", current)
        )
        third_onward = int(
            store.get("遺族基礎年金.子の加算.第3子以降.年額", current)
        )
        basic_annual += first_two * min(2, len(children))
        basic_annual += third_onward * max(0, len(children) - 2)

    if has_recipient and has_employee_coverage and record is not None:
        pension_months = record.kousei_months
        minimum_months = int(store.get("遺族厚生年金.短期要件.みなし加入月数", current))
        effective_after = max(record.kousei_months_after_2003_04, minimum_months)
        proportional = employee_pension_report_proportional(
            store, current, record.avg_standard_remuneration, effective_after
        )
        fixed = employee_pension_fixed_amount(
            store, current, record.kousei_months_before_2003_04
        )
        rate = float(store.get("遺族厚生年金.報酬比例.支給率", current))
        employee_annual = int((proportional + fixed) * rate)

    total_annual = basic_annual + employee_annual
    monthly = total_annual // 12
    recipients = []
    if children:
        recipients.append("子")
    if spouse_can_receive_employee and spouse is not None:
        recipients.append(f"配偶者({spouse.name})")
    return monthly, {
        "自動計算": True,
        "判定": "簡易判定（未納月数・死亡原因・生計維持は未入力）",
        "死亡者": deceased.name,
        "受給候補": recipients,
        "対象児童": [member.name for member in children],
        "加入記録あり": has_basic_coverage,
        "厚生年金加入月数": pension_months,
        "遺族基礎年金年額": basic_annual,
        "遺族厚生年金年額": employee_annual,
        "月額": monthly,
        "パラメータ": [
            "遺族基礎年金.本体.年額",
            "遺族基礎年金.子の加算.第1子第2子.年額",
            "遺族基礎年金.子の加算.第3子以降.年額",
            "遺族厚生年金.報酬比例.支給率",
            "遺族厚生年金.短期要件.みなし加入月数",
        ],
    }


def _loan_balance_at(
    schedule: list[MonthlyRepayment], principal: int, target: datetime.date
) -> int:
    """指定月の返済後残高を返す."""
    balance = principal
    for repayment in schedule:
        if repayment.date > target:
            break
        balance = repayment.balance
    return balance


def _prepare_financing(
    household: Household, start: datetime.date, end: datetime.date
) -> FinancingContext:
    """ローン返済・車両買替ローンのスケジュールを事前計算する."""
    loan_repayments_by_date: defaultdict[
        datetime.date, list[tuple[str, MonthlyRepayment]]
    ] = defaultdict(list)
    vehicle_loan_settlements_by_date: defaultdict[
        datetime.date, list[tuple[str, int]]
    ] = defaultdict(list)
    vehicle_loan_fees_by_date: defaultdict[
        datetime.date, list[tuple[str, int]]
    ] = defaultdict(list)
    vehicle_replacement_principal: dict[tuple[str, datetime.date], int] = {}
    vehicle_replacement_dates: dict[str, list[datetime.date]] = {}
    loan_stop_dates: dict[str, datetime.date] = {}
    loan_schedules: dict[str, list[MonthlyRepayment]] = {}

    for vehicle in household.vehicles:
        start_date = datetime.date(
            vehicle.ownership_start_year, vehicle.ownership_start_month, 1
        )
        end_date = datetime.date(
            vehicle.ownership_end_year, vehicle.ownership_end_month, 1
        )
        replacement_dates: list[datetime.date] = []
        if vehicle.replacement_cycle_years > 0:
            next_replacement = _add_months(
                start_date, vehicle.replacement_cycle_years * 12
            )
            while next_replacement < end_date:
                replacement_dates.append(next_replacement)
                next_replacement = _add_months(
                    next_replacement, vehicle.replacement_cycle_years * 12
                )
        vehicle_replacement_dates[vehicle.id] = replacement_dates
        if vehicle.loan_id:
            loan_stop_dates[vehicle.loan_id] = min(
                loan_stop_dates.get(vehicle.loan_id, end_date),
                replacement_dates[0] if replacement_dates else end_date,
            )

    for loan in household.loans:
        terms = LoanTerms(
            principal=loan.principal,
            annual_rate=loan.annual_rate,
            years=loan.years,
            repayment_type=loan.repayment_type,
            is_variable_rate=loan.is_variable_rate,
            bonus_amount=loan.bonus_amount,
            bonus_months=loan.bonus_months,
            deferment_months=loan.deferment_months,
            start_date=datetime.date(loan.start_year, loan.start_month, 1),
            rate_schedule=[
                (datetime.date(item.year, item.month, 1), item.annual_rate)
                for item in loan.rate_schedule
            ],
        )
        early = [
            (datetime.date(y, m, 1), amount, repayment_type)
            for y, m, amount, repayment_type in loan.early_repayments
        ]
        schedule = loan_schedule(terms, early)
        loan_schedules[loan.id] = schedule
        stop_date = loan_stop_dates.get(loan.id, end)
        for repayment in schedule:
            if start <= repayment.date <= end and repayment.date <= stop_date:
                loan_repayments_by_date[repayment.date].append((loan.name, repayment))

    for vehicle in household.vehicles:
        replacement_dates = vehicle_replacement_dates[vehicle.id]
        financing_schedule = (
            loan_schedules.get(vehicle.loan_id) if vehicle.loan_id else None
        )
        financing_principal = (
            next(
                loan.principal
                for loan in household.loans
                if loan.id == vehicle.loan_id
            )
            if vehicle.loan_id
            and any(loan.id == vehicle.loan_id for loan in household.loans)
            else 0
        )
        settlement_dates = [
            *replacement_dates,
            datetime.date(
                vehicle.ownership_end_year, vehicle.ownership_end_month, 1
            ),
        ]
        for index, settlement_date in enumerate(settlement_dates):
            if financing_schedule is not None and financing_principal > 0:
                balance = _loan_balance_at(
                    financing_schedule, financing_principal, settlement_date
                )
                if balance > 0:
                    vehicle_loan_settlements_by_date[settlement_date].append(
                        (vehicle.name, balance)
                    )
            if index >= len(replacement_dates):
                break

            replacement_date = replacement_dates[index]
            replacement_principal = vehicle.replacement_loan_principal
            vehicle_replacement_principal[
                (vehicle.id, replacement_date)
            ] = replacement_principal
            if vehicle.replacement_loan_fee > 0:
                vehicle_loan_fees_by_date[replacement_date].append(
                    (vehicle.name, vehicle.replacement_loan_fee)
                )
            if replacement_principal <= 0 or vehicle.replacement_loan_years <= 0:
                financing_schedule = None
                financing_principal = 0
                continue

            replacement_terms = LoanTerms(
                principal=replacement_principal,
                annual_rate=vehicle.replacement_loan_annual_rate,
                years=vehicle.replacement_loan_years,
                repayment_type=vehicle.replacement_loan_repayment_type,
                start_date=replacement_date,
            )
            financing_schedule = loan_schedule(replacement_terms)
            financing_principal = replacement_principal
            next_settlement = settlement_dates[index + 1]
            for repayment in financing_schedule:
                if repayment.date > next_settlement:
                    break
                loan_repayments_by_date[repayment.date].append(
                    (f"{vehicle.name}買替ローン", repayment)
                )

    return FinancingContext(
        loan_repayments_by_date=loan_repayments_by_date,
        vehicle_loan_settlements_by_date=vehicle_loan_settlements_by_date,
        vehicle_loan_fees_by_date=vehicle_loan_fees_by_date,
        vehicle_replacement_principal=vehicle_replacement_principal,
        vehicle_replacement_dates=vehicle_replacement_dates,
    )


def _record_event_expense(
    cf: MonthlyCashflow, amount: int, expense: Expense
) -> None:
    """イベント支出と計算根拠を月次CFへ記録する."""
    cf.event_expense += amount
    if amount > 0:
        cf.traces.append(
            TraceEntry(
                "ライフイベント",
                amount,
                {
                    "name": expense.name,
                    "type": expense.event_type,
                    "周期": expense.cycle,
                },
            )
        )


def _income_month_compensation(
    household: Household,
    income: Income,
    member: Member,
    current: datetime.date,
    assumptions: PlanAssumptions,
) -> IncomeMonthCompensation | None:
    """休業日数を反映した収入1件の月次支給額を返す."""
    if not income_is_active(income, member, current):
        return None

    days_in_month = calendar.monthrange(current.year, current.month)[1]
    years_elapsed = current.year - assumptions.base_year
    raise_factor = (1 + income.annual_raise_rate) ** years_elapsed
    base_salary = int(income.monthly_amount * raise_factor)
    base_bonus = int(income.bonus_amount * raise_factor)
    leave_days = leave_days_by_type(
        household.childcare_leaves,
        income.id,
        member.id,
        current.year,
        current.month,
    )
    total_leave_days = sum(len(days) for days in leave_days.values())
    work_days = max(days_in_month - total_leave_days, 0)
    return IncomeMonthCompensation(
        base_salary=base_salary,
        base_bonus=base_bonus,
        salary=base_salary * work_days // days_in_month,
        bonus=base_bonus * work_days // days_in_month
        if current.month in income.bonus_months
        else 0,
        days_in_month=days_in_month,
        work_days=work_days,
        leave_days=leave_days,
    )


def _annual_salary_estimate(
    household: Household,
    year: int,
    assumptions: PlanAssumptions,
    member_alive: MemberAlive,
    reference_date: datetime.date | None = None,
) -> int:
    """休業日数を反映した年間給与・賞与の推定額."""
    total = 0
    for month in range(1, 13):
        current = datetime.date(year, month, 1)
        for income in household.incomes:
            member = next((m for m in household.members if m.id == income.member_id), None)
            if member is None or not member_alive(member, current):
                continue
            if reference_date is not None and not income_is_active(income, member, reference_date):
                continue
            compensation = _income_month_compensation(
                household, income, member, current, assumptions
            )
            if compensation is not None:
                total += compensation.salary + compensation.bonus
    return total


def _annual_social_insurance_estimate(
    store: ParameterStore,
    household: Household,
    year: int,
    assumptions: PlanAssumptions,
    member_alive: MemberAlive,
    reference_date: datetime.date | None = None,
) -> int:
    """休業月の月末免除を反映した年間社会保険料の推定額."""
    total = 0
    employee_types = (
        SocialInsuranceType.KYOSAI_KOSEI,
        SocialInsuranceType.YAKUIN_KOSEI,
    )
    for month in range(1, 13):
        current = datetime.date(year, month, 1)
        for income in household.incomes:
            if income.social_insurance_type not in employee_types:
                continue
            member = next((m for m in household.members if m.id == income.member_id), None)
            if member is None or not member_alive(member, current):
                continue
            if reference_date is not None and not income_is_active(income, member, reference_date):
                continue
            compensation = _income_month_compensation(
                household, income, member, current, assumptions
            )
            if compensation is None or leave_includes_month_end(
                household.childcare_leaves,
                income.id,
                member.id,
                year,
                month,
            ):
                continue
            premiums = monthly_social_insurance(
                store,
                current,
                compensation.base_salary,
                member.prefecture,
                age_at(member.birth_date, current),
                is_employee=True,
            )
            total += premiums.total
    return total


def _pension_start_age(pension_input: Any) -> int:
    """繰上げ・繰下げを反映した受給開始年齢を返す."""
    return (
        pension_input.start_age
        - (pension_input.months_early // 12)
        + (pension_input.months_deferred // 12)
    )


def _ideco_annuity_receiving(ideco: Any, age: int) -> bool:
    """指定年齢がiDeCo年金の受取期間内かを返す."""
    if age < ideco.receive_start_age:
        return False
    return (
        ideco.annuity_years is None
        or age < ideco.receive_start_age + ideco.annuity_years
    )


def _period_overlap_years(a: tuple[int, int], b: tuple[int, int]) -> int:
    """年齢ベースの期間(開始, 終了)同士の重複年数を返す."""
    return max(0, min(a[1], b[1]) - max(a[0], b[0]))


def _ideco_age_period(ideco: Any, total_years: int) -> tuple[int, int]:
    """iDeCo加入期間の年齢レンジ(近似)を返す.

    受取開始(または拠出終了)年齢を終端とし、加入年数分だけ遡る。
    """
    end = min(ideco.end_age, ideco.receive_start_age)
    return (max(0, end - total_years), end)


def _ideco_estimated_years(ideco: Any) -> int:
    """iDeCoの加入年数の年齢ベース推定(拠出実績を参照できない場合用)."""
    sim_years = (
        max(0, min(ideco.end_age, ideco.receive_start_age) - ideco.start_age)
        if ideco.monthly_contribution > 0
        else 0
    )
    return ideco.prior_contribution_years + sim_years


def _ideco_lump_sum_dedup_overlap(
    household: Household, ideco: Any, member: Member, contribution_years: int
) -> int:
    """iDeCo一時金の退職所得控除の重複年数(19年ルール簡易版)を返す.

    同一メンバーの退職金受取が「前年以前19年内」(年齢差1〜19)にある場合、
    勤続期間とiDeCo加入期間の重複年数を返す。同一年受取の合算計算は行わない。
    """
    overlap = 0
    for income in household.incomes:
        if (
            income.member_id != member.id
            or income.retirement_age is None
            or income.retirement_allowance <= 0
        ):
            continue
        gap = ideco.receive_start_age - income.retirement_age
        if not 1 <= gap <= 19:
            continue
        overlap = max(
            overlap,
            _period_overlap_years(
                (income.start_age, income.retirement_age),
                _ideco_age_period(ideco, contribution_years),
            ),
        )
    return overlap


def _retirement_allowance_dedup_overlap(
    household: Household, income: Income, member: Member
) -> int:
    """退職金の退職所得控除の重複年数(5年ルール簡易版)を返す.

    同一メンバーのiDeCo一時金受取が「前年以前4年内」(年齢差1〜4)にある場合、
    勤続期間とiDeCo加入期間の重複年数を返す。
    """
    if income.retirement_age is None:
        return 0
    overlap = 0
    for ideco in household.ideco_plans:
        if (
            ideco.member_id != member.id
            or ideco.receive_type not in ("一時金", "一時金+年金")
        ):
            continue
        gap = income.retirement_age - ideco.receive_start_age
        if not 1 <= gap <= 4:
            continue
        overlap = max(
            overlap,
            _period_overlap_years(
                (income.start_age, income.retirement_age),
                _ideco_age_period(ideco, _ideco_estimated_years(ideco)),
            ),
        )
    return overlap


def _annual_pension_taxable_estimate(
    store: ParameterStore,
    household: Household,
    year: int,
    member_alive: MemberAlive,
) -> tuple[int, dict[str, dict[str, int]]]:
    """公的年金等(老齢年金+iDeCo年金受取)に係る雑所得の年間推定額を返す.

    メンバーごとに年間の公的年金等収入を推定し、公的年金等控除を適用する。
    iDeCoの年金受取は残高不足による減額を考慮しない簡略推定。

    Returns:
        (世帯合計の雑所得, メンバー名ごとの根拠 {収入, 控除, 雑所得})
    """
    year_end = datetime.date(year, 12, 31)
    total_misc = 0
    basis: dict[str, dict[str, int]] = {}
    for member in household.members:
        pension_income = 0
        for pension_input in household.pension_records:
            if pension_input.member_id != member.id:
                continue
            start_age = _pension_start_age(pension_input)
            record = PensionRecord(
                kokumin_months=pension_input.kokumin_months,
                kousei_months=pension_input.kousei_months,
                avg_standard_remuneration=pension_input.avg_standard_remuneration,
                kousei_months_before_2003_04=pension_input.kousei_months_before_2003_04,
                kousei_months_after_2003_04=pension_input.kousei_months_after_2003_04,
            )
            annual = total_pension(
                store,
                year_end,
                record,
                pension_input.months_early,
                pension_input.months_deferred,
            )
            monthly = annual // 12
            months = sum(
                1
                for month in range(1, 13)
                if member_alive(member, datetime.date(year, month, 1))
                and age_at(member.birth_date, datetime.date(year, month, 1)) >= start_age
            )
            pension_income += monthly * months
        for ideco in household.ideco_plans:
            if (
                ideco.member_id != member.id
                or ideco.receive_type not in ("年金", "一時金+年金")
                or ideco.monthly_withdrawal <= 0
            ):
                continue
            months = sum(
                1
                for month in range(1, 13)
                if member_alive(member, datetime.date(year, month, 1))
                and _ideco_annuity_receiving(
                    ideco, age_at(member.birth_date, datetime.date(year, month, 1))
                )
            )
            pension_income += ideco.monthly_withdrawal * months
        if pension_income <= 0:
            continue
        age = age_at_year_end(member.birth_date, year)
        deduction = public_pension_deduction(store, year_end, age, pension_income)
        misc = pension_miscellaneous_income(store, year_end, age, pension_income)
        total_misc += misc
        basis[member.name] = {
            "公的年金等収入": pension_income,
            "公的年金等控除": deduction,
            "雑所得": misc,
        }
    return total_misc, basis


def _annual_ideco_deduction_estimate(
    store: ParameterStore,
    household: Household,
    year: int,
    member_alive: MemberAlive,
) -> int:
    """iDeCo掛金の年間所得控除(小規模企業共済等掛金控除)の推定額.

    受給開始(一時金・年金受取)以降の月は拠出停止として数えない。
    """
    year_end = datetime.date(year, 12, 31)
    total = 0
    for ideco in household.ideco_plans:
        member = next((m for m in household.members if m.id == ideco.member_id), None)
        if member is None or ideco.monthly_contribution <= 0:
            continue
        monthly = min(
            ideco.monthly_contribution,
            ideco_contribution_limit(store, year_end, ideco.subscriber_type),
        )
        receives = ideco.receive_type in ("一時金", "一時金+年金") or (
            ideco.monthly_withdrawal > 0
        )
        months = 0
        for month in range(1, 13):
            first = datetime.date(year, month, 1)
            if not member_alive(member, first):
                continue
            age = age_at(member.birth_date, first)
            if not ideco.start_age <= age < ideco.end_age:
                continue
            if receives and age >= ideco.receive_start_age:
                continue
            months += 1
        total += monthly * months
    return total


def _ideco_annuity_active(
    household: Household, current: datetime.date, member_alive: MemberAlive
) -> bool:
    """当月にiDeCoの年金受取が発生しうるかを返す."""
    for ideco in household.ideco_plans:
        if ideco.receive_type not in ("年金", "一時金+年金") or ideco.monthly_withdrawal <= 0:
            continue
        member = next((m for m in household.members if m.id == ideco.member_id), None)
        if member is None or not member_alive(member, current):
            continue
        if _ideco_annuity_receiving(ideco, age_at(member.birth_date, current)):
            return True
    return False


@dataclass
class AnnualTaxEstimateCache:
    """年単位の税務推定のキャッシュ(1回のsimulate内で有効).

    公的年金等雑所得とiDeCo掛金控除の年次推定は年内で不変のため、
    月次ループからの再計算を避ける。
    """

    pension_misc: dict[int, tuple[int, dict[str, dict[str, int]]]] = field(
        default_factory=dict
    )
    ideco_deduction: dict[int, int] = field(default_factory=dict)

    def pension_taxable(
        self,
        store: ParameterStore,
        household: Household,
        year: int,
        member_alive: MemberAlive,
    ) -> tuple[int, dict[str, dict[str, int]]]:
        if year not in self.pension_misc:
            self.pension_misc[year] = _annual_pension_taxable_estimate(
                store, household, year, member_alive
            )
        return self.pension_misc[year]

    def ideco_annual_deduction(
        self,
        store: ParameterStore,
        household: Household,
        year: int,
        member_alive: MemberAlive,
    ) -> int:
        if year not in self.ideco_deduction:
            self.ideco_deduction[year] = _annual_ideco_deduction_estimate(
                store, household, year, member_alive
            )
        return self.ideco_deduction[year]


def _apply_leave_benefits(
    store: ParameterStore,
    household: Household,
    income: Income,
    member: Member,
    current: datetime.date,
    compensation: IncomeMonthCompensation,
    cf: MonthlyCashflow,
) -> None:
    """産休・育休の給付金を月次CFへ記録する."""
    if income.social_insurance_type not in (
        SocialInsuranceType.KYOSAI_KOSEI,
        SocialInsuranceType.YAKUIN_KOSEI,
    ):
        return

    standard = standard_remuneration(compensation.base_salary)
    if standard <= 0:
        return

    leave_days = compensation.leave_days
    for leave in household.childcare_leaves:
        if leave.member_id != member.id:
            continue
        if leave.income_id is not None and leave.income_id != income.id:
            continue
        periods = leave_periods(leave)
        benefit_periods = [
            period
            for period in periods
            if period.leave_type in {"産後パパ育休", "育児休業"}
        ]
        benefit_start = min((period.start for period in benefit_periods), default=None)
        for period in periods:
            dates = [
                date
                for date in leave_days.get(period.leave_type, [])
                if period.start <= date <= period.end
            ]
            if not dates:
                continue
            if period.leave_type == "産前産後休業":
                amount = maternity_allowance_for_days(
                    store,
                    current,
                    standard,
                    len(dates),
                    compensation.days_in_month,
                )
                cf.maternity_allowance += amount
                trace_item = "出産手当金"
                basis = {
                    "休業種別": period.leave_type,
                    "日割り": True,
                }
            else:
                if benefit_start is None:
                    continue
                amount = childcare_benefit_for_days(
                    store,
                    standard,
                    benefit_start,
                    dates,
                )
                target = (
                    "paternity_leave_benefit"
                    if period.leave_type == "産後パパ育休"
                    else "childcare_benefit"
                )
                setattr(cf, target, getattr(cf, target) + amount)
                trace_item = (
                    "産後パパ育休給付金"
                    if period.leave_type == "産後パパ育休"
                    else "育児休業給付金"
                )
                basis = {
                    "休業種別": period.leave_type,
                    "給付判定開始日": benefit_start.isoformat(),
                    "日割り": True,
                }
            cf.traces.append(
                TraceEntry(
                    trace_item,
                    amount,
                    {
                        "member": member.name,
                        "income": income.name,
                        **basis,
                        "標準報酬月額": standard,
                        "対象日数": len(dates),
                        "月日数": compensation.days_in_month,
                    },
                )
            )


def _apply_work_income(
    store: ParameterStore,
    household: Household,
    current: datetime.date,
    year: int,
    month: int,
    assumptions: PlanAssumptions,
    cf: MonthlyCashflow,
    member_alive: MemberAlive,
) -> int:
    """勤労収入・社会保険料・退職金を月次CFへ反映する."""
    monthly_salary_total = 0
    for income in household.incomes:
        member = next(
            (m for m in household.members if m.id == income.member_id), None
        )
        if member is None or not member_alive(member, current):
            continue
        member_age = age_at(member.birth_date, current)
        compensation = _income_month_compensation(
            household, income, member, current, assumptions
        )
        if compensation is not None:
            monthly_salary_total += compensation.salary + compensation.bonus
            _apply_leave_benefits(
                store,
                household,
                income,
                member,
                current,
                compensation,
                cf,
            )

            if income.social_insurance_type in (
                SocialInsuranceType.KYOSAI_KOSEI,
                SocialInsuranceType.YAKUIN_KOSEI,
            ):
                if leave_includes_month_end(
                    household.childcare_leaves,
                    income.id,
                    member.id,
                    year,
                    month,
                ):
                    cf.traces.append(
                        TraceEntry(
                            "社会保険料免除",
                            0,
                            {
                                "member": member.name,
                                "income": income.name,
                                "判定": "休業期間が月末を含む",
                            },
                        )
                    )
                else:
                    si = monthly_social_insurance(
                        store,
                        current,
                        compensation.base_salary,
                        member.prefecture,
                        member_age,
                        is_employee=True,
                    )
                    cf.social_insurance += si.total
                    cf.traces.append(
                        TraceEntry(
                            "社会保険料",
                            si.total,
                            {
                                "member": member.name,
                                "標準報酬月額": compensation.base_salary,
                                "厚生年金": si.pension,
                                "健康保険": si.health,
                                "介護保険": si.nursing,
                                "雇用保険": si.employment,
                            },
                        )
                    )

        if (
            income.retirement_age is not None
            and member_age == income.retirement_age
            and month == member.birth_date.month
            and income.retirement_allowance > 0
        ):
            years_of_service = income.retirement_age - income.start_age
            # 5年ルール(簡易): 前年以前4年内のiDeCo一時金と重複する勤続年数を控除年数から除く
            dedup_overlap = _retirement_allowance_dedup_overlap(household, income, member)
            adjusted_years = max(0, years_of_service - dedup_overlap)
            net = net_retirement_allowance(
                store, current, income.retirement_allowance, adjusted_years
            )
            cf.retirement_income += net
            basis: dict[str, TraceValue] = {
                "member": member.name,
                "額面": income.retirement_allowance,
                "勤続年数": years_of_service,
            }
            if dedup_overlap > 0:
                basis["重複調整(5年ルール簡易)"] = dedup_overlap
                basis["調整後控除年数"] = adjusted_years
            cf.traces.append(TraceEntry("退職金(手取り)", net, basis))

    cf.salary_income = monthly_salary_total
    return monthly_salary_total


def _apply_pension_and_disaster_income(
    store: ParameterStore,
    household: Household,
    current: datetime.date,
    scenario: DisasterScenario | None,
    death_date: datetime.date | None,
    cf: MonthlyCashflow,
    member_alive: MemberAlive,
) -> None:
    """年金と万が一シナリオの追加収入を月次CFへ反映する."""
    for pension_input in household.pension_records:
        member = next(
            (m for m in household.members if m.id == pension_input.member_id),
            None,
        )
        if member is None or not member_alive(member, current):
            continue
        member_age = age_at(member.birth_date, current)
        pension_start_age = (
            pension_input.start_age
            - (pension_input.months_early // 12)
            + (pension_input.months_deferred // 12)
        )
        if member_age < pension_start_age:
            continue

        record = PensionRecord(
            kokumin_months=pension_input.kokumin_months,
            kousei_months=pension_input.kousei_months,
            avg_standard_remuneration=pension_input.avg_standard_remuneration,
            kousei_months_before_2003_04=pension_input.kousei_months_before_2003_04,
            kousei_months_after_2003_04=pension_input.kousei_months_after_2003_04,
        )
        annual = total_pension(
            store,
            current,
            record,
            pension_input.months_early,
            pension_input.months_deferred,
        )
        monthly_pension = annual // 12
        cf.pension_income += monthly_pension
        cf.traces.append(
            TraceEntry(
                "年金収入",
                monthly_pension,
                {"member": member.name, "年額": annual},
            )
        )

    disaster_active = scenario is not None and death_date is not None and current >= death_date
    if disaster_active and scenario is not None:
        if scenario.survivor_pension_monthly is not None:
            if scenario.survivor_pension_monthly > 0:
                cf.survivor_pension = scenario.survivor_pension_monthly
                cf.traces.append(
                    TraceEntry(
                        "遺族年金",
                        cf.survivor_pension,
                        {
                            "自動計算": False,
                            "月額": scenario.survivor_pension_monthly,
                            "scenario": scenario.name,
                        },
                    )
                )
        else:
            deceased = next(
                (
                    member
                    for member in household.members
                    if member.id == scenario.deceased_member_id
                ),
                None,
            )
            if deceased is not None:
                allowance, basis = _automatic_survivor_pension(
                    store, household, deceased, current, member_alive
                )
                cf.survivor_pension = allowance
                cf.traces.append(TraceEntry("遺族年金", allowance, basis))

    if (
        disaster_active
        and scenario is not None
        and scenario.child_allowance_monthly is not None
    ):
        eligible_child = any(
            member.relationship == Relationship.CHILD
            and member_alive(member, current)
            and age_at(member.birth_date, current) < scenario.child_allowance_end_age
            for member in household.members
        )
        if eligible_child and scenario.child_allowance_monthly > 0:
            cf.child_allowance = scenario.child_allowance_monthly
            cf.traces.append(
                TraceEntry(
                    "児童手当",
                    cf.child_allowance,
                    {
                        "自動計算": False,
                        "月額": scenario.child_allowance_monthly,
                        "対象年齢未満": scenario.child_allowance_end_age,
                        "scenario": scenario.name,
                    },
                )
            )
        return

    allowance, basis = _automatic_child_allowance(store, household, current, member_alive)
    if allowance > 0:
        cf.child_allowance = allowance
        cf.traces.append(TraceEntry("児童手当", allowance, basis))


def _apply_income_tax(
    store: ParameterStore,
    household: Household,
    current: datetime.date,
    year: int,
    assumptions: PlanAssumptions,
    monthly_salary_total: int,
    cf: MonthlyCashflow,
    member_alive: MemberAlive,
    estimate_cache: AnnualTaxEstimateCache,
) -> None:
    """所得税の源泉徴収・年末調整を月次CFへ反映する.

    給与所得に加え、公的年金等(老齢年金・iDeCo年金受取)の雑所得を合算して課税する。
    iDeCo掛金は小規模企業共済等掛金控除として全額所得控除する。
    """
    pension_receiving_now = cf.pension_income > 0 or _ideco_annuity_active(
        household, current, member_alive
    )
    if monthly_salary_total <= 0 and not pension_receiving_now:
        return

    active_income_now = any(
        (member := next((m for m in household.members if m.id == income.member_id), None))
        is not None
        and member_alive(member, current)
        and income_is_active(income, member, current)
        for income in household.incomes
    )
    est_annual = (
        _annual_salary_estimate(
            household,
            year,
            assumptions,
            member_alive,
            reference_date=current,
        )
        if active_income_now
        else 0
    )
    est_pension_misc, pension_misc_basis = estimate_cache.pension_taxable(
        store, household, year, member_alive
    )

    spouse_income = 0  # MVP: 配偶者収入は0扱い(将来拡張)
    spouse_ded, dep_ded = calc_deductions_for_household(
        store, year, household, est_annual, spouse_income
    )
    deductions = Deductions(
        basic=store.get("所得税.基礎控除.控除額", datetime.date(year, 12, 31)),
        social_insurance=_annual_social_insurance_estimate(
            store,
            household,
            year,
            assumptions,
            member_alive,
            reference_date=current,
        ),
        spouse=spouse_ded,
        dependent=dep_ded,
        mutual_aid=estimate_cache.ideco_annual_deduction(
            store, household, year, member_alive
        ),
    )
    income_after = salary_income_after_deduction(
        store, datetime.date(year, 12, 31), est_annual
    )
    # 総所得金額 = 給与所得 + 公的年金等に係る雑所得
    annual_tax = calc_annual_income_tax(
        store, datetime.date(year, 12, 31), income_after + est_pension_misc, deductions
    )

    # 年金のみで公的年金等控除等の範囲に収まる(税額0)の場合はトレースを出さない
    if monthly_salary_total <= 0 and annual_tax <= 0:
        return

    if current.month < 12:
        monthly_tax = annual_tax // 12
        cf.income_tax = monthly_tax
        cf.traces.append(
            TraceEntry(
                "所得税(源泉徴収)",
                monthly_tax,
                {
                    "推定年収": est_annual,
                    "公的年金等雑所得": est_pension_misc,
                    "公的年金等の内訳": pension_misc_basis,
                    "年間推定税額": annual_tax,
                    "年間社会保険料控除": deductions.social_insurance,
                    "小規模企業共済等掛金控除": deductions.mutual_aid,
                },
            )
        )
    else:
        withheld_so_far = (annual_tax // 12) * 11
        adjustment = annual_tax - withheld_so_far
        cf.income_tax = adjustment
        cf.traces.append(
            TraceEntry(
                "所得税(年末調整)",
                adjustment,
                {
                    "年間確定税額": annual_tax,
                    "1-11月徴収済": withheld_so_far,
                    "公的年金等雑所得": est_pension_misc,
                    "公的年金等の内訳": pension_misc_basis,
                    "年間社会保険料控除": deductions.social_insurance,
                    "小規模企業共済等掛金控除": deductions.mutual_aid,
                },
            )
        )


def _apply_resident_tax(
    store: ParameterStore,
    household: Household,
    current: datetime.date,
    year: int,
    assumptions: PlanAssumptions,
    resident_tax_cache: dict[int, dict[datetime.date, int]],
    cf: MonthlyCashflow,
    member_alive: MemberAlive,
    estimate_cache: AnnualTaxEstimateCache,
) -> None:
    """前年所得課税の住民税を月次CFへ反映する.

    給与所得に加え、公的年金等の雑所得を前年所得へ含める。
    """
    if year not in resident_tax_cache:
        prev_year = year - 1
        prev_est_annual = _annual_salary_estimate(
            household,
            prev_year,
            assumptions,
            member_alive,
        )
        prev_pension_misc, _ = estimate_cache.pension_taxable(
            store, household, prev_year, member_alive
        )

        if prev_est_annual > 0 or prev_pension_misc > 0:
            prev_si = _annual_social_insurance_estimate(
                store,
                household,
                prev_year,
                assumptions,
                member_alive,
            )

            prev_deductions = Deductions(
                basic=store.get(
                    "所得税.基礎控除.控除額",
                    datetime.date(prev_year, 12, 31),
                ),
                social_insurance=prev_si,
                spouse=0,
                dependent=0,
                mutual_aid=estimate_cache.ideco_annual_deduction(
                    store, household, prev_year, member_alive
                ),
            )
            prev_income_after = salary_income_after_deduction(
                store,
                datetime.date(prev_year, 12, 31),
                prev_est_annual,
            )
            prev_taxable = calc_taxable_income(
                store,
                datetime.date(prev_year, 12, 31),
                prev_income_after + prev_pension_misc,
                prev_deductions,
            )
            resident_tax_cache[year] = monthly_resident_tax_schedule(
                store, prev_year, prev_taxable, prev_deductions
            )
        else:
            resident_tax_cache[year] = {}

    cf.resident_tax = resident_tax_cache[year].get(current, 0)
    if cf.resident_tax > 0:
        cf.traces.append(
            TraceEntry("住民税", cf.resident_tax, {"前年所得課税": True})
        )


def _apply_education_expenses(
    store: ParameterStore,
    household: Household,
    current: datetime.date,
    cf: MonthlyCashflow,
    member_alive: MemberAlive,
) -> None:
    """教育費を月次CFへ反映する."""
    for education_plan in household.education_plans:
        member = next(
            (m for m in household.members if m.id == education_plan.member_id),
            None,
        )
        if member is None or not member_alive(member, current):
            continue
        child_age = age_at(member.birth_date, current)
        breakdown = education_cost_breakdown(
            store,
            current,
            child_age,
            education_plan,
            base_year=household.assumptions.base_year,
        )
        monthly_cost = breakdown.total
        cf.education_expense += monthly_cost
        if monthly_cost > 0:
            cf.traces.append(
                TraceEntry(
                    "教育費",
                    monthly_cost,
                    {
                        "child": member.name,
                        "member_id": member.id,
                        "schools": breakdown.schools,
                        "学校関連": breakdown.school_cost,
                        "塾・習い事": breakdown.lessons_cost,
                        "入学金": breakdown.admission_fee,
                        "教材費": breakdown.material_cost,
                        "通学費": breakdown.transport_cost,
                        "通学・生活": breakdown.living_cost,
                        "支援・奨学金": breakdown.support,
                        "教育費上昇率": education_plan.education_raise_rate,
                    },
                )
            )


def _apply_investments(
    store: ParameterStore,
    household: Household,
    current: datetime.date,
    cf: MonthlyCashflow,
    ideco_accounts: dict[str, IdecoAccount],
    nisa_accounts: dict[str, NisaAccount],
    member_alive: MemberAlive,
) -> None:
    """iDeCo・NISAの積立・運用・取崩を月次CFへ反映する.

    iDeCoの受取:
    - 一時金: 受取開始年齢の到達月に残高(×一時金割合)を退職所得として分離課税
    - 年金: 受取月額を計上し、公的年金等の雑所得として所得税・住民税側で課税
    NISAの取崩は非課税。売却分の簿価は翌年に生涯非課税枠へ復活する。
    """
    for ideco in household.ideco_plans:
        member = next(
            (m for m in household.members if m.id == ideco.member_id), None
        )
        if member is None or not member_alive(member, current):
            continue
        member_age = age_at(member.birth_date, current)
        previous = ideco_accounts[ideco.id]
        # 受給開始後(一時金受取済み・年金受取開始後)は脱退扱いで拠出しない
        receipt_started = member_age >= ideco.receive_start_age and (
            ideco.receive_type in ("一時金", "一時金+年金")
            or ideco.monthly_withdrawal > 0
        )
        in_contribution_window = (
            ideco.start_age <= member_age < ideco.end_age
            and not previous.lump_sum_paid
            and not receipt_started
        )
        updated = ideco_monthly_step(
            store,
            current,
            previous,
            ideco.monthly_contribution if in_contribution_window else 0,
            ideco.subscriber_type,
            ideco.annual_return_rate,
        )
        contribution = updated.total_contributions - previous.total_contributions
        cf.ideco_contribution += contribution
        if contribution > 0:
            cf.traces.append(
                TraceEntry(
                    "iDeCo掛金",
                    contribution,
                    {
                        "member": member.name,
                        "上限": ideco_contribution_limit(
                            store, current, ideco.subscriber_type
                        ),
                        "note": "全額所得控除(小規模企業共済等掛金控除)",
                    },
                )
            )

        # 一時金受取(退職所得として分離課税)
        if (
            not updated.lump_sum_paid
            and ideco.receive_type in ("一時金", "一時金+年金")
            and member_age >= ideco.receive_start_age
        ):
            ratio = 1.0 if ideco.receive_type == "一時金" else ideco.lump_sum_ratio
            lump_sum = int(updated.balance * ratio)
            updated = replace(
                updated,
                balance=updated.balance - lump_sum,
                lump_sum_paid=True,
            )
            if lump_sum > 0:
                years = ideco_contribution_years(updated, ideco.prior_contribution_years)
                # 19年ルール(簡易): 前年以前19年内の退職金と重複する加入年数を控除年数から除く
                dedup_overlap = _ideco_lump_sum_dedup_overlap(
                    household, ideco, member, years
                )
                adjusted_years = max(0, years - dedup_overlap)
                tax = retirement_tax(store, current, lump_sum, adjusted_years)
                cf.ideco_withdrawal += lump_sum
                cf.ideco_withdrawal_tax += tax.total
                lump_basis: dict[str, TraceValue] = {
                    "member": member.name,
                    "加入年数": years,
                    "退職所得控除": retirement_income_deduction(adjusted_years),
                    "課税退職所得": retirement_income(
                        store, current, lump_sum, adjusted_years
                    ),
                    "note": "退職所得として分離課税(退職金との控除重複調整は年単位の簡易近似)",
                }
                if dedup_overlap > 0:
                    lump_basis["重複調整(19年ルール簡易)"] = dedup_overlap
                    lump_basis["調整後控除年数"] = adjusted_years
                cf.traces.append(TraceEntry("iDeCo一時金受取", lump_sum, lump_basis))
                if tax.total > 0:
                    cf.traces.append(
                        TraceEntry(
                            "iDeCo受取時税",
                            tax.total,
                            {
                                "所得税": tax.income_tax,
                                "住民税": tax.resident_tax,
                                "課税方式": "退職所得(分離課税)",
                            },
                        )
                    )

        # 年金受取(公的年金等の雑所得として総合課税側で処理)
        if (
            ideco.receive_type in ("年金", "一時金+年金")
            and ideco.monthly_withdrawal > 0
            and _ideco_annuity_receiving(ideco, member_age)
        ):
            withdrawal = withdrawal_amount(
                updated.balance, ideco.monthly_withdrawal
            )
            updated = replace(updated, balance=updated.balance - withdrawal)
            cf.ideco_withdrawal += withdrawal
            if withdrawal > 0:
                cf.traces.append(
                    TraceEntry(
                        "iDeCo年金受取",
                        withdrawal,
                        {
                            "member": member.name,
                            "月額": ideco.monthly_withdrawal,
                            "受取期間(年)": ideco.annuity_years or "残高が尽きるまで",
                            "note": "公的年金等の雑所得として所得税・住民税へ合算",
                        },
                    )
                )
        ideco_accounts[ideco.id] = updated

    for nisa in household.nisa_plans:
        member = next(
            (m for m in household.members if m.id == nisa.member_id), None
        )
        if member is None or not member_alive(member, current):
            continue
        member_age = age_at(member.birth_date, current)
        in_contribution_window = member_age >= nisa.start_age and (
            nisa.end_age is None or member_age <= nisa.end_age
        )
        previous = nisa_accounts[nisa.id]
        updated = nisa_monthly_step(
            store,
            current,
            previous,
            nisa.monthly_investment if in_contribution_window else 0,
            nisa.growth_monthly_investment if in_contribution_window else 0,
            nisa.annual_return_rate,
        )
        investment = updated.total_invested - previous.total_invested
        cf.nisa_investment += investment
        if investment > 0:
            cf.traces.append(
                TraceEntry(
                    "NISA投資",
                    investment,
                    {
                        "member": member.name,
                        "つみたて枠投資": updated.year_invested_tsumitate
                        - previous.year_invested_tsumitate,
                        "成長枠投資": updated.year_invested_growth
                        - previous.year_invested_growth,
                        "年間上限": nisa_annual_limit(store, current),
                        "生涯枠消費": updated.total_invested,
                        "note": "運用益非課税。つみたて枠超過分は成長投資枠へ振替",
                    },
                )
            )
        if (
            nisa.receive_start_age is not None
            and member_age >= nisa.receive_start_age
            and nisa.monthly_withdrawal > 0
        ):
            updated, withdrawal = nisa_withdraw(updated, nisa.monthly_withdrawal)
            cf.nisa_withdrawal += withdrawal
            if withdrawal > 0:
                cf.traces.append(
                    TraceEntry(
                        "NISA取崩",
                        withdrawal,
                        {
                            "member": member.name,
                            "月額": nisa.monthly_withdrawal,
                            "税": "非課税",
                            "翌年復活する簿価": updated.pending_restore_tsumitate
                            + updated.pending_restore_growth,
                        },
                    )
                )
        nisa_accounts[nisa.id] = updated


def _member_age_for_account(
    household: Household, member_id: str | None, current: datetime.date
) -> int:
    member = next((item for item in household.members if item.id == member_id), None)
    return age_at(member.birth_date, current) if member else age_at(household.householder().birth_date, current)


def _apply_account_schedules(
    household: Household,
    current: datetime.date,
    age: int,
    base_year: int,
    balances: dict[str, int],
    scheduled_balances: dict[str, int],
    cf: MonthlyCashflow,
) -> int:
    """一般口座の積立と汎用スケジュールを適用する."""
    account_investment_balance = 0
    for account in household.accounts:
        account_age = _member_age_for_account(household, account.member_id, current)
        balance = balances[account.id]
        active_schedule_rates = [
            investment.annual_return_rate
            for investment in account.investment_schedules
            if investment.annual_return_rate is not None
            and investment_schedule_amount(
                investment, current, account_age, base_year=base_year
            )
            > 0
        ]
        effective_rate = max([account.interest_rate, *active_schedule_rates])
        if effective_rate:
            balance = int(balance * (1 + effective_rate / 12))
        for investment in account.investment_schedules:
            contribution = investment_schedule_amount(
                investment, current, account_age, base_year=base_year
            )
            if contribution <= 0:
                continue
            balance += contribution
            cf.account_investment += contribution
            cf.traces.append(
                TraceEntry(
                    "一般口座積立",
                    contribution,
                    {"account": account.name, "年齢": account_age},
                )
            )
        balances[account.id] = balance
        account_investment_balance += max(0, balance - account.balance)

    for schedule in household.schedules:
        schedule_age = (
            _member_age_for_account(household, schedule.member_id, current)
            if schedule.member_id
            else age
        )
        amount = schedule_amount(schedule, current, schedule_age, base_year=base_year)
        if amount <= 0:
            continue
        if schedule.kind == "income":
            cf.scheduled_income += amount
            cf.traces.append(TraceEntry("汎用スケジュール収入", amount, {"name": schedule.name}))
        elif schedule.kind == "expense":
            cf.scheduled_expense += amount
            cf.traces.append(TraceEntry("汎用スケジュール支出", amount, {"name": schedule.name}))
        else:
            cf.account_investment += amount
            if schedule.account_id in balances:
                balances[schedule.account_id] += amount
                account_investment_balance += amount
            else:
                scheduled_balances[schedule.id] = (
                    scheduled_balances.get(schedule.id, 0) + amount
                )
            cf.traces.append(
                TraceEntry(
                    "汎用積立",
                    amount,
                    {"name": schedule.name, "account_id": schedule.account_id},
                )
            )
    cf.scheduled_investment_balance = sum(scheduled_balances.values())
    return account_investment_balance


def _apply_expenses(
    household: Household,
    current: datetime.date,
    year: int,
    month: int,
    age: int,
    assumptions: PlanAssumptions,
    householder: Member,
    scenario: DisasterScenario | None,
    death_date: datetime.date | None,
    cf: MonthlyCashflow,
    member_alive: MemberAlive,
) -> None:
    """生活費・ライフイベントを月次CFへ反映する."""
    for expense in household.expenses:
        target_member = None
        if expense.member_id is not None:
            target_member = next(
                (m for m in household.members if m.id == expense.member_id), None
            )
            if target_member is None:
                continue
            if not member_alive(target_member, current):
                continue
            if expense.start_date is None:
                member_age = age_at(target_member.birth_date, current)
                if member_age < expense.start_age:
                    continue
                if expense.end_age is not None and member_age > expense.end_age:
                    continue
        else:
            # 世帯全体の支出: 世帯主年齢で判定。
            if expense.start_date is None:
                # start_age=0 は「基準年開始」を意味するため、年齢0以上は常に対象
                if expense.start_age > 0 and age < expense.start_age:
                    continue
                if (
                    expense.end_age is not None
                    and expense.end_age > 0
                    and age > expense.end_age
                ):
                    continue
        if expense.start_date and current < expense.start_date:
            continue
        if expense.end_date and current > expense.end_date:
            continue

        years_elapsed = year - assumptions.base_year
        raise_start_year = (
            expense.start_date.year
            if expense.start_date
            else assumptions.base_year
        )
        raise_factor = (1 + expense.annual_raise_rate) ** max(
            0, year - raise_start_year
        )
        inflation_factor = (1 + assumptions.inflation_rate) ** years_elapsed

        recurring_amount = int(
            expense.monthly_amount * raise_factor * inflation_factor
        )
        once_amount = int(expense.monthly_amount * inflation_factor)
        if (
            scenario
            and death_date
            and current >= death_date
            and expense.disaster_amount is not None
        ):
            recurring_amount = int(expense.disaster_amount * inflation_factor)
            once_amount = recurring_amount

        if expense.cycle == "monthly" and expense.event_type == "生活費":
            cf.living_expense += recurring_amount
        elif expense.cycle == "monthly" or (
            expense.cycle == "yearly" and month == expense.yearly_month
        ):
            _record_event_expense(cf, recurring_amount, expense)
        elif expense.cycle == "once":
            # 開始年月の1回のみ。対象者指定時は対象者の年齢を基準にする
            anchor = target_member if target_member is not None else householder
            event_date = expense.start_date or datetime.date(
                assumptions.base_year
                if expense.start_age == 0
                else anchor.birth_date.year + expense.start_age,
                expense.start_month,
                1,
            )
            if current == event_date:
                _record_event_expense(cf, once_amount, expense)


def _apply_insurance(
    household: Household,
    current: datetime.date,
    cf: MonthlyCashflow,
    scenario: DisasterScenario | None,
    death_date: datetime.date | None,
    deceased: Member | None,
    member_alive: MemberAlive,
) -> None:
    """保険料と万が一時の死亡保険金を月次CFへ反映する."""
    for ins in household.insurances:
        policy = InsurancePolicy(
            name=ins.name,
            insured_member_id=ins.insured_member_id,
            payer_member_id=ins.payer_member_id,
            monthly_premium=ins.monthly_premium,
            start_date=datetime.date(ins.start_year, ins.start_month, 1),
            end_date=datetime.date(ins.end_year, ins.end_month, 1),
            death_benefit=ins.death_benefit,
            surrender_value_rate=ins.surrender_value_rate,
            insurance_type=ins.insurance_type,
            payment_frequency=ins.payment_frequency,
            payment_month=ins.payment_month,
            payment_interval_years=ins.payment_interval_years,
            payment_amount=ins.payment_amount,
        )
        payer = next(
            (m for m in household.members if m.id == ins.payer_member_id), None
        )
        insured = next(
            (m for m in household.members if m.id == ins.insured_member_id), None
        )
        # 被保険者の死亡で契約は消滅するため、以後の保険料は計上しない
        insured_alive = insured is None or member_alive(insured, current)
        payer_alive = payer is None or member_alive(payer, current)
        premium = 0
        if insured_alive and payer_alive and policy.start_date <= current <= policy.end_date:
            premium = insurance_payment_amount(
                monthly_premium=ins.monthly_premium,
                payment_frequency=ins.payment_frequency,
                payment_amount=ins.payment_amount,
                payment_month=ins.payment_month,
                payment_interval_years=ins.payment_interval_years,
                current=current,
                start_year=ins.start_year,
                start_month=ins.start_month,
            )
        cf.insurance_premium += premium
        if premium > 0:
            cf.traces.append(
                TraceEntry(
                    "保険料",
                    premium,
                    {"name": ins.name, "type": ins.insurance_type},
                )
            )

        if (
            death_date
            and insured
            and deceased
            and insured.id == deceased.id
            and current == death_date
        ):
            # 保障期間内の死亡のみ保険金を支払う
            benefit = death_benefit_if_died(policy, death_date)
            cf.death_benefit += benefit
            if benefit > 0 and scenario is not None:
                cf.traces.append(
                    TraceEntry(
                        "死亡保険金",
                        benefit,
                        {"name": ins.name, "scenario": scenario.name},
                    )
                )


def _apply_housing(
    housing: OwnedHousingPlan | None,
    current: datetime.date,
    month: int,
    cf: MonthlyCashflow,
    base_year: int = 0,
) -> None:
    """所有住宅の頭金・固定資産税・修繕費を月次CFへ反映する."""
    if housing is None:
        return

    purchase_date = datetime.date(
        housing.purchase_year, housing.purchase_month, 1
    )
    if current < purchase_date:
        return
    if current == purchase_date and housing.down_payment > 0:
        cf.housing_down_payment = housing.down_payment
        cf.traces.append(
            TraceEntry(
                "住宅購入頭金",
                housing.down_payment,
                {
                    "物件価格": housing.property_price,
                    "購入年月": purchase_date.isoformat(),
                },
            )
        )
    if month == housing.purchase_month and housing.annual_property_tax > 0:
        cf.property_tax = housing.annual_property_tax
        cf.traces.append(
            TraceEntry(
                "固定資産税",
                housing.annual_property_tax,
                {"年額": housing.annual_property_tax},
            )
        )
    if month == housing.purchase_month and housing.annual_repair_cost > 0:
        cf.repair_expense = housing.annual_repair_cost
        cf.traces.append(
            TraceEntry(
                "住宅修繕費",
                housing.annual_repair_cost,
                {"年額": housing.annual_repair_cost},
            )
        )
    for schedule in housing.cost_schedules:
        amount = housing_schedule_amount(
            schedule,
            current,
            base_year=base_year or current.year,
        )
        if amount <= 0:
            continue
        if schedule.cost_type in {"固定資産税"}:
            cf.property_tax += amount
        else:
            cf.repair_expense += amount
        cf.traces.append(
            TraceEntry(
                f"住宅{schedule.cost_type}",
                amount,
                {
                    "支払周期": schedule.cycle,
                    "適用開始": f"{schedule.start_year}/{schedule.start_month}",
                    "上昇率": schedule.annual_raise_rate,
                },
            )
        )


def _vehicle_sale_price(
    vehicle,
    months_since_purchase: int,
    inflation_factor: float,
) -> int:
    """売却額を手入力・残価率・減価償却の設定から算出する."""
    if vehicle.sale_price_mode == "手入力":
        base_price = vehicle.sale_price
    elif vehicle.sale_price_mode == "残価率":
        base_price = vehicle.purchase_price * vehicle.residual_value_rate
    else:
        age_years = months_since_purchase / 12
        depreciation_years = vehicle.depreciation_years
        if vehicle.sale_price_mode == "定額法":
            remaining_rate = max(
                vehicle.residual_value_rate,
                1 - age_years / depreciation_years,
            )
        else:
            remaining_rate = max(
                vehicle.residual_value_rate,
                (1 - vehicle.declining_depreciation_rate) ** age_years,
            )
        base_price = vehicle.purchase_price * remaining_rate
    return int(base_price * inflation_factor)


def _automobile_tax_before_reduction(vehicle) -> tuple[int, str]:
    """自動車税の減税前年額を入力値または排気量区分から算出する."""
    if vehicle.annual_automobile_tax > 0:
        return vehicle.annual_automobile_tax, "手入力"
    if vehicle.vehicle_category == "軽自動車":
        return 10_800, "軽自動車（自動計算）"
    if vehicle.vehicle_category == "二輪車":
        displacement = vehicle.engine_displacement_cc
        if displacement <= 0:
            return 0, "未設定"
        motorcycle_tax_by_displacement = (
            (90, 2_000),
            (125, 2_400),
            (250, 3_600),
        )
        for maximum_cc, amount in motorcycle_tax_by_displacement:
            if displacement <= maximum_cc:
                return amount, "二輪車排気量区分（自動計算）"
        return 6_000, "二輪車排気量区分（自動計算）"
    displacement = vehicle.engine_displacement_cc
    if displacement <= 0 and vehicle.energy_type != "電気":
        return 0, "未設定"
    tax_by_displacement = (
        (1_000, 25_000),
        (1_500, 30_500),
        (2_000, 36_000),
        (2_500, 43_500),
        (3_000, 50_000),
        (3_500, 57_000),
        (4_000, 65_500),
        (4_500, 75_500),
        (6_000, 87_000),
    )
    for maximum_cc, amount in tax_by_displacement:
        if displacement <= maximum_cc:
            return amount, "排気量区分（自動計算）"
    return 110_000, "排気量区分（自動計算）"


def _weight_tax_before_reduction(
    vehicle, inspection_period_years: int | None = None
) -> tuple[int, str]:
    """重量税の車検期間分を入力値または車両区分・重量から算出する."""
    if vehicle.weight_tax_per_inspection > 0:
        return vehicle.weight_tax_per_inspection, "手入力"
    if vehicle.vehicle_category == "二輪車" or vehicle.vehicle_weight_kg <= 0:
        return 0, "未設定"
    period_years = inspection_period_years or vehicle.inspection_cycle_years
    if vehicle.vehicle_category == "軽自動車":
        amount = int(6_600 * period_years / 2)
    else:
        half_ton_units = (vehicle.vehicle_weight_kg + 499) // 500
        amount = int(half_ton_units * 8_200 * period_years / 2)
    return amount, "車両区分・重量区分（自動計算）"


def _apply_vehicle_expenses(
    household: Household,
    current: datetime.date,
    year: int,
    month: int,
    assumptions: PlanAssumptions,
    cf: MonthlyCashflow,
    vehicle_replacement_dates: dict[str, list[datetime.date]],
    vehicle_replacement_principal: dict[tuple[str, datetime.date], int],
) -> None:
    """車両の購入・維持・エネルギー・税・車検・売却を月次CFへ反映する."""
    for vehicle in household.vehicles:
        start_date = datetime.date(
            vehicle.ownership_start_year, vehicle.ownership_start_month, 1
        )
        end_date = datetime.date(
            vehicle.ownership_end_year, vehicle.ownership_end_month, 1
        )
        if not start_date <= current <= end_date:
            continue

        months_owned = (year - vehicle.ownership_start_year) * 12 + (
            month - vehicle.ownership_start_month
        )
        replacement_months = (
            vehicle.replacement_cycle_years * 12
            if vehicle.replacement_cycle_years > 0
            else 0
        )
        is_replacement = current in vehicle_replacement_dates[vehicle.id]
        is_end_sale = current == end_date and not is_replacement
        months_since_purchase = (
            months_owned % replacement_months
            if replacement_months > 0
            else months_owned
        )
        if (
            is_end_sale
            and replacement_months > 0
            and months_owned > 0
            and months_since_purchase == 0
        ):
            months_since_purchase = replacement_months
        inflation_factor = (1 + assumptions.inflation_rate) ** (
            year - assumptions.base_year
        )

        if current == start_date or is_replacement:
            purchase_amount = int(vehicle.purchase_price * inflation_factor)
            if current == start_date and vehicle.loan_id:
                linked_loan = next(
                    (
                        loan
                        for loan in household.loans
                        if loan.id == vehicle.loan_id
                    ),
                    None,
                )
                if linked_loan is not None:
                    purchase_amount = max(
                        0, purchase_amount - linked_loan.principal
                    )
            elif is_replacement:
                purchase_amount = max(
                    0,
                    purchase_amount
                    - vehicle_replacement_principal.get(
                        (vehicle.id, current), 0
                    ),
                )
            cf.vehicle_purchase_expense += purchase_amount
            cf.traces.append(
                TraceEntry(
                    "乗り物取得価格",
                    purchase_amount,
                    {
                        "乗り物": vehicle.name,
                        "買替": is_replacement,
                        "取得価格": int(
                            vehicle.purchase_price * inflation_factor
                        ),
                        "初回ローン控除": current == start_date
                        and bool(vehicle.loan_id),
                        "買替ローン控除": (
                            vehicle_replacement_principal.get(
                                (vehicle.id, current), 0
                            )
                            if is_replacement
                            else 0
                        ),
                    },
                )
            )
            if is_replacement:
                sale_amount = _vehicle_sale_price(
                    vehicle,
                    replacement_months or months_since_purchase,
                    inflation_factor,
                )
            else:
                sale_amount = 0
            if is_replacement and sale_amount > 0:
                cf.vehicle_sale_income += sale_amount
                cf.traces.append(
                    TraceEntry(
                        "乗り物売却収入",
                        sale_amount,
                        {
                            "乗り物": vehicle.name,
                            "買替": True,
                            "売却額計算": vehicle.sale_price_mode,
                        },
                    )
                )
        elif is_end_sale:
            sale_amount = _vehicle_sale_price(
                vehicle, months_since_purchase, inflation_factor
            )
            sale_amount = max(0, sale_amount)
            if sale_amount > 0:
                cf.vehicle_sale_income += sale_amount
                cf.traces.append(
                    TraceEntry(
                        "乗り物売却収入",
                        sale_amount,
                        {
                            "乗り物": vehicle.name,
                            "所有終了": True,
                            "売却額計算": vehicle.sale_price_mode,
                        },
                    )
                )

        if vehicle.energy_type == "ガソリン" and vehicle.monthly_distance_km > 0:
            fuel_amount = int(
                vehicle.monthly_distance_km
                / vehicle.fuel_efficiency_km_per_liter
                * vehicle.fuel_price_per_liter
                * inflation_factor
            )
            if fuel_amount > 0:
                cf.vehicle_fuel_expense += fuel_amount
                cf.traces.append(
                    TraceEntry(
                        "ガソリン代",
                        fuel_amount,
                        {
                            "乗り物": vehicle.name,
                            "走行距離(km)": vehicle.monthly_distance_km,
                            "燃費(km/L)": vehicle.fuel_efficiency_km_per_liter,
                            "単価(円/L)": vehicle.fuel_price_per_liter,
                        },
                    )
                )
        elif vehicle.energy_type == "電気" and vehicle.monthly_distance_km > 0:
            electricity_amount = int(
                vehicle.monthly_distance_km
                / 100
                * vehicle.electricity_consumption_kwh_per_100km
                * vehicle.electricity_price_per_kwh
                * inflation_factor
            )
            if electricity_amount > 0:
                cf.vehicle_electricity_expense += electricity_amount
                cf.traces.append(
                    TraceEntry(
                        "電気代",
                        electricity_amount,
                        {
                            "乗り物": vehicle.name,
                            "走行距離(km)": vehicle.monthly_distance_km,
                            "電費(kWh/100km)": vehicle.electricity_consumption_kwh_per_100km,
                            "単価(円/kWh)": vehicle.electricity_price_per_kwh,
                        },
                    )
                )

        automobile_tax_before_reduction, automobile_tax_basis = (
            _automobile_tax_before_reduction(vehicle)
        )
        if month == vehicle.ownership_start_month and automobile_tax_before_reduction > 0:
            amount = int(
                automobile_tax_before_reduction
                * (1 - vehicle.automobile_tax_reduction_rate)
                * inflation_factor
            )
            cf.vehicle_automobile_tax += amount
            cf.traces.append(
                TraceEntry(
                    "自動車税",
                    amount,
                    {
                        "乗り物": vehicle.name,
                        "減税前年額": automobile_tax_before_reduction,
                        "計算方式": automobile_tax_basis,
                        "エコカー減税率": vehicle.automobile_tax_reduction_rate,
                    },
                )
            )

        if vehicle.monthly_maintenance > 0:
            amount = int(vehicle.monthly_maintenance * inflation_factor)
            cf.vehicle_maintenance += amount
            cf.traces.append(
                TraceEntry(
                    "乗り物維持費",
                    amount,
                    {"乗り物": vehicle.name, "月額": vehicle.monthly_maintenance},
                )
            )
        if month == vehicle.ownership_start_month and vehicle.annual_tax_repair > 0:
            amount = int(vehicle.annual_tax_repair * inflation_factor)
            cf.vehicle_tax_repair += amount
            cf.traces.append(
                TraceEntry(
                    "乗り物税金・修繕費",
                    amount,
                    {"乗り物": vehicle.name, "年額": vehicle.annual_tax_repair},
                )
            )
        inspection_start_month = 36 if vehicle.vehicle_type == "新車" else 0
        if months_since_purchase >= inspection_start_month:
            inspection_cycle_months = vehicle.inspection_cycle_years * 12
            if (
                months_since_purchase - inspection_start_month
            ) % inspection_cycle_months == 0:
                if vehicle.inspection_cost > 0:
                    inspection_amount = int(vehicle.inspection_cost * inflation_factor)
                    cf.vehicle_inspection_expense += inspection_amount
                    cf.traces.append(
                        TraceEntry(
                            "車検費用",
                            inspection_amount,
                            {
                                "乗り物": vehicle.name,
                                "周期年数": vehicle.inspection_cycle_years,
                            },
                        )
                    )
                inspection_period_years = (
                    3
                    if vehicle.vehicle_type == "新車"
                    and months_since_purchase == inspection_start_month
                    else vehicle.inspection_cycle_years
                )
                weight_tax_before_reduction, weight_tax_basis = (
                    _weight_tax_before_reduction(vehicle, inspection_period_years)
                )
                if weight_tax_before_reduction > 0:
                    weight_tax = int(
                        weight_tax_before_reduction
                        * (1 - vehicle.weight_tax_reduction_rate)
                        * inflation_factor
                    )
                    cf.vehicle_weight_tax += weight_tax
                    cf.traces.append(
                        TraceEntry(
                            "重量税",
                            weight_tax,
                            {
                                "乗り物": vehicle.name,
                                "減税前車検時税額": weight_tax_before_reduction,
                                "計算方式": weight_tax_basis,
                                "エコカー減税率": vehicle.weight_tax_reduction_rate,
                            },
                        )
                    )


def simulate(
    store: ParameterStore,
    household: Household,
    scenario: DisasterScenario | None = None,
) -> SimulationResult:
    """世帯の生涯キャッシュフローをシミュレーションする."""
    household.validate_childcare_leave_links()
    if scenario is not None:
        if scenario.death_age < 0 or scenario.death_age > 120:
            raise ValueError("death_age must be between 0 and 120")
        if (
            scenario.survivor_pension_monthly is not None
            and scenario.survivor_pension_monthly < 0
        ):
            raise ValueError("survivor_pension_monthly must not be negative")
        if (
            scenario.child_allowance_monthly is not None
            and scenario.child_allowance_monthly < 0
        ):
            raise ValueError("child_allowance_monthly must not be negative")
        if scenario.child_allowance_end_age < 0:
            raise ValueError("child_allowance_end_age must not be negative")
        if not 0 <= scenario.living_expense_reduction_rate <= 1:
            raise ValueError("living_expense_reduction_rate must be between 0 and 1")

    assumptions = household.assumptions
    householder = household.householder()

    # シミュレーション期間: 基準年月〜世帯主の想定寿命の誕生月
    start = datetime.date(assumptions.base_year, assumptions.base_month, 1)
    end_year = householder.birth_date.year + householder.life_expectancy_age
    end = datetime.date(end_year, householder.birth_date.month, 1)

    # 口座残高の初期値(基準月の月初残高)
    balances: dict[str, int] = {acc.id: acc.balance for acc in household.accounts}
    scheduled_balances: dict[str, int] = {}
    total_balance = sum(balances.values())
    ideco_accounts = {
        plan.id: IdecoAccount(balance=plan.initial_balance)
        for plan in household.ideco_plans
    }
    nisa_accounts = {
        # 初期残高は簿価=評価額と仮定してつみたて枠へ計上(生涯枠を消費)
        plan.id: NisaAccount(
            tsumitate_balance=plan.initial_balance,
            tsumitate_cost=plan.initial_balance,
        )
        for plan in household.nisa_plans
    }

    financing = _prepare_financing(household, start, end)
    loan_repayments_by_date = financing.loan_repayments_by_date
    vehicle_loan_settlements_by_date = financing.vehicle_loan_settlements_by_date
    vehicle_loan_fees_by_date = financing.vehicle_loan_fees_by_date
    vehicle_replacement_principal = financing.vehicle_replacement_principal
    vehicle_replacement_dates = financing.vehicle_replacement_dates

    results: list[MonthlyCashflow] = []

    # 住民税スケジュールを事前計算(年ごとにキャッシュ)
    resident_tax_cache: dict[int, dict[datetime.date, int]] = {}
    estimate_cache = AnnualTaxEstimateCache()

    current = start
    deceased = next(
        (member for member in household.members if scenario and member.id == scenario.deceased_member_id),
        None,
    )
    death_date = (
        datetime.date(deceased.birth_date.year + scenario.death_age, deceased.birth_date.month, 1)
        if deceased and scenario
        else None
    )

    def member_alive(member: Member, date: datetime.date) -> bool:
        if deceased is None or member.id != deceased.id:
            return True
        return death_date is not None and date < death_date

    while current <= end:
        year = current.year
        month = current.month
        age = age_at(householder.birth_date, current)

        cf = MonthlyCashflow(date=current, age=age)

        # --- 収入(勤労)・年金・万が一時の追加収入 ---
        monthly_salary_total = _apply_work_income(
            store=store,
            household=household,
            current=current,
            year=year,
            month=month,
            assumptions=assumptions,
            cf=cf,
            member_alive=member_alive,
        )
        _apply_pension_and_disaster_income(
            store=store,
            household=household,
            current=current,
            scenario=scenario,
            death_date=death_date,
            cf=cf,
            member_alive=member_alive,
        )

        # --- 所得税・住民税 ---
        _apply_income_tax(
            store=store,
            household=household,
            current=current,
            year=year,
            assumptions=assumptions,
            monthly_salary_total=monthly_salary_total,
            cf=cf,
            member_alive=member_alive,
            estimate_cache=estimate_cache,
        )
        _apply_resident_tax(
            store=store,
            household=household,
            current=current,
            year=year,
            assumptions=assumptions,
            resident_tax_cache=resident_tax_cache,
            cf=cf,
            member_alive=member_alive,
            estimate_cache=estimate_cache,
        )

        # --- ローン返済 ---
        for loan_name, repayment in loan_repayments_by_date.get(current, []):
            cf.loan_payment += repayment.payment
            cf.loan_interest += repayment.interest_part
            cf.traces.append(
                TraceEntry(
                    "ローン返済",
                    repayment.payment,
                    {
                        "loan": loan_name,
                        "元金": repayment.principal_part,
                        "利息": repayment.interest_part,
                        "残高": repayment.balance,
                    },
                )
            )
        for vehicle_name, settlement in vehicle_loan_settlements_by_date.get(current, []):
            cf.loan_payment += settlement
            cf.traces.append(
                TraceEntry(
                    "車両ローン残債精算",
                    settlement,
                    {"乗り物": vehicle_name, "買替・売却時": True},
                )
            )
        for vehicle_name, fee in vehicle_loan_fees_by_date.get(current, []):
            cf.loan_payment += fee
            cf.traces.append(
                TraceEntry(
                    "車両ローン手数料",
                    fee,
                    {"乗り物": vehicle_name, "買替時": True},
                )
            )

        # --- 支出 ---
        _apply_housing(
            household.owned_housing,
            current,
            month,
            cf,
            base_year=assumptions.base_year,
        )
        _apply_vehicle_expenses(
            household=household,
            current=current,
            year=year,
            month=month,
            assumptions=assumptions,
            cf=cf,
            vehicle_replacement_dates=vehicle_replacement_dates,
            vehicle_replacement_principal=vehicle_replacement_principal,
        )

        _apply_expenses(
            household=household,
            current=current,
            year=year,
            month=month,
            age=age,
            assumptions=assumptions,
            householder=householder,
            scenario=scenario,
            death_date=death_date,
            cf=cf,
            member_alive=member_alive,
        )

        if (
            scenario
            and death_date
            and current >= death_date
            and scenario.living_expense_reduction_rate > 0
        ):
            reduction = int(cf.living_expense * scenario.living_expense_reduction_rate)
            cf.living_expense -= reduction
            if reduction > 0:
                cf.traces.append(
                    TraceEntry(
                        "万が一時の生活費削減",
                        reduction,
                        {
                            "削減率": scenario.living_expense_reduction_rate,
                            "scenario": scenario.name,
                        },
                    )
                )

        # --- 教育費 ---
        _apply_education_expenses(
            store=store,
            household=household,
            current=current,
            cf=cf,
            member_alive=member_alive,
        )

        # --- 保険 ---
        _apply_insurance(
            household=household,
            current=current,
            cf=cf,
            scenario=scenario,
            death_date=death_date,
            deceased=deceased,
            member_alive=member_alive,
        )

        # --- iDeCo/NISA ---
        _apply_investments(
            store=store,
            household=household,
            current=current,
            cf=cf,
            ideco_accounts=ideco_accounts,
            nisa_accounts=nisa_accounts,
            member_alive=member_alive,
        )

        cf.ideco_balance = sum(account.balance for account in ideco_accounts.values())
        cf.nisa_balance = sum(account.balance for account in nisa_accounts.values())
        cf.account_investment_balance = _apply_account_schedules(
            household=household,
            current=current,
            age=age,
            base_year=assumptions.base_year,
            balances=balances,
            scheduled_balances=scheduled_balances,
            cf=cf,
        )

        # --- 口座残高 ---
        total_balance += cf.net
        cf.balance = total_balance

        results.append(cf)
        current = _add_months(current, 1)

    snapshot = store.snapshot(start)
    return SimulationResult(monthly=results, parameter_snapshot=snapshot)
