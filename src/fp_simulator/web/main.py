"""FastAPIアプリケーションのエントリポイント."""

from __future__ import annotations

import contextlib
import datetime
import os
import pathlib
import uuid
from collections.abc import AsyncIterator

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from fp_simulator.db.database import (
    delete_household,
    delete_plan,
    assign_owner_to_unowned,
    get_household,
    get_plan,
    init_db,
    list_plans,
    list_households,
    save_household,
    save_plan_snapshot,
)
from fp_simulator.engine.models import (
    Account,
    EducationPlan,
    Expense,
    Household,
    IdecoPlan,
    Income,
    Insurance,
    Loan,
    Member,
    NisaPlan,
    PensionRecordInput,
    PlanAssumptions,
    Relationship,
    SocialInsuranceType,
)
from fp_simulator.parameters.loader import get_store
from fp_simulator.mcp_server.server import mcp as mcp_server
from fp_simulator.web.auth import McpAuthMiddleware, authenticated_email, iap_auth_required

BASE_DIR = pathlib.Path(__file__).resolve().parent


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """起動時にDB初期化とMCPセッションマネージャーを開始."""
    await init_db()
    legacy_owner_email = os.environ.get("FP_LEGACY_OWNER_EMAIL", "").strip()
    if legacy_owner_email:
        await assign_owner_to_unowned(legacy_owner_email)
    async with mcp_server.session_manager.run():
        yield


app = FastAPI(title="FPシミュレーター", version="0.1.0", lifespan=lifespan)

# MCPサーバーを /mcp にマウント(Streamable HTTP)
mcp_app = McpAuthMiddleware(mcp_server.streamable_http_app(streamable_http_path="/"))
app.mount("/mcp", mcp_app)

# 静的ファイル・テンプレート
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


@app.middleware("http")
async def enforce_web_auth(request: Request, call_next):
    """Require IAP identity and restrict household URLs when enabled."""
    if not iap_auth_required() or request.url.path == "/healthz" or request.url.path.startswith("/mcp"):
        return await call_next(request)

    email = authenticated_email(dict(request.headers))
    if email is None:
        return HTMLResponse("認証が必要です", status_code=401)

    parts = request.url.path.strip("/").split("/")
    if len(parts) >= 2 and parts[0] == "households" and parts[1] not in {"new"}:
        household = await get_household(parts[1])
        if household is None:
            return HTMLResponse("世帯が見つかりません", status_code=404)
        if household.owner_email != email:
            return HTMLResponse("この世帯へのアクセス権がありません", status_code=403)

    request.state.authenticated_email = email
    return await call_next(request)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Cloud Run用ヘルスチェック."""
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    """トップページ(世帯一覧)."""
    owner_email = getattr(request.state, "authenticated_email", None)
    households = await list_households(owner_email=owner_email)
    return templates.TemplateResponse(
        request, "index.html", {"title": "FPシミュレーター", "households": households}
    )


@app.get("/debug/parameters", response_class=HTMLResponse)
async def debug_parameters(request: Request) -> HTMLResponse:
    """パラメータローダーの動作確認用(開発用)."""
    store = get_store()
    today = datetime.date.today()
    snapshot = store.snapshot(today)
    return templates.TemplateResponse(
        request,
        "debug_parameters.html",
        {"title": "パラメータ確認", "snapshot": snapshot, "date": today.isoformat()},
    )


# --- 世帯の作成・編集 ---

@app.get("/households/new", response_class=HTMLResponse)
async def household_new(request: Request) -> HTMLResponse:
    """新規世帯の入力フォーム."""
    return templates.TemplateResponse(
        request, "household_form.html", {"title": "世帯の新規作成", "household": None}
    )


@app.post("/households/new")
async def household_create(
    request: Request,
    name: str = Form(...),
    base_year: int = Form(2026),
    base_month: int = Form(1),
) -> RedirectResponse:
    """世帯を作成してメンバー編集へ."""
    household = Household(
        id="",
        name=name,
        owner_email=getattr(request.state, "authenticated_email", None),
        assumptions=PlanAssumptions(base_year=base_year, base_month=base_month),
    )
    household = await save_household(household)
    return RedirectResponse(f"/households/{household.id}/members", status_code=303)


@app.get("/households/{household_id}/members", response_class=HTMLResponse)
async def members_edit(request: Request, household_id: str) -> HTMLResponse:
    """Q1: 家族設定."""
    household = await get_household(household_id)
    if household is None:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request,
        "wizard/members.html",
        {"title": "Q1 ご家族", "household": household, "relationships": list(Relationship), "active_q": "Q1"},
    )


@app.post("/households/{household_id}/members")
async def members_add(
    household_id: str,
    name: str = Form(...),
    relationship: Relationship = Form(...),
    birth_date: str = Form(...),
    gender: str = Form(""),
    life_expectancy_age: int = Form(90),
    prefecture: str = Form("東京都"),
) -> RedirectResponse:
    """家族メンバーを追加."""
    household = await get_household(household_id)
    if household is None:
        return RedirectResponse("/", status_code=303)
    household.members.append(
        Member(
            id=str(uuid.uuid4()),
            name=name,
            relationship=relationship,
            birth_date=datetime.date.fromisoformat(birth_date),
            gender=gender or None,
            life_expectancy_age=life_expectancy_age,
            prefecture=prefecture,
        )
    )
    await save_household(household)
    return RedirectResponse(f"/households/{household_id}/members", status_code=303)


@app.post("/households/{household_id}/members/{member_id}/delete")
async def members_delete(household_id: str, member_id: str) -> RedirectResponse:
    household = await get_household(household_id)
    if household is None:
        return RedirectResponse("/", status_code=303)
    household.members = [m for m in household.members if m.id != member_id]
    await save_household(household)
    return RedirectResponse(f"/households/{household_id}/members", status_code=303)


@app.get("/households/{household_id}/incomes", response_class=HTMLResponse)
async def incomes_edit(request: Request, household_id: str) -> HTMLResponse:
    """Q2: 収入設定."""
    household = await get_household(household_id)
    if household is None:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request,
        "wizard/incomes.html",
        {
            "title": "Q2 収入",
            "household": household,
            "si_types": list(SocialInsuranceType),
            "active_q": "Q2",
        },
    )


@app.post("/households/{household_id}/incomes")
async def incomes_add(
    household_id: str,
    member_id: str = Form(...),
    name: str = Form("給与"),
    social_insurance_type: SocialInsuranceType = Form(...),
    start_age: int = Form(0),
    end_age: int = Form(60),
    monthly_amount: int = Form(...),
    bonus_amount: int = Form(0),
    retirement_allowance: int = Form(0),
    retirement_age: int = Form(60),
) -> RedirectResponse:
    """収入を追加."""
    household = await get_household(household_id)
    if household is None:
        return RedirectResponse("/", status_code=303)
    household.incomes.append(
        Income(
            id=str(uuid.uuid4()),
            member_id=member_id,
            name=name,
            social_insurance_type=social_insurance_type,
            start_age=start_age,
            end_age=end_age,
            monthly_amount=monthly_amount,
            bonus_months=[6, 12] if bonus_amount > 0 else [],
            bonus_amount=bonus_amount,
            retirement_allowance=retirement_allowance,
            retirement_age=retirement_age,
        )
    )
    await save_household(household)
    return RedirectResponse(f"/households/{household_id}/incomes", status_code=303)


@app.post("/households/{household_id}/incomes/{income_id}/delete")
async def incomes_delete(household_id: str, income_id: str) -> RedirectResponse:
    household = await get_household(household_id)
    if household is None:
        return RedirectResponse("/", status_code=303)
    household.incomes = [i for i in household.incomes if i.id != income_id]
    await save_household(household)
    return RedirectResponse(f"/households/{household_id}/incomes", status_code=303)


@app.get("/households/{household_id}/pensions", response_class=HTMLResponse)
async def pensions_edit(request: Request, household_id: str) -> HTMLResponse:
    """Q3: 年金設定."""
    household = await get_household(household_id)
    if household is None:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request, "wizard/pensions.html", {"title": "Q3 年金", "household": household, "active_q": "Q3"}
    )


@app.post("/households/{household_id}/pensions")
async def pensions_add(
    household_id: str,
    member_id: str = Form(...),
    kokumin_months: int = Form(480),
    kousei_months: int = Form(0),
    avg_standard_remuneration: int = Form(0),
    start_age: int = Form(65),
) -> RedirectResponse:
    """年金加入記録を追加."""
    household = await get_household(household_id)
    if household is None:
        return RedirectResponse("/", status_code=303)
    household.pension_records.append(
        PensionRecordInput(
            member_id=member_id,
            kokumin_months=kokumin_months,
            kousei_months=kousei_months,
            avg_standard_remuneration=avg_standard_remuneration,
            kousei_months_after_2003_04=kousei_months,
            start_age=start_age,
        )
    )
    await save_household(household)
    return RedirectResponse(f"/households/{household_id}/pensions", status_code=303)


@app.get("/households/{household_id}/expenses", response_class=HTMLResponse)
async def expenses_edit(request: Request, household_id: str) -> HTMLResponse:
    """Q4: 生活費設定."""
    household = await get_household(household_id)
    if household is None:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request, "wizard/expenses.html", {"title": "Q4 生活費", "household": household, "active_q": "Q4"}
    )


@app.post("/households/{household_id}/expenses")
async def expenses_add(
    household_id: str,
    name: str = Form("生活費"),
    monthly_amount: int = Form(...),
    start_age: int = Form(0),
    end_age: int = Form(0),  # 0=生涯
) -> RedirectResponse:
    """支出を追加."""
    household = await get_household(household_id)
    if household is None:
        return RedirectResponse("/", status_code=303)
    household.expenses.append(
        Expense(
            id=str(uuid.uuid4()),
            name=name,
            monthly_amount=monthly_amount,
            start_age=start_age,
            end_age=end_age if end_age > 0 else None,
        )
    )
    await save_household(household)
    return RedirectResponse(f"/households/{household_id}/expenses", status_code=303)


@app.get("/households/{household_id}/accounts", response_class=HTMLResponse)
async def accounts_edit(request: Request, household_id: str) -> HTMLResponse:
    """Q11: 貯蓄・資産設定."""
    household = await get_household(household_id)
    if household is None:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request, "wizard/accounts.html", {"title": "Q11 貯蓄・資産", "household": household, "active_q": "Q11"}
    )


@app.post("/households/{household_id}/accounts")
async def accounts_add(
    household_id: str,
    name: str = Form(...),
    balance: int = Form(...),
    interest_rate: float = Form(0.0),
) -> RedirectResponse:
    """口座を追加."""
    household = await get_household(household_id)
    if household is None:
        return RedirectResponse("/", status_code=303)
    household.accounts.append(
        Account(id=str(uuid.uuid4()), name=name, balance=balance, interest_rate=interest_rate)
    )
    await save_household(household)
    return RedirectResponse(f"/households/{household_id}/accounts", status_code=303)


@app.get("/households/{household_id}/loans", response_class=HTMLResponse)
async def loans_edit(request: Request, household_id: str) -> HTMLResponse:
    """Q9: ローン設定."""
    household = await get_household(household_id)
    if household is None:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request, "wizard/loans.html", {"title": "Q9 ローン", "household": household, "active_q": "Q9"}
    )


@app.post("/households/{household_id}/loans")
async def loans_add(
    household_id: str,
    member_id: str = Form(...),
    name: str = Form("住宅ローン"),
    principal: int = Form(...),
    annual_rate: float = Form(0.015),
    years: int = Form(35),
    repayment_type: str = Form("元利均等"),
    start_year: int = Form(2026),
    start_month: int = Form(1),
    bonus_amount: int = Form(0),
) -> RedirectResponse:
    """ローンを追加."""
    household = await get_household(household_id)
    if household is None:
        return RedirectResponse("/", status_code=303)
    household.loans.append(
        Loan(
            id=str(uuid.uuid4()),
            member_id=member_id,
            name=name,
            principal=principal,
            annual_rate=annual_rate,
            years=years,
            repayment_type=repayment_type,
            start_year=start_year,
            start_month=start_month,
            bonus_amount=bonus_amount,
            bonus_months=[6, 12] if bonus_amount > 0 else [],
        )
    )
    await save_household(household)
    return RedirectResponse(f"/households/{household_id}/loans", status_code=303)


@app.get("/households/{household_id}/education", response_class=HTMLResponse)
async def education_edit(request: Request, household_id: str) -> HTMLResponse:
    household = await get_household(household_id)
    if household is None:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request, "wizard/education.html", {"title": "Q5 教育費", "household": household, "active_q": "Q5"}
    )


@app.post("/households/{household_id}/education")
async def education_add(
    household_id: str,
    member_id: str = Form(...),
    path: str = Form("公立"),
    include_lessons: str = Form(""),
) -> RedirectResponse:
    household = await get_household(household_id)
    if household is None:
        return RedirectResponse("/", status_code=303)
    household.education_plans.append(
        EducationPlan(
            id=str(uuid.uuid4()),
            member_id=member_id,
            path=path,
            include_lessons=bool(include_lessons),
        )
    )
    await save_household(household)
    return RedirectResponse(f"/households/{household_id}/education", status_code=303)


@app.get("/households/{household_id}/insurance", response_class=HTMLResponse)
async def insurance_edit(request: Request, household_id: str) -> HTMLResponse:
    household = await get_household(household_id)
    if household is None:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request, "wizard/insurance.html", {"title": "Q10 保険", "household": household, "active_q": "Q10"}
    )


@app.post("/households/{household_id}/insurance")
async def insurance_add(
    household_id: str,
    name: str = Form(...),
    insured_member_id: str = Form(...),
    payer_member_id: str = Form(...),
    monthly_premium: int = Form(...),
    start_year: int = Form(2026),
    start_month: int = Form(1),
    end_year: int = Form(2060),
    end_month: int = Form(12),
    death_benefit: int = Form(0),
) -> RedirectResponse:
    household = await get_household(household_id)
    if household is None:
        return RedirectResponse("/", status_code=303)
    household.insurances.append(
        Insurance(
            id=str(uuid.uuid4()),
            name=name,
            insured_member_id=insured_member_id,
            payer_member_id=payer_member_id,
            monthly_premium=monthly_premium,
            start_year=start_year,
            start_month=start_month,
            end_year=end_year,
            end_month=end_month,
            death_benefit=death_benefit,
        )
    )
    await save_household(household)
    return RedirectResponse(f"/households/{household_id}/insurance", status_code=303)


@app.post("/households/{household_id}/ideco")
async def ideco_add(
    household_id: str,
    member_id: str = Form(...),
    initial_balance: int = Form(0),
    monthly_contribution: int = Form(23000),
    annual_return_rate: float = Form(0.0),
) -> RedirectResponse:
    household = await get_household(household_id)
    if household is None:
        return RedirectResponse("/", status_code=303)
    household.ideco_plans.append(
        IdecoPlan(
            id=str(uuid.uuid4()),
            member_id=member_id,
            initial_balance=initial_balance,
            monthly_contribution=monthly_contribution,
            annual_return_rate=annual_return_rate,
        )
    )
    await save_household(household)
    return RedirectResponse(f"/households/{household_id}/accounts", status_code=303)


@app.post("/households/{household_id}/nisa")
async def nisa_add(
    household_id: str,
    member_id: str = Form(...),
    initial_balance: int = Form(0),
    monthly_investment: int = Form(0),
    annual_return_rate: float = Form(0.0),
) -> RedirectResponse:
    household = await get_household(household_id)
    if household is None:
        return RedirectResponse("/", status_code=303)
    household.nisa_plans.append(
        NisaPlan(
            id=str(uuid.uuid4()),
            member_id=member_id,
            initial_balance=initial_balance,
            monthly_investment=monthly_investment,
            annual_return_rate=annual_return_rate,
        )
    )
    await save_household(household)
    return RedirectResponse(f"/households/{household_id}/accounts", status_code=303)


@app.post("/households/{household_id}/delete")
async def household_delete(household_id: str) -> RedirectResponse:
    await delete_household(household_id)
    return RedirectResponse("/", status_code=303)


@app.get("/households/{household_id}/plans", response_class=HTMLResponse)
async def plans_list(request: Request, household_id: str) -> HTMLResponse:
    """保存プランの一覧."""
    household = await get_household(household_id)
    if household is None:
        return RedirectResponse("/", status_code=303)
    plans = await list_plans(household_id)
    return templates.TemplateResponse(
        request,
        "plans.html",
        {"title": "保存プラン", "household": household, "plans": plans, "active_q": "plans"},
    )


@app.post("/households/{household_id}/plans")
async def plan_save(
    household_id: str,
    name: str = Form("保存プラン"),
) -> RedirectResponse:
    """現在の世帯状態を保存プランとして追加."""
    household = await get_household(household_id)
    if household is None:
        return RedirectResponse("/", status_code=303)
    await save_plan_snapshot(household_id, household, name)
    return RedirectResponse(f"/households/{household_id}/plans", status_code=303)


@app.post("/households/{household_id}/plans/{plan_id}/copy")
async def plan_copy(
    household_id: str,
    plan_id: str,
    name: str = Form("コピー"),
) -> RedirectResponse:
    """保存済みプランを複製."""
    household = await get_household(household_id)
    source = await get_plan(household_id, plan_id)
    if household is None or source is None:
        return RedirectResponse(f"/households/{household_id}/plans", status_code=303)
    await save_plan_snapshot(household_id, source, name, parent_plan_id=plan_id)
    return RedirectResponse(f"/households/{household_id}/plans", status_code=303)


@app.post("/households/{household_id}/plans/{plan_id}/restore")
async def plan_restore(household_id: str, plan_id: str) -> RedirectResponse:
    """保存プランを現在の世帯へ復元."""
    current = await get_household(household_id)
    snapshot = await get_plan(household_id, plan_id)
    if current is None or snapshot is None:
        return RedirectResponse(f"/households/{household_id}/plans", status_code=303)
    snapshot.id = current.id
    snapshot.owner_email = current.owner_email
    await save_household(snapshot)
    return RedirectResponse(f"/households/{household_id}/plans", status_code=303)


@app.post("/households/{household_id}/plans/{plan_id}/delete")
async def plan_delete(household_id: str, plan_id: str) -> RedirectResponse:
    """保存済みプランを削除."""
    await delete_plan(household_id, plan_id)
    return RedirectResponse(f"/households/{household_id}/plans", status_code=303)


# --- シミュレーション結果 ---

def _yearly_summary(monthly) -> list[dict]:
    """月次結果を年次比較用のサマリーへ集計."""
    yearly: dict[int, dict] = {}
    for month in monthly:
        summary = yearly.setdefault(
            month.date.year,
            {
                "year": month.date.year,
                "age": month.age,
                "income": 0,
                "expense": 0,
                "tax_si": 0,
                "net": 0,
                "balance_end": 0,
                "ideco_balance_end": 0,
                "nisa_balance_end": 0,
                "total_assets_end": 0,
            },
        )
        summary["income"] += month.total_income
        summary["expense"] += month.total_expense
        summary["tax_si"] += month.total_tax_si
        summary["net"] += month.net
        summary["balance_end"] = month.balance
        summary["ideco_balance_end"] = month.ideco_balance
        summary["nisa_balance_end"] = month.nisa_balance
        summary["total_assets_end"] = month.total_assets
        summary["age"] = month.age
    return sorted(yearly.values(), key=lambda item: item["year"])


@app.get("/households/{household_id}/simulate", response_class=HTMLResponse)
async def simulate_result(
    request: Request, household_id: str, plan_id: str | None = None
) -> HTMLResponse:
    """シミュレーション実行・結果表示."""
    household = await get_household(household_id)
    if household is None:
        return RedirectResponse("/", status_code=303)
    selected_plan = await get_plan(household_id, plan_id) if plan_id else None
    if plan_id and selected_plan is None:
        return RedirectResponse(f"/households/{household_id}/plans", status_code=303)
    simulation_household = selected_plan or household

    store = get_store()
    from fp_simulator.engine.cashflow import simulate

    result = simulate(store, simulation_household)

    yearly_list = _yearly_summary(result.monthly)

    # グラフ用データ
    labels = [f"{m.date.year}/{m.date.month}" for m in result.monthly]
    balances = [m.balance for m in result.monthly]
    ideco_balances = [m.ideco_balance for m in result.monthly]
    nisa_balances = [m.nisa_balance for m in result.monthly]
    total_assets = [m.total_assets for m in result.monthly]

    # サマリー指標
    min_balance = min(balances) if balances else 0
    min_balance_month = result.monthly[balances.index(min_balance)].date if balances else None
    final_balance = balances[-1] if balances else 0

    return templates.TemplateResponse(
        request,
        "result.html",
        {
            "title": f"シミュレーション結果 — {simulation_household.name}",
            "household": household,
            "simulation_household": simulation_household,
            "plan_id": plan_id,
            "yearly": yearly_list,
            "labels": labels,
            "balances": balances,
            "min_balance": min_balance,
            "min_balance_month": min_balance_month,
            "final_balance": final_balance,
            "final_assets": total_assets[-1] if total_assets else 0,
            "ideco_balances": ideco_balances,
            "nisa_balances": nisa_balances,
            "total_assets": total_assets,
            "active_q": "sim",
        },
    )


@app.get("/households/{household_id}/compare", response_class=HTMLResponse)
async def compare_plans(
    request: Request,
    household_id: str,
    plan_a_id: str | None = None,
    plan_b_id: str | None = None,
    alternative_name: str = "変更プラン",
    alternative_inflation_rate: float = 0.0,
    alternative_investment_return_rate: float = 0.0,
) -> HTMLResponse:
    """保存済みプラン、または現行プランと変更条件を並べて比較."""
    household = await get_household(household_id)
    if household is None:
        return RedirectResponse("/", status_code=303)

    from fp_simulator.engine.cashflow import simulate

    saved_plans = await list_plans(household_id)
    selected_a = await get_plan(household_id, plan_a_id) if plan_a_id else None
    selected_b = await get_plan(household_id, plan_b_id) if plan_b_id else None
    if (plan_a_id and selected_a is None) or (plan_b_id and selected_b is None):
        return RedirectResponse(f"/households/{household_id}/compare", status_code=303)

    baseline = selected_a or household
    alternative = selected_b
    if alternative is None:
        alternative = household.model_copy(deep=True)
        alternative.name = alternative_name.strip() or "変更プラン"
        alternative.assumptions.inflation_rate = alternative_inflation_rate
        alternative.assumptions.investment_return_rate = alternative_investment_return_rate

    baseline_result = simulate(get_store(), baseline)
    alternative_result = simulate(get_store(), alternative)

    def metrics(result) -> dict:
        balances = [month.balance for month in result.monthly]
        return {
            "min_balance": min(balances) if balances else 0,
            "final_balance": balances[-1] if balances else 0,
            "min_balance_month": (
                result.monthly[balances.index(min(balances))].date if balances else None
            ),
            "yearly": _yearly_summary(result.monthly),
        }

    baseline_metrics = metrics(baseline_result)
    alternative_metrics = metrics(alternative_result)
    years = [
        {
            "year": baseline["year"],
            "baseline": baseline,
            "alternative": alternative,
        }
        for baseline, alternative in zip(
            baseline_metrics["yearly"], alternative_metrics["yearly"], strict=True
        )
    ]

    return templates.TemplateResponse(
        request,
        "compare.html",
        {
            "title": "プラン比較",
            "household": household,
            "saved_plans": saved_plans,
            "plan_a_id": plan_a_id or "",
            "plan_b_id": plan_b_id or "",
            "baseline": {
                "name": baseline.name,
                "inflation_rate": baseline.assumptions.inflation_rate,
                "investment_return_rate": baseline.assumptions.investment_return_rate,
                **baseline_metrics,
            },
            "alternative": {
                "name": alternative.name,
                "inflation_rate": alternative.assumptions.inflation_rate,
                "investment_return_rate": alternative.assumptions.investment_return_rate,
                **alternative_metrics,
            },
            "years": years,
            "active_q": "compare",
        },
    )


@app.get("/households/{household_id}/disaster", response_class=HTMLResponse)
async def disaster_scenarios(
    request: Request,
    household_id: str,
    deceased_member_id: str | None = None,
    death_age: int | None = None,
) -> HTMLResponse:
    """生きる道と指定メンバー死亡シナリオを比較."""
    household = await get_household(household_id)
    if household is None:
        return RedirectResponse("/", status_code=303)

    from fp_simulator.engine.cashflow import DisasterScenario, simulate

    selected_member = next(
        (member for member in household.members if member.id == deceased_member_id),
        None,
    )
    scenario_result = None
    baseline_result = None
    if selected_member and death_age is not None:
        if death_age < 0 or death_age > 120:
            return HTMLResponse("死亡年齢は0〜120歳で指定してください", status_code=400)
        baseline_result = simulate(get_store(), household)
        scenario_result = simulate(
            get_store(),
            household,
            DisasterScenario(selected_member.id, death_age, f"{selected_member.name}万が一"),
        )

    def metrics(result) -> dict | None:
        if result is None:
            return None
        balances = [month.balance for month in result.monthly]
        return {
            "min_balance": min(balances) if balances else 0,
            "final_balance": balances[-1] if balances else 0,
            "min_balance_month": (
                result.monthly[balances.index(min(balances))].date if balances else None
            ),
            "yearly": _yearly_summary(result.monthly),
        }

    return templates.TemplateResponse(
        request,
        "disaster.html",
        {
            "title": "万が一シナリオ",
            "household": household,
            "members": household.members,
            "selected_member_id": deceased_member_id,
            "death_age": death_age,
            "baseline": metrics(baseline_result),
            "scenario": metrics(scenario_result),
            "active_q": "disaster",
        },
    )


@app.get("/households/{household_id}/simulate/monthly", response_class=HTMLResponse)
async def monthly_simulation_result(
    request: Request, household_id: str, year: int
) -> HTMLResponse:
    """指定年の月次キャッシュフローを表示."""
    household = await get_household(household_id)
    if household is None:
        return RedirectResponse("/", status_code=303)

    from fp_simulator.engine.cashflow import simulate

    result = simulate(get_store(), household)
    monthly = [m for m in result.monthly if m.date.year == year]
    if not monthly:
        return HTMLResponse("指定された年のシミュレーション結果がありません", status_code=404)

    return templates.TemplateResponse(
        request,
        "monthly_result.html",
        {
            "title": f"{year}年 月次キャッシュフロー",
            "household": household,
            "year": year,
            "monthly": monthly,
            "active_q": "sim",
        },
    )
