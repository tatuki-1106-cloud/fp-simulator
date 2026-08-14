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

import datetime
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from fp_simulator.parameters.loader import ParameterStore
from fp_simulator.engine.models import Household, Member, Relationship, SocialInsuranceType
from fp_simulator.engine.income import salary_income_after_deduction
from fp_simulator.engine.income_tax import (
    Deductions,
    calc_annual_income_tax,
    monthly_income_tax_schedule,
)
from fp_simulator.engine.resident_tax import monthly_resident_tax_schedule
from fp_simulator.engine.social_insurance import monthly_social_insurance
from fp_simulator.engine.pension import PensionRecord, total_pension
from fp_simulator.engine.retirement import net_retirement_allowance
from fp_simulator.engine.dependency import calc_deductions_for_household, age_at_year_end, age_at
from fp_simulator.engine.investment import IdecoAccount, NisaAccount, withdrawal_amount
from fp_simulator.engine.loan import LoanTerms, MonthlyRepayment, loan_schedule


@dataclass
class TraceEntry:
    """計算根拠の1エントリ."""

    item: str  # 項目名(例: "所得税")
    amount: int  # 金額
    basis: dict[str, Any]  # 根拠(パラメータパス、式、中間値等)


@dataclass
class MonthlyCashflow:
    """1ヶ月のキャッシュフロー."""

    date: datetime.date  # 年月(1日)
    age: int  # 世帯主の当月末年齢
    # 収入
    salary_income: int = 0
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
    vehicle_tax_repair: int = 0
    vehicle_inspection_expense: int = 0
    loan_payment: int = 0  # ローン返済
    loan_interest: int = 0  # うち利息
    education_expense: int = 0  # 教育費
    insurance_premium: int = 0  # 保険料
    ideco_contribution: int = 0  # iDeCo掛金(所得控除対象)
    nisa_investment: int = 0  # NISA投資
    ideco_balance: int = 0  # iDeCo運用残高
    nisa_balance: int = 0  # NISA運用残高
    # 収支
    @property
    def total_income(self) -> int:
        return (
            self.salary_income
            + self.pension_income
            + self.retirement_income
            + self.other_income
            + self.death_benefit
            + self.survivor_pension
            + self.child_allowance
            + self.ideco_withdrawal
            + self.nisa_withdrawal
            + self.vehicle_sale_income
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
            + self.vehicle_tax_repair
            + self.vehicle_inspection_expense
            + self.loan_payment
            + self.education_expense
            + self.insurance_premium
            + self.ideco_contribution
            + self.nisa_investment
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
        return self.balance + self.ideco_balance + self.nisa_balance

    # 残高
    balance: int = 0  # 当月末の世帯総残高
    traces: list[TraceEntry] = field(default_factory=list)


@dataclass
class SimulationResult:
    """シミュレーション結果."""

    monthly: list[MonthlyCashflow]
    parameter_snapshot: dict[str, Any]  # 計算時のパラメータ版(再現性)


@dataclass(frozen=True)
class DisasterScenario:
    """万が一シナリオ。指定メンバーが指定年齢で死亡した前提.

    遺族年金・児童手当は制度の詳細条件を入力データだけで確定できないため、
    現段階では利用者が設定する世帯合計の月額として扱う。
    """

    deceased_member_id: str
    death_age: int
    name: str = "万が一"
    survivor_pension_monthly: int = 0
    child_allowance_monthly: int = 0
    child_allowance_end_age: int = 18
    living_expense_reduction_rate: float = 0.0


def _add_months(date: datetime.date, months: int) -> datetime.date:
    """date に months ヶ月を加算(日は1日固定)."""
    total = date.year * 12 + date.month - 1 + months
    return datetime.date(total // 12, total % 12 + 1, 1)


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


def simulate(
    store: ParameterStore,
    household: Household,
    scenario: DisasterScenario | None = None,
) -> SimulationResult:
    """世帯の生涯キャッシュフローをシミュレーションする."""
    if scenario is not None:
        if scenario.death_age < 0 or scenario.death_age > 120:
            raise ValueError("death_age must be between 0 and 120")
        if scenario.survivor_pension_monthly < 0:
            raise ValueError("survivor_pension_monthly must not be negative")
        if scenario.child_allowance_monthly < 0:
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
    total_balance = sum(balances.values())
    ideco_accounts = {
        plan.id: IdecoAccount(balance=plan.initial_balance)
        for plan in household.ideco_plans
    }
    nisa_accounts = {
        plan.id: NisaAccount(balance=plan.initial_balance)
        for plan in household.nisa_plans
    }

    loan_repayments_by_date: defaultdict[
        datetime.date, list[tuple[str, MonthlyRepayment]]
    ] = defaultdict(list)
    vehicle_loan_settlements_by_date: defaultdict[
        datetime.date, list[tuple[str, int]]
    ] = defaultdict(list)
    vehicle_loan_fees_by_date: defaultdict[datetime.date, list[tuple[str, int]]] = defaultdict(list)
    vehicle_replacement_principal: dict[tuple[str, datetime.date], int] = {}
    vehicle_replacement_dates: dict[str, list[datetime.date]] = {}
    loan_stop_dates: dict[str, datetime.date] = {}
    loan_schedules: dict[str, list[MonthlyRepayment]] = {}

    for vehicle in household.vehicles:
        start_date = datetime.date(
            vehicle.ownership_start_year, vehicle.ownership_start_month, 1
        )
        end_date = datetime.date(vehicle.ownership_end_year, vehicle.ownership_end_month, 1)
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
            if vehicle.loan_id and any(loan.id == vehicle.loan_id for loan in household.loans)
            else 0
        )
        settlement_dates = [*replacement_dates, datetime.date(
            vehicle.ownership_end_year, vehicle.ownership_end_month, 1
        )]
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
            vehicle_replacement_principal[(vehicle.id, replacement_date)] = replacement_principal
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

    results: list[MonthlyCashflow] = []

    # 住民税スケジュールを事前計算(年ごとにキャッシュ)
    resident_tax_cache: dict[int, dict[datetime.date, int]] = {}

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
        return not deceased or member.id != deceased.id or date < death_date

    while current <= end:
        year = current.year
        month = current.month
        age = age_at(householder.birth_date, current)

        cf = MonthlyCashflow(date=current, age=age)

        # --- 収入(勤労) ---
        monthly_salary_total = 0
        for income in household.incomes:
            member = next((m for m in household.members if m.id == income.member_id), None)
            if member is None:
                continue
            if not member_alive(member, current):
                continue
            member_age = age_at(member.birth_date, current)

            # 期間判定
            if member_age < income.start_age:
                continue
            if income.end_age is not None and member_age > income.end_age:
                continue
            if member_age == income.start_age and month < income.start_month:
                continue
            is_active = not (
                income.end_age is not None
                and member_age == income.end_age
                and month > income.end_month
            )

            if is_active:
                # 上昇率適用(基準年からの年数)
                years_elapsed = year - assumptions.base_year
                raise_factor = (1 + income.annual_raise_rate) ** years_elapsed
                salary = int(income.monthly_amount * raise_factor)
                bonus = 0
                if month in income.bonus_months:
                    bonus = int(income.bonus_amount * raise_factor)
                gross = salary + bonus
                monthly_salary_total += gross

                # 社会保険料
                if income.social_insurance_type in (
                    SocialInsuranceType.KYOSAI_KOSEI,
                    SocialInsuranceType.YAKUIN_KOSEI,
                ):
                    si = monthly_social_insurance(
                        store, current, salary, member.prefecture, member_age, is_employee=True
                    )
                    cf.social_insurance += si.total
                    cf.traces.append(
                        TraceEntry("社会保険料", si.total, {
                            "member": member.name,
                            "標準報酬月額": salary,
                            "厚生年金": si.pension, "健康保険": si.health,
                            "介護保険": si.nursing, "雇用保険": si.employment,
                        })
                    )

            # 退職金(退職年齢に達する誕生月に計上、給与期間終了後でも)
            if (
                income.retirement_age is not None
                and member_age == income.retirement_age
                and month == member.birth_date.month
            ):
                if income.retirement_allowance > 0:
                    years_of_service = income.retirement_age - income.start_age
                    net = net_retirement_allowance(
                        store, current, income.retirement_allowance, years_of_service
                    )
                    cf.retirement_income += net
                    cf.traces.append(
                        TraceEntry("退職金(手取り)", net, {
                            "member": member.name,
                            "額面": income.retirement_allowance,
                            "勤続年数": years_of_service,
                        })
                    )
        cf.salary_income = monthly_salary_total

        # --- 年金 ---
        for pr in household.pension_records:
            member = next((m for m in household.members if m.id == pr.member_id), None)
            if member is None:
                continue
            if not member_alive(member, current):
                continue
            member_age = age_at(member.birth_date, current)

            pension_start_age = pr.start_age - (pr.months_early // 12) + (pr.months_deferred // 12)
            if member_age >= pension_start_age:
                record = PensionRecord(
                    kokumin_months=pr.kokumin_months,
                    kousei_months=pr.kousei_months,
                    avg_standard_remuneration=pr.avg_standard_remuneration,
                    kousei_months_before_2003_04=pr.kousei_months_before_2003_04,
                    kousei_months_after_2003_04=pr.kousei_months_after_2003_04,
                )
                annual = total_pension(store, current, record, pr.months_early, pr.months_deferred)
                monthly_pension = annual // 12
                cf.pension_income += monthly_pension
                cf.traces.append(
                    TraceEntry("年金収入", monthly_pension, {
                        "member": member.name,
                        "年額": annual,
                    })
                )

        # --- 万が一時の追加収入 ---
        if scenario and death_date and current >= death_date:
            if scenario.survivor_pension_monthly > 0:
                cf.survivor_pension = scenario.survivor_pension_monthly
                cf.traces.append(
                    TraceEntry(
                        "遺族年金",
                        cf.survivor_pension,
                        {"月額": scenario.survivor_pension_monthly, "scenario": scenario.name},
                    )
                )

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
                            "月額": scenario.child_allowance_monthly,
                            "対象年齢未満": scenario.child_allowance_end_age,
                            "scenario": scenario.name,
                        },
                    )
                )

        # --- 所得税(月次源泉徴収+年末調整) ---
        # 簡易モデル: その年の推定年収から年間税額を計算し、12で割って月次計上、
        # 12月に年末調整で精算する。
        # 扶養控除の判定にはその年の推定年収を使う
        if monthly_salary_total > 0:
            # 推定年収(月給の12倍+賞与)を計算
            est_annual = 0
            for income in household.incomes:
                member = next((m for m in household.members if m.id == income.member_id), None)
                if member is None:
                    continue
                if not member_alive(member, current):
                    continue
                member_age = age_at_year_end(member.birth_date, year)
                if current.month < member.birth_date.month:
                    member_age -= 1
                if member_age < income.start_age or (
                    income.end_age is not None and member_age > income.end_age
                ):
                    continue
                years_elapsed = year - assumptions.base_year
                raise_factor = (1 + income.annual_raise_rate) ** years_elapsed
                annual = int(income.monthly_amount * raise_factor) * 12
                annual += int(income.bonus_amount * raise_factor) * len(income.bonus_months)
                est_annual += annual

            spouse_income = 0  # MVP: 配偶者収入は0扱い(将来拡張)
            spouse_ded, dep_ded = calc_deductions_for_household(
                store, year, household, est_annual, spouse_income
            )
            deductions = Deductions(
                basic=store.get("所得税.基礎控除.控除額", datetime.date(year, 12, 31)),
                social_insurance=cf.social_insurance * 12,  # 年間推定
                spouse=spouse_ded,
                dependent=dep_ded,
            )
            income_after = salary_income_after_deduction(store, datetime.date(year, 12, 31), est_annual)
            annual_tax = calc_annual_income_tax(store, datetime.date(year, 12, 31), income_after, deductions)

            if month < 12:
                monthly_tax = annual_tax // 12
                cf.income_tax = monthly_tax
                cf.traces.append(
                    TraceEntry("所得税(源泉徴収)", monthly_tax, {
                        "推定年収": est_annual, "年間推定税額": annual_tax,
                    })
                )
            else:
                # 12月: 年末調整(年間確定税額 - 1〜11月徴収分)
                withheld_so_far = (annual_tax // 12) * 11
                adjustment = annual_tax - withheld_so_far
                cf.income_tax = adjustment
                cf.traces.append(
                    TraceEntry("所得税(年末調整)", adjustment, {
                        "年間確定税額": annual_tax, "1-11月徴収済": withheld_so_far,
                    })
                )

        # --- 住民税(前年所得課税、翌年6月〜) ---
        if year not in resident_tax_cache:
            # 前年の課税所得を計算(簡易: 前年の推定年収ベース)
            prev_year = year - 1
            prev_est_annual = 0
            for income in household.incomes:
                member = next((m for m in household.members if m.id == income.member_id), None)
                if member is None:
                    continue
                if not member_alive(member, datetime.date(prev_year, 12, 31)):
                    continue
                member_age = age_at_year_end(member.birth_date, prev_year)
                if member_age < income.start_age or (
                    income.end_age is not None and member_age > income.end_age
                ):
                    continue
                years_elapsed = prev_year - assumptions.base_year
                raise_factor = (1 + income.annual_raise_rate) ** years_elapsed
                annual = int(income.monthly_amount * raise_factor) * 12
                annual += int(income.bonus_amount * raise_factor) * len(income.bonus_months)
                prev_est_annual += annual

            if prev_est_annual > 0:
                # 前年の社会保険料を推定(前年の月給で計算)
                prev_si = 0
                for income in household.incomes:
                    member = next((m for m in household.members if m.id == income.member_id), None)
                    if member is None:
                        continue
                    member_age_prev = age_at(member.birth_date, datetime.date(prev_year, 6, 1))
                    if member_age_prev < income.start_age or (
                        income.end_age is not None and member_age_prev > income.end_age
                    ):
                        continue
                    years_elapsed = prev_year - assumptions.base_year
                    raise_factor = (1 + income.annual_raise_rate) ** years_elapsed
                    prev_monthly = int(income.monthly_amount * raise_factor)
                    si = monthly_social_insurance(
                        store, datetime.date(prev_year, 6, 1),
                        prev_monthly, member.prefecture, member_age_prev, is_employee=True,
                    )
                    prev_si += si.total * 12

                prev_deductions = Deductions(
                    basic=store.get("所得税.基礎控除.控除額", datetime.date(prev_year, 12, 31)),
                    social_insurance=prev_si,
                    spouse=0, dependent=0,
                )
                prev_income_after = salary_income_after_deduction(
                    store, datetime.date(prev_year, 12, 31), prev_est_annual
                )
                from fp_simulator.engine.income_tax import calc_taxable_income
                prev_taxable = calc_taxable_income(
                    store, datetime.date(prev_year, 12, 31), prev_income_after, prev_deductions
                )
                resident_tax_cache[year] = monthly_resident_tax_schedule(
                    store, prev_year, prev_taxable, prev_deductions
                )
            else:
                resident_tax_cache[year] = {}

        cf.resident_tax = resident_tax_cache[year].get(current, 0)
        if cf.resident_tax > 0:
            cf.traces.append(TraceEntry("住民税", cf.resident_tax, {"前年所得課税": True}))

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
        # Q6所有住宅: 頭金は購入月、固定資産税・修繕費は購入後の購入月に年1回計上。
        housing = household.owned_housing
        if housing is not None:
            purchase_date = datetime.date(housing.purchase_year, housing.purchase_month, 1)
            if current >= purchase_date:
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
                if month == housing.purchase_month:
                    if housing.annual_property_tax > 0:
                        cf.property_tax = housing.annual_property_tax
                        cf.traces.append(
                            TraceEntry(
                                "固定資産税",
                                housing.annual_property_tax,
                                {"年額": housing.annual_property_tax},
                            )
                        )
                    if housing.annual_repair_cost > 0:
                        cf.repair_expense = housing.annual_repair_cost
                        cf.traces.append(
                            TraceEntry(
                                "住宅修繕費",
                                housing.annual_repair_cost,
                                {"年額": housing.annual_repair_cost},
                            )
                        )

        # Q7乗り物: FP-UNIV方式で初回購入/買替/維持費/税修繕/車検/売却を計上。
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
            months_since_purchase = (
                months_owned % replacement_months if replacement_months > 0 else months_owned
            )
            is_end_sale = current == end_date and not is_replacement
            inflation_factor = (1 + assumptions.inflation_rate) ** (year - assumptions.base_year)

            if current == start_date or is_replacement:
                purchase_amount = int(vehicle.purchase_price * inflation_factor)
                if current == start_date and vehicle.loan_id:
                    linked_loan = next(
                        (loan for loan in household.loans if loan.id == vehicle.loan_id), None
                    )
                    if linked_loan is not None:
                        purchase_amount = max(0, purchase_amount - linked_loan.principal)
                elif is_replacement:
                    purchase_amount = max(
                        0,
                        purchase_amount
                        - vehicle_replacement_principal.get((vehicle.id, current), 0),
                    )
                cf.vehicle_purchase_expense += purchase_amount
                cf.traces.append(
                    TraceEntry(
                        "乗り物取得価格",
                        purchase_amount,
                        {
                            "乗り物": vehicle.name,
                            "買替": is_replacement,
                            "取得価格": int(vehicle.purchase_price * inflation_factor),
                            "初回ローン控除": current == start_date and bool(vehicle.loan_id),
                            "買替ローン控除": (
                                vehicle_replacement_principal.get((vehicle.id, current), 0)
                                if is_replacement
                                else 0
                            ),
                        },
                    )
                )
                if is_replacement and vehicle.sale_price > 0:
                    sale_amount = int(vehicle.sale_price * inflation_factor)
                    cf.vehicle_sale_income += sale_amount
                    cf.traces.append(
                        TraceEntry(
                            "乗り物売却収入",
                            sale_amount,
                            {"乗り物": vehicle.name, "買替": True},
                        )
                    )
            elif is_end_sale and vehicle.sale_price > 0:
                sale_amount = int(vehicle.sale_price * inflation_factor)
                cf.vehicle_sale_income += sale_amount
                cf.traces.append(
                    TraceEntry(
                        "乗り物売却収入",
                        sale_amount,
                        {"乗り物": vehicle.name, "所有終了": True},
                    )
                )

            if vehicle.monthly_maintenance > 0:
                amount = int(vehicle.monthly_maintenance * inflation_factor)
                cf.vehicle_maintenance += amount
                cf.traces.append(
                    TraceEntry("乗り物維持費", amount, {"乗り物": vehicle.name, "月額": vehicle.monthly_maintenance})
                )
            if month == vehicle.ownership_start_month and vehicle.annual_tax_repair > 0:
                amount = int(vehicle.annual_tax_repair * inflation_factor)
                cf.vehicle_tax_repair += amount
                cf.traces.append(
                    TraceEntry("乗り物税金・修繕費", amount, {"乗り物": vehicle.name, "年額": vehicle.annual_tax_repair})
                )
            inspection_start_month = 36 if vehicle.vehicle_type == "新車" else 0
            if vehicle.inspection_cost > 0 and months_since_purchase >= inspection_start_month:
                inspection_cycle_months = vehicle.inspection_cycle_years * 12
                if (months_since_purchase - inspection_start_month) % inspection_cycle_months == 0:
                    cf.vehicle_inspection_expense += vehicle.inspection_cost
                    cf.traces.append(
                        TraceEntry(
                            "車検費用",
                            vehicle.inspection_cost,
                            {"乗り物": vehicle.name, "周期年数": vehicle.inspection_cycle_years},
                        )
                    )

        for expense in household.expenses:
                if expense.member_id is not None:
                    member = next((m for m in household.members if m.id == expense.member_id), None)
                    if member is None:
                        continue
                    if not member_alive(member, current):
                        continue
                    member_age = age_at(member.birth_date, current)
                    if member_age < expense.start_age:
                        continue
                    if expense.end_age is not None and member_age > expense.end_age:
                        continue
                else:
                    # 世帯全体の支出: 世帯主年齢で判定。
                    # start_age=0 は「基準年開始」を意味するため、年齢0以上は常に対象
                    if expense.start_age > 0 and age < expense.start_age:
                        continue
                    if expense.end_age is not None and expense.end_age > 0 and age > expense.end_age:
                        continue

                years_elapsed = year - assumptions.base_year
                raise_factor = (1 + expense.annual_raise_rate) ** years_elapsed
                inflation_factor = (1 + assumptions.inflation_rate) ** years_elapsed

                recurring_amount = int(expense.monthly_amount * raise_factor * inflation_factor)
                once_amount = int(expense.monthly_amount * inflation_factor)
                if scenario and death_date and current >= death_date and expense.disaster_amount is not None:
                    recurring_amount = int(expense.disaster_amount * inflation_factor)
                    once_amount = recurring_amount

                if expense.cycle == "monthly" and expense.event_type == "生活費":
                    cf.living_expense += recurring_amount
                elif expense.cycle == "monthly":
                    cf.event_expense += recurring_amount
                elif expense.cycle == "yearly" and month == expense.yearly_month:
                    cf.event_expense += recurring_amount
                elif expense.cycle == "once":
                    # 開始年月の1回のみ
                    event_year = (
                        assumptions.base_year
                        if expense.start_age == 0
                        else householder.birth_date.year + expense.start_age
                    )
                    if year == event_year and month == expense.start_month:
                        cf.event_expense += once_amount

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
        for edu in household.education_plans:
            member = next((m for m in household.members if m.id == edu.member_id), None)
            if member is None:
                continue
            if not member_alive(member, current):
                continue
            child_age = age_at(member.birth_date, current)
            from fp_simulator.engine.education import monthly_education_costs

            monthly_cost, schools = monthly_education_costs(store, current, child_age, edu.path)
            if edu.include_lessons:
                lessons = store.get("教育費.習い事", current) // 12
                monthly_cost += lessons
                schools.append("習い事")
            cf.education_expense += monthly_cost
            if monthly_cost > 0:
                cf.traces.append(
                    TraceEntry("教育費", monthly_cost, {"child": member.name, "schools": schools})
                )

        # --- 保険 ---
        for ins in household.insurances:
            ins_start = datetime.date(ins.start_year, ins.start_month, 1)
            ins_end = datetime.date(ins.end_year, ins.end_month, 1)
            from fp_simulator.engine.insurance import InsurancePolicy, monthly_premium_in_period

            policy = InsurancePolicy(
                name=ins.name,
                insured_member_id=ins.insured_member_id,
                payer_member_id=ins.payer_member_id,
                monthly_premium=ins.monthly_premium,
                start_date=ins_start,
                end_date=ins_end,
                death_benefit=ins.death_benefit,
                surrender_value_rate=ins.surrender_value_rate,
            )
            payer = next((m for m in household.members if m.id == ins.payer_member_id), None)
            premium = (
                monthly_premium_in_period(policy, current)
                if payer is None or member_alive(payer, current)
                else 0
            )
            cf.insurance_premium += premium
            if premium > 0:
                cf.traces.append(TraceEntry("保険料", premium, {"name": ins.name}))

            insured = next((m for m in household.members if m.id == ins.insured_member_id), None)
            if (
                death_date
                and insured
                and insured.id == deceased.id
                and current == death_date
            ):
                benefit = int(ins.death_benefit)
                cf.death_benefit += benefit
                if benefit > 0:
                    cf.traces.append(
                        TraceEntry("死亡保険金", benefit, {"name": ins.name, "scenario": scenario.name})
                    )

        # --- iDeCo ---
        for ideco in household.ideco_plans:
            member = next((m for m in household.members if m.id == ideco.member_id), None)
            if member is None:
                continue
            if not member_alive(member, current):
                continue
            member_age = age_at(member.birth_date, current)
            from fp_simulator.engine.investment import (
                ideco_contribution_limit,
                ideco_monthly_step,
            )

            in_contribution_window = ideco.start_age <= member_age < ideco.end_age
            previous = ideco_accounts[ideco.id]
            updated = ideco_monthly_step(
                store,
                current,
                previous,
                ideco.monthly_contribution if in_contribution_window else 0,
                ideco.subscriber_type,
                ideco.annual_return_rate,
            )
            ideco_accounts[ideco.id] = updated
            contribution = updated.total_contributions - previous.total_contributions
            cf.ideco_contribution += contribution
            if contribution > 0:
                cf.traces.append(
                    TraceEntry("iDeCo掛金", contribution, {
                        "member": member.name,
                        "上限": ideco_contribution_limit(store, current, ideco.subscriber_type),
                        "note": "全額所得控除(小規模企業共済等掛金控除)",
                    })
                )
            if (
                ideco.receive_start_age is not None
                and member_age >= ideco.receive_start_age
                and ideco.monthly_withdrawal > 0
            ):
                withdrawal = withdrawal_amount(updated.balance, ideco.monthly_withdrawal)
                updated = IdecoAccount(
                    balance=updated.balance - withdrawal,
                    total_contributions=updated.total_contributions,
                )
                cf.ideco_withdrawal += withdrawal
                withdrawal_tax = int(withdrawal * ideco.withdrawal_tax_rate)
                cf.ideco_withdrawal_tax += withdrawal_tax
                if withdrawal > 0:
                    cf.traces.append(
                        TraceEntry(
                            "iDeCo受取",
                            withdrawal,
                            {
                                "member": member.name,
                                "月額": ideco.monthly_withdrawal,
                                "概算税率": ideco.withdrawal_tax_rate,
                                "概算税額": withdrawal_tax,
                            },
                        )
                    )
                    if withdrawal_tax > 0:
                        cf.traces.append(
                            TraceEntry(
                                "iDeCo受取時税",
                                withdrawal_tax,
                                {"概算税率": ideco.withdrawal_tax_rate},
                            )
                        )
            ideco_accounts[ideco.id] = updated

        # --- NISA ---
        for nisa in household.nisa_plans:
            member = next((m for m in household.members if m.id == nisa.member_id), None)
            if member is None:
                continue
            if not member_alive(member, current):
                continue
            member_age = age_at(member.birth_date, current)
            from fp_simulator.engine.investment import nisa_annual_limit, nisa_monthly_step

            in_contribution_window = member_age >= nisa.start_age and (
                nisa.end_age is None or member_age <= nisa.end_age
            )
            previous = nisa_accounts[nisa.id]
            updated = nisa_monthly_step(
                store,
                current,
                previous,
                nisa.monthly_investment if in_contribution_window else 0,
                nisa.annual_return_rate,
            )
            nisa_accounts[nisa.id] = updated
            investment = updated.total_invested - previous.total_invested
            cf.nisa_investment += investment
            if investment > 0:
                cf.traces.append(
                    TraceEntry("NISA投資", investment, {
                        "member": member.name,
                        "年間上限": nisa_annual_limit(store, current),
                        "note": "運用益非課税",
                    })
                )
            if (
                nisa.receive_start_age is not None
                and member_age >= nisa.receive_start_age
                and nisa.monthly_withdrawal > 0
            ):
                withdrawal = withdrawal_amount(updated.balance, nisa.monthly_withdrawal)
                nisa_accounts[nisa.id] = NisaAccount(
                    balance=updated.balance - withdrawal,
                    total_invested=updated.total_invested,
                )
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
                            },
                        )
                    )

        cf.ideco_balance = sum(account.balance for account in ideco_accounts.values())
        cf.nisa_balance = sum(account.balance for account in nisa_accounts.values())

        # --- 口座残高 ---
        total_balance += cf.net
        cf.balance = total_balance

        results.append(cf)
        current = _add_months(current, 1)

    snapshot = store.snapshot(start)
    return SimulationResult(monthly=results, parameter_snapshot=snapshot)
