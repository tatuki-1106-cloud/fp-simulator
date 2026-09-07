"""教育費の計算(純粋関数).

子ごとの学校種別・期間・家庭別の追加費用に応じた月次教育費を計算する。
旧形式の公立/私立パスも引き続き受け付ける。
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from fp_simulator.engine.models import EducationPlan, EducationStage
from fp_simulator.parameters.loader import ParameterStore


@dataclass(frozen=True)
class EducationPeriod:
    """教育期間の定義."""

    stage: str
    school_type: str
    start_age: int
    years: int


@dataclass(frozen=True)
class EducationCostBreakdown:
    """1か月分の教育費内訳."""

    total: int
    school_cost: int
    lessons_cost: int
    admission_fee: int
    material_cost: int
    transport_cost: int
    support: int
    living_cost: int
    schools: list[str]


# 旧形式の進学パス。新形式では段階ごとの設定が優先される。
STANDARD_PATHS: dict[str, list[EducationPeriod]] = {
    "公立": [
        EducationPeriod("保育園", "認可", 0, 3),
        EducationPeriod("幼稚園", "公立", 3, 3),
        EducationPeriod("小学校", "公立", 6, 6),
        EducationPeriod("中学校", "公立", 12, 3),
        EducationPeriod("高校", "公立", 15, 3),
        EducationPeriod("大学", "国立", 18, 4),
    ],
    "私立": [
        EducationPeriod("保育園", "認可", 0, 3),
        EducationPeriod("幼稚園", "私立", 3, 3),
        EducationPeriod("小学校", "私立", 6, 6),
        EducationPeriod("中学校", "私立", 12, 3),
        EducationPeriod("高校", "私立", 15, 3),
        EducationPeriod("大学", "私立文系", 18, 4),
    ],
}


def _standard_stage(plan: EducationPlan, stage_name: str) -> EducationPeriod:
    """指定段階の旧形式パス設定を返す."""
    for period in STANDARD_PATHS[plan.path]:
        if period.stage == stage_name:
            return period
    raise ValueError(f"unsupported education stage: {stage_name}")


def resolved_stages(plan: EducationPlan) -> list[EducationStage]:
    """教育プランを段階別設定へ正規化する."""
    if not plan.stages:
        return [
            EducationStage(
                stage=period.stage,
                school_type=period.school_type,
            )
            for period in STANDARD_PATHS[plan.path]
        ]

    normalized: list[EducationStage] = []
    for stage in plan.stages:
        if stage.school_type != "未定":
            normalized.append(stage)
            continue
        default = _standard_stage(plan, stage.stage)
        normalized.append(stage.model_copy(update={"school_type": default.school_type}))
    return normalized


def annual_education_cost(
    store: ParameterStore,
    date: datetime.date,
    school_type: str,
) -> int:
    """学校種別の年間費用を返す."""
    return store.get(f"教育費.{school_type}", date)


def education_cost_breakdown(
    store: ParameterStore,
    date: datetime.date,
    child_age: int,
    plan: EducationPlan,
    *,
    base_year: int | None = None,
) -> EducationCostBreakdown:
    """指定月の教育費を学校・付随費・支援・習い事に分解して返す."""
    base_year = date.year if base_year is None else base_year
    raise_factor = (1 + plan.education_raise_rate) ** max(0, date.year - base_year)
    school_cost = 0
    lessons_cost = 0
    admission_fee = 0
    material_cost = 0
    transport_cost = 0
    support = 0
    living_cost = 0
    schools: list[str] = []

    for stage in resolved_stages(plan):
        period = _standard_stage(plan, stage.stage)
        if not period.start_age <= child_age < period.start_age + period.years:
            continue
        if stage.cost_mode == "個別":
            if stage.annual_cost is None:
                raise ValueError(f"{stage.stage}の個別年間費用を入力してください")
            annual_base = stage.annual_cost
        else:
            annual_base = annual_education_cost(
                store,
                date,
                f"{stage.stage}.{stage.school_type}",
            )
        annual_total = int(
            (
                annual_base
                + stage.annual_material_cost
                + stage.annual_transport_cost
                + stage.annual_other_cost
            )
            * raise_factor
        )
        monthly_support = int(stage.annual_support * raise_factor) // 12
        school_cost += annual_total // 12
        material_cost += int(stage.annual_material_cost * raise_factor) // 12
        transport_cost += int(stage.annual_transport_cost * raise_factor) // 12
        support += monthly_support
        if stage.living_arrangement == "一人暮らし":
            living_cost += int(stage.monthly_living_cost * raise_factor)
        if child_age == period.start_age and date.month == 4:
            admission_fee += int(stage.admission_fee * raise_factor)
        schools.append(f"{stage.stage}.{stage.school_type}")

    if (
        plan.include_lessons
        and plan.lessons_start_age <= child_age <= plan.lessons_end_age
    ):
        lessons = plan.lessons_monthly_amount
        if lessons is None:
            lessons = annual_education_cost(store, date, "習い事") // 12
        lessons_cost += int(lessons * raise_factor)
        schools.append("習い事")
    if (
        plan.cram_start_age <= child_age <= plan.cram_end_age
        and plan.cram_monthly_amount is not None
    ):
        lessons_cost += int(plan.cram_monthly_amount * raise_factor)
        schools.append("塾")

    total = max(0, school_cost + lessons_cost + admission_fee + living_cost - support)
    return EducationCostBreakdown(
        total=total,
        school_cost=school_cost,
        lessons_cost=lessons_cost,
        admission_fee=admission_fee,
        material_cost=material_cost,
        transport_cost=transport_cost,
        support=support,
        living_cost=living_cost,
        schools=schools,
    )


def monthly_education_costs(
    store: ParameterStore,
    date: datetime.date,
    child_age: int,
    path: str = "公立",
    *,
    plan: EducationPlan | None = None,
    base_year: int | None = None,
) -> tuple[int, list[str]]:
    """指定年齢の子の月額教育費と内訳を返す.

    旧形式の呼び出しでは公立/私立パスをそのまま計算する。
    """
    selected_plan = plan or EducationPlan(
        id="_",
        member_id="_",
        path=path if path in STANDARD_PATHS else "公立",
    )
    breakdown = education_cost_breakdown(
        store,
        date,
        child_age,
        selected_plan,
        base_year=base_year,
    )
    return breakdown.total, breakdown.schools
