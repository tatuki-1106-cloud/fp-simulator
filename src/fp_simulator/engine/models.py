"""ドメインモデル(世帯・家族・収入・支出・資産).

FP-UNIVのQ1/Q2/Q4/Q11に対応する入力データモデル。
すべてpydanticで定義し、JSONシリアライズ可能。
"""

from __future__ import annotations

import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class Relationship(str, Enum):
    """続柄."""

    HOUSEHOLDER = "世帯主"
    SPOUSE = "配偶者"
    CHILD = "子"
    OTHER = "その他"


class SocialInsuranceType(str, Enum):
    """社会保険の加入区分(FP-UNIVの収入種別4区分に対応)."""

    KYOSAI_KOSEI = "給与(厚生年金)"  # 会社員
    YAKUIN_KOSEI = "役員報酬(厚生年金)"  # 役員(厚生年金)
    YAKUIN_KOKUMIN = "役員報酬(国民年金・国保)"  # 役員(国保)
    KYOSAI_KOKUMIN = "給与(国民年金・国保)"  # 給与(国保)


class Member(BaseModel):
    """家族成员."""

    id: str
    name: str
    relationship: Relationship
    birth_date: datetime.date
    gender: Literal["男", "女"] | None = None
    life_expectancy_age: int = 90
    disability_grade: str | None = None  # 障害等級(障害者控除用)
    # 世帯主と生計を一にする期間(扶養判定用)。None=生涯
    dependent_until_age: int | None = None
    dependent_until_event: Literal["生涯", "最終学歴"] | None = None
    prefecture: str = "東京都"  # 居住地(健康保険料率)


class Income(BaseModel):
    """収入(勤労収入)."""

    id: str
    member_id: str
    name: str = "給与"
    social_insurance_type: SocialInsuranceType = SocialInsuranceType.KYOSAI_KOSEI
    start_age: int = 0  # 開始年齢(0=基準年から)
    start_month: int = 1
    end_age: int | None = None  # 終了年齢(None=生涯)
    end_month: int = 12
    monthly_amount: int  # 月額(額面、円)
    bonus_months: list[int] = Field(default_factory=list)  # 賞与支給月(例: [6, 12])
    bonus_amount: int = 0  # 賞与1回あたり(額面、円)
    annual_raise_rate: float = 0.0  # 年間上昇率(例: 0.01 = 1%)
    retirement_allowance: int = 0  # 退職金(額面、円)
    retirement_age: int | None = None  # 退職年齢


class PensionRecordInput(BaseModel):
    """年金加入記録の入力."""

    member_id: str
    kokumin_months: int = 0
    kousei_months: int = 0
    avg_standard_remuneration: int = 0
    kousei_months_before_2003_04: int = 0
    kousei_months_after_2003_04: int = 0
    start_age: int = 65  # 受給開始年齢
    months_early: int = 0  # 繰上げ月数
    months_deferred: int = 0  # 繰下げ月数


class Expense(BaseModel):
    """支出(生活費・イベント的支出)."""

    id: str
    name: str = "生活費"
    event_type: Literal["生活費", "汎用", "結婚援助", "葬儀費"] = "生活費"
    member_id: str | None = None  # None=世帯全体
    start_age: int = 0
    start_month: int = 1
    end_age: int | None = None
    end_month: int = 12
    start_date: datetime.date | None = None
    end_date: datetime.date | None = None
    monthly_amount: int = 0  # 月額(円)
    # 周期: 毎月 or 毎年 or 1回限り
    cycle: Literal["monthly", "yearly", "once"] = "monthly"
    yearly_month: int = 1  # cycle=yearly の支払月
    annual_raise_rate: float = 0.0
    disaster_amount: int | None = None  # 万が一時の1回/月/年あたり金額

    @model_validator(mode="after")
    def validate_dates(self) -> Expense:
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError("end_date must not precede start_date")
        return self


class Loan(BaseModel):
    """ローン(住宅ローン等)."""

    id: str
    member_id: str
    name: str = "住宅ローン"
    principal: int  # 借入額(円)
    annual_rate: float  # 年利
    years: int  # 返済期間(年)
    repayment_type: Literal["元利均等", "元金均等"] = "元利均等"
    is_variable_rate: bool = False
    bonus_amount: int = 0
    bonus_months: list[int] = Field(default_factory=list)
    deferment_months: int = 0
    start_year: int = 2026
    start_month: int = 1
    # 繰上返済計画: [(年, 月, 金額, タイプ)]
    early_repayments: list[tuple[int, int, int, str]] = Field(default_factory=list)


class OwnedHousingPlan(BaseModel):
    """所有住宅の取得・保有コスト(Q6)."""

    property_price: int = Field(ge=0)  # 物件価格(円)
    down_payment: int = Field(ge=0)  # 頭金(円)
    purchase_year: int = Field(ge=1900, le=2200)
    purchase_month: int = Field(default=1, ge=1, le=12)
    annual_property_tax: int = Field(default=0, ge=0)  # 固定資産税(年額)
    annual_repair_cost: int = Field(default=0, ge=0)  # 修繕費(年額)

    @model_validator(mode="after")
    def validate_down_payment(self) -> OwnedHousingPlan:
        if self.down_payment > self.property_price:
            raise ValueError("down_payment must not exceed property_price")
        return self


class Vehicle(BaseModel):
    """乗り物の所有・買替・売却設定(Q7)."""

    id: str
    name: str = "自動車"
    vehicle_type: Literal["新車", "中古車"] = "新車"
    ownership_start_year: int = Field(default=2026, ge=1900, le=2200)
    ownership_start_month: int = Field(default=1, ge=1, le=12)
    ownership_end_year: int = Field(default=2090, ge=1900, le=2200)
    ownership_end_month: int = Field(default=12, ge=1, le=12)
    purchase_price: int = Field(ge=0)  # 取得価格(円)
    monthly_maintenance: int = Field(default=0, ge=0)  # 維持費(月額)
    annual_tax_repair: int = Field(default=0, ge=0)  # 税金・修繕費(年額)
    replacement_cycle_years: int = Field(default=0, ge=0)  # 0=買替なし
    sale_price: int = Field(default=0, ge=0)  # 買替・所有終了時の売却額
    inspection_cost: int = Field(default=0, ge=0)  # 車検費用
    inspection_cycle_years: int = Field(default=2, ge=1, le=10)
    loan_id: str | None = None  # 初回購入に紐づくQ9ローン
    replacement_loan_principal: int = Field(default=0, ge=0)  # 買替時の借入額
    replacement_loan_annual_rate: float = Field(default=0.0, ge=0)  # 買替ローン年利
    replacement_loan_years: int = Field(default=0, ge=0)  # 0=買替ローンなし
    replacement_loan_fee: int = Field(default=0, ge=0)  # 買替ローン手数料
    replacement_loan_repayment_type: Literal["元利均等", "元金均等"] = "元利均等"

    @model_validator(mode="after")
    def validate_period(self) -> Vehicle:
        start = (self.ownership_start_year, self.ownership_start_month)
        end = (self.ownership_end_year, self.ownership_end_month)
        if end < start:
            raise ValueError("ownership_end must not precede ownership_start")
        if self.replacement_loan_principal > 0 and self.replacement_loan_years <= 0:
            raise ValueError("replacement_loan_years is required when replacement loan is used")
        return self


class EducationPlan(BaseModel):
    """教育費プラン(子ごと)."""

    id: str
    member_id: str  # 子のID
    path: Literal["公立", "私立"] = "公立"
    include_lessons: bool = False  # 習い事を含めるか


class IdecoPlan(BaseModel):
    """iDeCo設定."""

    id: str
    member_id: str
    initial_balance: int = Field(default=0, ge=0)
    monthly_contribution: int = Field(ge=0)  # 月額掛金
    subscriber_type: int = 2  # 1=自営業, 2=会社員, 3=専業主婦
    start_age: int = Field(default=0, ge=0, le=120)
    end_age: int = Field(default=60, ge=0, le=120)  # 掛金拠出終了年齢
    receive_start_age: int = Field(default=65, ge=0, le=120)  # 受取開始年齢
    receive_type: Literal["一時金", "年金", "一時金+年金"] = "一時金"
    monthly_withdrawal: int = Field(default=0, ge=0)  # 明示的な受取月額
    withdrawal_tax_rate: float = Field(default=0.0, ge=0, le=1)  # 概算源泉税率
    annual_return_rate: float = 0.0  # 運用利回り


class NisaPlan(BaseModel):
    """NISA設定."""

    id: str
    member_id: str
    initial_balance: int = Field(default=0, ge=0)
    monthly_investment: int = Field(ge=0)  # 月額投資
    start_age: int = Field(default=0, ge=0, le=120)
    end_age: int | None = Field(default=None, ge=0, le=120)  # None=生涯
    receive_start_age: int | None = Field(default=None, ge=0, le=120)  # 明示的な取崩開始年齢
    monthly_withdrawal: int = Field(default=0, ge=0)  # 受取月額
    annual_return_rate: float = 0.0


class Insurance(BaseModel):
    """保険."""

    id: str
    name: str
    insurance_type: Literal["死亡保障", "医療", "就業不能", "個人年金"] = "死亡保障"
    insured_member_id: str
    payer_member_id: str
    monthly_premium: int
    start_year: int = 2026
    start_month: int = 1
    end_year: int = 2090
    end_month: int = 12
    death_benefit: int = 0
    surrender_value_rate: float = 0.0


class ChildcareLeave(BaseModel):
    """産休・育休."""

    id: str
    member_id: str
    child_birth_date: datetime.date
    maternity_leave_start: datetime.date  # 産前休業開始
    maternity_leave_end: datetime.date  # 産後休業終了
    childcare_leave_start: datetime.date  # 育休開始
    childcare_leave_end: datetime.date  # 育休終了


class Account(BaseModel):
    """資産口座."""

    id: str
    name: str
    member_id: str | None = None  # None=世帯共有
    account_type: Literal["現金", "預金"] = "預金"
    balance: int = 0  # 月初残高(円)
    interest_rate: float = 0.0  # 年利(例: 0.001 = 0.1%)


class PlanAssumptions(BaseModel):
    """プランの前提条件."""

    inflation_rate: float = 0.0  # 物価上昇率(デフォルト0%)
    investment_return_rate: float = 0.0  # 運用利回り(デフォルト0%)
    base_year: int = 2026  # 基準年(シミュレーション開始年)
    base_month: int = 1  # 基準月


class Household(BaseModel):
    """世帯(シミュレーションの単位)."""

    id: str
    name: str
    owner_email: str | None = None
    members: list[Member] = Field(default_factory=list)
    incomes: list[Income] = Field(default_factory=list)
    pension_records: list[PensionRecordInput] = Field(default_factory=list)
    expenses: list[Expense] = Field(default_factory=list)
    accounts: list[Account] = Field(default_factory=list)
    loans: list[Loan] = Field(default_factory=list)
    owned_housing: OwnedHousingPlan | None = None
    vehicles: list[Vehicle] = Field(default_factory=list)
    education_plans: list[EducationPlan] = Field(default_factory=list)
    ideco_plans: list[IdecoPlan] = Field(default_factory=list)
    nisa_plans: list[NisaPlan] = Field(default_factory=list)
    insurances: list[Insurance] = Field(default_factory=list)
    childcare_leaves: list[ChildcareLeave] = Field(default_factory=list)
    assumptions: PlanAssumptions = Field(default_factory=PlanAssumptions)

    def householder(self) -> Member:
        """世帯主を返す."""
        for m in self.members:
            if m.relationship == Relationship.HOUSEHOLDER:
                return m
        raise ValueError("世帯主が見つかりません")
