"""FastAPIアプリケーションのエントリポイント."""

from __future__ import annotations

import contextlib
import csv
import datetime
import io
import os
import pathlib
import uuid
import zipfile
from collections.abc import AsyncIterator
from xml.sax.saxutils import escape

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from fp_simulator.db.database import (
    add_audit_log,
    assign_owner_to_unowned,
    delete_household,
    delete_plan,
    get_household,
    get_plan,
    init_db,
    list_audit_logs,
    list_households,
    list_plans,
    save_household,
    save_plan_snapshot,
)
from fp_simulator.engine.insurance import InsurancePolicy, analyze_coverage
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
    OwnedHousingPlan,
    PensionRecordInput,
    PlanAssumptions,
    Relationship,
    SocialInsuranceType,
    Vehicle,
)
from fp_simulator.mcp_server.server import mcp as mcp_server
from fp_simulator.parameters.loader import ParameterStore, get_store
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


def _export_rows(result, granularity: str) -> list[list[object]]:
    """シミュレーション結果をCSV/Excel共通の行データへ変換."""
    if granularity == "yearly":
        yearly = _yearly_summary(result.monthly)
        rows: list[list[object]] = [[
            "年", "年齢", "収入", "乗り物売却", "支出", "住宅頭金", "固定資産税", "修繕費",
            "乗り物購入", "乗り物維持費", "乗り物税金・修繕", "車検", "税・社保", "収支",
            "現金・預金", "iDeCo", "NISA", "iDeCo受取", "NISA取崩", "金融資産合計",
        ]]
        rows.extend([
            [
                item["year"], item["age"], item["income"], item["vehicle_sale_income"], item["expense"],
                item["housing_down_payment"], item["property_tax"], item["repair_expense"],
                item["vehicle_purchase_expense"], item["vehicle_maintenance"],
                item["vehicle_tax_repair"], item["vehicle_inspection_expense"],
                item["tax_si"], item["net"], item["balance_end"],
                item["ideco_balance_end"], item["nisa_balance_end"],
                item["ideco_withdrawal"], item["nisa_withdrawal"],
                item["total_assets_end"],
            ]
            for item in yearly
        ])
        return rows

    rows = [[
        "年月", "年齢", "給与収入", "年金収入", "退職金", "その他収入",
        "死亡保険金", "乗り物売却", "iDeCo受取", "NISA取崩", "社会保険", "所得税", "住民税",
        "iDeCo受取時税", "生活費", "イベント支出",
        "住宅頭金", "固定資産税", "修繕費", "乗り物購入", "乗り物維持費", "乗り物税金・修繕", "車検",
        "ローン返済", "教育費", "保険料", "iDeCo掛金", "NISA投資",
        "収支", "現金・預金", "iDeCo残高", "NISA残高", "金融資産合計",
    ]]
    rows.extend([
        [
            month.date.isoformat(), month.age, month.salary_income,
            month.pension_income, month.retirement_income, month.other_income,
            month.death_benefit, month.vehicle_sale_income, month.ideco_withdrawal, month.nisa_withdrawal,
            month.social_insurance, month.income_tax, month.resident_tax,
            month.ideco_withdrawal_tax, month.living_expense, month.event_expense,
            month.housing_down_payment, month.property_tax, month.repair_expense,
            month.vehicle_purchase_expense, month.vehicle_maintenance,
            month.vehicle_tax_repair, month.vehicle_inspection_expense,
            month.loan_payment, month.education_expense, month.insurance_premium,
            month.ideco_contribution, month.nisa_investment, month.net,
            month.balance, month.ideco_balance, month.nisa_balance,
            month.total_assets,
        ]
        for month in result.monthly
    ])
    return rows


def _xlsx_bytes(rows: list[list[object]]) -> bytes:
    """外部依存なしで最小構成の.xlsxを生成."""
    def cell_ref(column: int, row: int) -> str:
        letters = ""
        while column:
            column, remainder = divmod(column - 1, 26)
            letters = chr(65 + remainder) + letters
        return f"{letters}{row}"

    sheet_rows = []
    for row_number, row in enumerate(rows, 1):
        cells = []
        for column_number, value in enumerate(row, 1):
            if isinstance(value, (int, float)):
                cells.append(
                    f'<c r="{cell_ref(column_number, row_number)}" t="n"><v>{value}</v></c>'
                )
            else:
                text = escape(str(value))
                cells.append(
                    f'<c r="{cell_ref(column_number, row_number)}" t="inlineStr">'
                    f"<is><t>{text}</t></is></c>"
                )
        sheet_rows.append(f'<row r="{row_number}">{"".join(cells)}</row>')

    files = {
        "[Content_Types].xml": """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>""",
        "_rels/.rels": """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>""",
        "xl/workbook.xml": """<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="キャッシュフロー" sheetId="1" r:id="rId1"/></sheets>
</workbook>""",
        "xl/_rels/workbook.xml.rels": """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>""",
        "xl/worksheets/sheet1.xml": (
            """<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<sheetData>""" + "".join(sheet_rows) + "</sheetData></worksheet>"
        ),
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content.encode("utf-8"))
    return output.getvalue()


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
    request: Request,
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
    await add_audit_log(
        household_id,
        getattr(request.state, "authenticated_email", None) or "web-user",
        "web",
        "household.member.add",
        details={"name": name, "relationship": str(relationship)},
    )
    return RedirectResponse(f"/households/{household_id}/members", status_code=303)


@app.post("/households/{household_id}/members/{member_id}/delete")
async def members_delete(request: Request, household_id: str, member_id: str) -> RedirectResponse:
    household = await get_household(household_id)
    if household is None:
        return RedirectResponse("/", status_code=303)
    removed = next((m for m in household.members if m.id == member_id), None)
    household.members = [m for m in household.members if m.id != member_id]
    await save_household(household)
    await add_audit_log(
        household_id,
        getattr(request.state, "authenticated_email", None) or "web-user",
        "web",
        "household.member.delete",
        member_id,
        {"name": removed.name} if removed else {},
    )
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
    request: Request,
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
    await add_audit_log(
        household_id,
        getattr(request.state, "authenticated_email", None) or "web-user",
        "web",
        "household.income.add",
        details={"name": name, "monthly_amount": monthly_amount},
    )
    return RedirectResponse(f"/households/{household_id}/incomes", status_code=303)


@app.post("/households/{household_id}/incomes/{income_id}/delete")
async def incomes_delete(request: Request, household_id: str, income_id: str) -> RedirectResponse:
    household = await get_household(household_id)
    if household is None:
        return RedirectResponse("/", status_code=303)
    removed = next((i for i in household.incomes if i.id == income_id), None)
    household.incomes = [i for i in household.incomes if i.id != income_id]
    await save_household(household)
    await add_audit_log(
        household_id,
        getattr(request.state, "authenticated_email", None) or "web-user",
        "web",
        "household.income.delete",
        income_id,
        {"name": removed.name} if removed else {},
    )
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
    request: Request,
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
    await add_audit_log(
        household_id,
        getattr(request.state, "authenticated_email", None) or "web-user",
        "web",
        "household.pension.add",
        details={"kokumin_months": kokumin_months, "kousei_months": kousei_months},
    )
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
    request: Request,
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
    await add_audit_log(
        household_id,
        getattr(request.state, "authenticated_email", None) or "web-user",
        "web",
        "household.expense.add",
        details={"name": name, "monthly_amount": monthly_amount},
    )
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
    request: Request,
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
    await add_audit_log(
        household_id,
        getattr(request.state, "authenticated_email", None) or "web-user",
        "web",
        "household.account.add",
        details={"name": name, "balance": balance},
    )
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
    request: Request,
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
    await add_audit_log(
        household_id,
        getattr(request.state, "authenticated_email", None) or "web-user",
        "web",
        "household.loan.add",
        details={"name": name, "principal": principal},
    )
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
    request: Request,
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
    await add_audit_log(
        household_id,
        getattr(request.state, "authenticated_email", None) or "web-user",
        "web",
        "household.education.add",
        details={"path": path},
    )
    return RedirectResponse(f"/households/{household_id}/education", status_code=303)


async def _wizard_placeholder(
    request: Request,
    household_id: str,
    q_number: str,
    page_title: str,
    description: str,
    notes: list[str],
    links: list[dict[str, str]],
    event_expenses: list | None = None,
) -> HTMLResponse:
    """未統合のFP-UNIV入力タブを明示的なMVP画面として表示."""
    household = await get_household(household_id)
    if household is None:
        return RedirectResponse("/", status_code=303)
    context = {
        "title": f"{q_number}. {page_title}",
        "household": household,
        "q_number": q_number,
        "page_title": page_title,
        "description": description,
        "notes": notes,
        "links": links,
        "active_q": q_number,
    }
    if event_expenses is not None:
        context["event_expenses"] = event_expenses
    return templates.TemplateResponse(request, "wizard/placeholder.html", context)


@app.get("/households/{household_id}/housing", response_class=HTMLResponse)
async def housing_edit(request: Request, household_id: str) -> HTMLResponse:
    """Q6: 住まい設定."""
    household = await get_household(household_id)
    if household is None:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request,
        "wizard/housing.html",
        {"title": "Q6 住まい", "household": household, "active_q": "Q6"},
    )


@app.post("/households/{household_id}/housing")
async def housing_save(
    request: Request,
    household_id: str,
    property_price: int = Form(...),
    down_payment: int = Form(0),
    purchase_year: int = Form(2026),
    purchase_month: int = Form(1),
    annual_property_tax: int = Form(0),
    annual_repair_cost: int = Form(0),
) -> Response:
    """所有住宅の設定を保存."""
    household = await get_household(household_id)
    if household is None:
        return RedirectResponse("/", status_code=303)
    if property_price < 0 or down_payment < 0:
        return Response("property_price and down_payment must not be negative", status_code=400)
    if down_payment > property_price:
        return Response("down_payment must not exceed property_price", status_code=400)
    if annual_property_tax < 0 or annual_repair_cost < 0:
        return Response("housing costs must not be negative", status_code=400)
    if not 1900 <= purchase_year <= 2200 or not 1 <= purchase_month <= 12:
        return Response("purchase date is invalid", status_code=400)
    household.owned_housing = OwnedHousingPlan(
        property_price=property_price,
        down_payment=down_payment,
        purchase_year=purchase_year,
        purchase_month=purchase_month,
        annual_property_tax=annual_property_tax,
        annual_repair_cost=annual_repair_cost,
    )
    await save_household(household)
    await add_audit_log(
        household_id,
        getattr(request.state, "authenticated_email", None) or "web-user",
        "web",
        "household.owned_housing.save",
        details={
            "property_price": property_price,
            "down_payment": down_payment,
            "purchase_year": purchase_year,
        },
    )
    return RedirectResponse(f"/households/{household_id}/housing", status_code=303)


@app.get("/households/{household_id}/vehicles", response_class=HTMLResponse)
async def vehicles_edit(request: Request, household_id: str) -> HTMLResponse:
    """Q7: 乗り物設定."""
    household = await get_household(household_id)
    if household is None:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request,
        "wizard/vehicles.html",
        {"title": "Q7 乗り物", "household": household, "active_q": "Q7"},
    )


@app.post("/households/{household_id}/vehicles")
async def vehicles_add(
    request: Request,
    household_id: str,
    name: str = Form("自動車"),
    vehicle_type: str = Form("新車"),
    ownership_start_year: int = Form(2026),
    ownership_start_month: int = Form(1),
    ownership_end_year: int = Form(2090),
    ownership_end_month: int = Form(12),
    purchase_price: int = Form(...),
    monthly_maintenance: int = Form(0),
    annual_tax_repair: int = Form(0),
    replacement_cycle_years: int = Form(0),
    sale_price: int = Form(0),
    inspection_cost: int = Form(0),
    inspection_cycle_years: int = Form(2),
    loan_id: str = Form(""),
    replacement_loan_principal: int = Form(0),
    replacement_loan_annual_rate: float = Form(0.0),
    replacement_loan_years: int = Form(0),
    replacement_loan_fee: int = Form(0),
    replacement_loan_repayment_type: str = Form("元利均等"),
) -> Response:
    """乗り物を追加."""
    household = await get_household(household_id)
    if household is None:
        return RedirectResponse("/", status_code=303)
    if vehicle_type not in {"新車", "中古車"}:
        return Response("vehicle_type is invalid", status_code=400)
    if loan_id and not any(loan.id == loan_id for loan in household.loans):
        return Response("loan_id does not exist", status_code=400)
    if any(
        value < 0
        for value in (
            purchase_price,
            monthly_maintenance,
            annual_tax_repair,
            replacement_cycle_years,
            sale_price,
            inspection_cost,
            replacement_loan_principal,
            replacement_loan_years,
            replacement_loan_fee,
        )
    ):
        return Response("vehicle amounts must not be negative", status_code=400)
    if replacement_loan_annual_rate < 0:
        return Response("replacement_loan_annual_rate must not be negative", status_code=400)
    if replacement_loan_repayment_type not in {"元利均等", "元金均等"}:
        return Response("replacement_loan_repayment_type is invalid", status_code=400)
    if replacement_loan_principal > 0 and replacement_loan_years <= 0:
        return Response("replacement_loan_years is required when replacement loan is used", status_code=400)
    if not 1900 <= ownership_start_year <= 2200 or not 1900 <= ownership_end_year <= 2200:
        return Response("ownership year is invalid", status_code=400)
    if not 1 <= ownership_start_month <= 12 or not 1 <= ownership_end_month <= 12:
        return Response("ownership month is invalid", status_code=400)
    if not 1 <= inspection_cycle_years <= 10:
        return Response("inspection_cycle_years is invalid", status_code=400)
    if ownership_end_year < ownership_start_year or (
        ownership_end_year == ownership_start_year
        and ownership_end_month < ownership_start_month
    ):
        return Response("ownership_end must not precede ownership_start", status_code=400)
    household.vehicles.append(
        Vehicle(
            id=str(uuid.uuid4()),
            name=name,
            vehicle_type=vehicle_type,
            ownership_start_year=ownership_start_year,
            ownership_start_month=ownership_start_month,
            ownership_end_year=ownership_end_year,
            ownership_end_month=ownership_end_month,
            purchase_price=purchase_price,
            monthly_maintenance=monthly_maintenance,
            annual_tax_repair=annual_tax_repair,
            replacement_cycle_years=replacement_cycle_years,
            sale_price=sale_price,
            inspection_cost=inspection_cost,
            inspection_cycle_years=inspection_cycle_years,
            loan_id=loan_id or None,
            replacement_loan_principal=replacement_loan_principal,
            replacement_loan_annual_rate=replacement_loan_annual_rate,
            replacement_loan_years=replacement_loan_years,
            replacement_loan_fee=replacement_loan_fee,
            replacement_loan_repayment_type=replacement_loan_repayment_type,
        )
    )
    await save_household(household)
    await add_audit_log(
        household_id,
        getattr(request.state, "authenticated_email", None) or "web-user",
        "web",
        "household.vehicle.add",
        details={"name": name, "purchase_price": purchase_price},
    )
    return RedirectResponse(f"/households/{household_id}/vehicles", status_code=303)


@app.post("/households/{household_id}/vehicles/{vehicle_id}/delete")
async def vehicles_delete(request: Request, household_id: str, vehicle_id: str) -> RedirectResponse:
    """乗り物を削除."""
    household = await get_household(household_id)
    if household is None:
        return RedirectResponse("/", status_code=303)
    removed = next((vehicle for vehicle in household.vehicles if vehicle.id == vehicle_id), None)
    household.vehicles = [vehicle for vehicle in household.vehicles if vehicle.id != vehicle_id]
    await save_household(household)
    await add_audit_log(
        household_id,
        getattr(request.state, "authenticated_email", None) or "web-user",
        "web",
        "household.vehicle.delete",
        vehicle_id,
        {"name": removed.name} if removed else {},
    )
    return RedirectResponse(f"/households/{household_id}/vehicles", status_code=303)


@app.get("/households/{household_id}/events", response_class=HTMLResponse)
async def events_edit(request: Request, household_id: str) -> HTMLResponse:
    """Q8: ライフイベント設定."""
    household = await get_household(household_id)
    if household is None:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request,
        "wizard/events.html",
        {
            "title": "Q8 ライフイベント",
            "household": household,
            "active_q": "Q8",
            "event_expenses": [expense for expense in household.expenses if expense.event_type != "生活費"],
        },
    )


@app.post("/households/{household_id}/events")
async def events_add(
    request: Request,
    household_id: str,
    event_type: str = Form("汎用"),
    name: str = Form("ライフイベント"),
    member_id: str = Form(""),
    monthly_amount: int = Form(...),
    cycle: str = Form("once"),
    yearly_month: int = Form(1),
    start_age: int = Form(0),
    start_month: int = Form(1),
    end_age: int = Form(0),
    end_month: int = Form(12),
    start_date_raw: str = Form("", alias="start_date"),
    end_date_raw: str = Form("", alias="end_date"),
    annual_raise_rate: float = Form(0.0),
    disaster_amount_raw: str = Form(""),
) -> Response:
    """Q8のライフイベントを追加."""
    household = await get_household(household_id)
    if household is None:
        return RedirectResponse("/", status_code=303)
    if event_type not in {"汎用", "結婚援助", "葬儀費"}:
        return Response("event_type is invalid", status_code=400)
    if cycle not in {"monthly", "yearly", "once"}:
        return Response("cycle is invalid", status_code=400)
    if member_id and not any(member.id == member_id for member in household.members):
        return Response("member_id does not exist", status_code=400)
    if annual_raise_rate < -1:
        return Response("annual_raise_rate is invalid", status_code=400)
    try:
        disaster_amount = int(disaster_amount_raw) if disaster_amount_raw.strip() else None
    except ValueError:
        return Response("disaster_amount is invalid", status_code=400)
    try:
        start_date = (
            datetime.date.fromisoformat(start_date_raw) if start_date_raw.strip() else None
        )
        end_date = datetime.date.fromisoformat(end_date_raw) if end_date_raw.strip() else None
    except ValueError:
        return Response("event date is invalid", status_code=400)
    if start_date and end_date and end_date < start_date:
        return Response("end_date must not precede start_date", status_code=400)
    if monthly_amount < 0 or (disaster_amount is not None and disaster_amount < 0):
        return Response("event amounts must not be negative", status_code=400)
    if not 0 <= start_age <= 120 or not 0 <= end_age <= 120:
        return Response("event age is invalid", status_code=400)
    if end_age and end_age < start_age:
        return Response("end_age must not precede start_age", status_code=400)
    if not 1 <= start_month <= 12 or not 1 <= end_month <= 12 or not 1 <= yearly_month <= 12:
        return Response("event month is invalid", status_code=400)
    household.expenses.append(
        Expense(
            id=str(uuid.uuid4()),
            name=name.strip() or "ライフイベント",
            event_type=event_type,
            member_id=member_id or None,
            monthly_amount=monthly_amount,
            cycle=cycle,
            yearly_month=yearly_month,
            start_age=start_age,
            start_month=start_month,
            end_age=end_age if end_age > 0 else None,
            end_month=end_month,
            start_date=start_date,
            end_date=end_date,
            annual_raise_rate=annual_raise_rate,
            disaster_amount=disaster_amount,
        )
    )
    await save_household(household)
    await add_audit_log(
        household_id,
        getattr(request.state, "authenticated_email", None) or "web-user",
        "web",
        "household.event.add",
        details={"event_type": event_type, "name": name, "cycle": cycle},
    )
    return RedirectResponse(f"/households/{household_id}/events", status_code=303)


@app.post("/households/{household_id}/events/{expense_id}/delete")
async def events_delete(request: Request, household_id: str, expense_id: str) -> RedirectResponse:
    """Q8のライフイベントを削除."""
    household = await get_household(household_id)
    if household is None:
        return RedirectResponse("/", status_code=303)
    removed = next(
        (expense for expense in household.expenses if expense.id == expense_id),
        None,
    )
    household.expenses = [
        expense
        for expense in household.expenses
        if expense.id != expense_id or expense.event_type == "生活費"
    ]
    await save_household(household)
    await add_audit_log(
        household_id,
        getattr(request.state, "authenticated_email", None) or "web-user",
        "web",
        "household.event.delete",
        expense_id,
        {"name": removed.name} if removed else {},
    )
    return RedirectResponse(f"/households/{household_id}/events", status_code=303)


@app.get("/households/{household_id}/insurance", response_class=HTMLResponse)
async def insurance_edit(request: Request, household_id: str) -> Response:
    household = await get_household(household_id)
    if household is None:
        return RedirectResponse("/", status_code=303)
    policies = [
        InsurancePolicy(
            name=insurance.name,
            insurance_type=insurance.insurance_type,
            insured_member_id=insurance.insured_member_id,
            payer_member_id=insurance.payer_member_id,
            monthly_premium=insurance.monthly_premium,
            start_date=datetime.date(insurance.start_year, insurance.start_month, 1),
            end_date=datetime.date(insurance.end_year, insurance.end_month, 1),
            death_benefit=insurance.death_benefit,
            surrender_value_rate=insurance.surrender_value_rate,
        )
        for insurance in household.insurances
    ]
    analysis = analyze_coverage(
        policies,
        datetime.date(household.assumptions.base_year, household.assumptions.base_month, 1),
    )
    return templates.TemplateResponse(
        request,
        "wizard/insurance.html",
        {
            "title": "Q10 保険",
            "household": household,
            "active_q": "Q10",
            "insurance_analysis": analysis,
        },
    )


@app.post("/households/{household_id}/insurance")
async def insurance_add(
    request: Request,
    household_id: str,
    name: str = Form(...),
    insurance_type: str = Form("死亡保障"),
    insured_member_id: str = Form(...),
    payer_member_id: str = Form(...),
    monthly_premium: int = Form(...),
    start_year: int = Form(2026),
    start_month: int = Form(1),
    end_year: int = Form(2060),
    end_month: int = Form(12),
    death_benefit: int = Form(0),
    surrender_value_rate: float = Form(0.0),
) -> Response:
    household = await get_household(household_id)
    if household is None:
        return RedirectResponse("/", status_code=303)
    if insurance_type not in {"死亡保障", "医療", "就業不能", "個人年金"}:
        return Response("insurance_type is invalid", status_code=400)
    if not any(member.id == insured_member_id for member in household.members):
        return Response("insured_member_id does not exist", status_code=400)
    if not any(member.id == payer_member_id for member in household.members):
        return Response("payer_member_id does not exist", status_code=400)
    if monthly_premium < 0 or death_benefit < 0:
        return Response("insurance amounts must not be negative", status_code=400)
    if not 0 <= surrender_value_rate <= 1:
        return Response("surrender_value_rate must be between 0 and 1", status_code=400)
    if not 1900 <= start_year <= 2200 or not 1900 <= end_year <= 2200:
        return Response("insurance year is invalid", status_code=400)
    if not 1 <= start_month <= 12 or not 1 <= end_month <= 12:
        return Response("insurance month is invalid", status_code=400)
    start_date = datetime.date(start_year, start_month, 1)
    end_date = datetime.date(end_year, end_month, 1)
    if end_date < start_date:
        return Response("insurance end must not precede start", status_code=400)
    household.insurances.append(
        Insurance(
            id=str(uuid.uuid4()),
            name=name.strip() or "保険",
            insurance_type=insurance_type,
            insured_member_id=insured_member_id,
            payer_member_id=payer_member_id,
            monthly_premium=monthly_premium,
            start_year=start_year,
            start_month=start_month,
            end_year=end_year,
            end_month=end_month,
            death_benefit=death_benefit,
            surrender_value_rate=surrender_value_rate,
        )
    )
    await save_household(household)
    await add_audit_log(
        household_id,
        getattr(request.state, "authenticated_email", None) or "web-user",
        "web",
        "household.insurance.add",
        details={"name": name, "monthly_premium": monthly_premium},
    )
    return RedirectResponse(f"/households/{household_id}/insurance", status_code=303)


@app.post("/households/{household_id}/insurance/{insurance_id}/delete")
async def insurance_delete(
    request: Request, household_id: str, insurance_id: str
) -> RedirectResponse:
    """保険を削除."""
    household = await get_household(household_id)
    if household is None:
        return RedirectResponse("/", status_code=303)
    removed = next(
        (insurance for insurance in household.insurances if insurance.id == insurance_id),
        None,
    )
    household.insurances = [
        insurance for insurance in household.insurances if insurance.id != insurance_id
    ]
    await save_household(household)
    await add_audit_log(
        household_id,
        getattr(request.state, "authenticated_email", None) or "web-user",
        "web",
        "household.insurance.delete",
        insurance_id,
        {"name": removed.name} if removed else {},
    )
    return RedirectResponse(f"/households/{household_id}/insurance", status_code=303)


@app.post("/households/{household_id}/ideco")
async def ideco_add(
    request: Request,
    household_id: str,
    member_id: str = Form(...),
    initial_balance: int = Form(0),
    monthly_contribution: int = Form(23000),
    receive_start_age: int = Form(65),
    monthly_withdrawal: int = Form(0),
    withdrawal_tax_rate: float = Form(0.0),
    annual_return_rate: float = Form(0.0),
) -> RedirectResponse:
    household = await get_household(household_id)
    if household is None:
        return RedirectResponse("/", status_code=303)
    if (
        initial_balance < 0
        or monthly_contribution < 0
        or monthly_withdrawal < 0
        or not 0 <= withdrawal_tax_rate <= 1
        or not 0 <= receive_start_age <= 120
    ):
        return HTMLResponse("iDeCoの入力値が不正です", status_code=400)
    household.ideco_plans.append(
        IdecoPlan(
            id=str(uuid.uuid4()),
            member_id=member_id,
            initial_balance=initial_balance,
            monthly_contribution=monthly_contribution,
            receive_start_age=receive_start_age,
            monthly_withdrawal=monthly_withdrawal,
            withdrawal_tax_rate=withdrawal_tax_rate,
            annual_return_rate=annual_return_rate,
        )
    )
    await save_household(household)
    await add_audit_log(
        household_id,
        getattr(request.state, "authenticated_email", None) or "web-user",
        "web",
        "household.ideco.add",
        details={"monthly_contribution": monthly_contribution},
    )
    return RedirectResponse(f"/households/{household_id}/accounts", status_code=303)


@app.post("/households/{household_id}/nisa")
async def nisa_add(
    request: Request,
    household_id: str,
    member_id: str = Form(...),
    initial_balance: int = Form(0),
    monthly_investment: int = Form(0),
    receive_start_age: int | None = Form(None),
    monthly_withdrawal: int = Form(0),
    annual_return_rate: float = Form(0.0),
) -> RedirectResponse:
    household = await get_household(household_id)
    if household is None:
        return RedirectResponse("/", status_code=303)
    if (
        initial_balance < 0
        or monthly_investment < 0
        or monthly_withdrawal < 0
        or (receive_start_age is not None and not 0 <= receive_start_age <= 120)
    ):
        return HTMLResponse("NISAの入力値が不正です", status_code=400)
    household.nisa_plans.append(
        NisaPlan(
            id=str(uuid.uuid4()),
            member_id=member_id,
            initial_balance=initial_balance,
            monthly_investment=monthly_investment,
            receive_start_age=receive_start_age,
            monthly_withdrawal=monthly_withdrawal,
            annual_return_rate=annual_return_rate,
        )
    )
    await save_household(household)
    await add_audit_log(
        household_id,
        getattr(request.state, "authenticated_email", None) or "web-user",
        "web",
        "household.nisa.add",
        details={"monthly_investment": monthly_investment},
    )
    return RedirectResponse(f"/households/{household_id}/accounts", status_code=303)


@app.post("/households/{household_id}/delete")
async def household_delete(request: Request, household_id: str) -> RedirectResponse:
    household = await get_household(household_id)
    if household is not None:
        await add_audit_log(
            household_id,
            getattr(request.state, "authenticated_email", None) or "web-user",
            "web",
            "household.delete",
            household_id,
            {"name": household.name},
        )
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
    request: Request,
    household_id: str,
    name: str = Form("保存プラン"),
) -> RedirectResponse:
    """現在の世帯状態を保存プランとして追加."""
    household = await get_household(household_id)
    if household is None:
        return RedirectResponse("/", status_code=303)
    plan = await save_plan_snapshot(household_id, household, name)
    await add_audit_log(
        household_id,
        getattr(request.state, "authenticated_email", None) or "web-user",
        "web",
        "plan.save",
        plan["id"],
        {"name": plan["name"]},
    )
    return RedirectResponse(f"/households/{household_id}/plans", status_code=303)


@app.post("/households/{household_id}/plans/{plan_id}/copy")
async def plan_copy(
    request: Request,
    household_id: str,
    plan_id: str,
    name: str = Form("コピー"),
) -> RedirectResponse:
    """保存済みプランを複製."""
    household = await get_household(household_id)
    source = await get_plan(household_id, plan_id)
    if household is None or source is None:
        return RedirectResponse(f"/households/{household_id}/plans", status_code=303)
    plan = await save_plan_snapshot(household_id, source, name, parent_plan_id=plan_id)
    await add_audit_log(
        household_id,
        getattr(request.state, "authenticated_email", None) or "web-user",
        "web",
        "plan.copy",
        plan["id"],
        {"source_plan_id": plan_id, "name": plan["name"]},
    )
    return RedirectResponse(f"/households/{household_id}/plans", status_code=303)


@app.post("/households/{household_id}/plans/{plan_id}/restore")
async def plan_restore(
    request: Request, household_id: str, plan_id: str
) -> RedirectResponse:
    """保存プランを現在の世帯へ復元."""
    current = await get_household(household_id)
    snapshot = await get_plan(household_id, plan_id)
    if current is None or snapshot is None:
        return RedirectResponse(f"/households/{household_id}/plans", status_code=303)
    snapshot.id = current.id
    snapshot.owner_email = current.owner_email
    await save_household(snapshot)
    await add_audit_log(
        household_id,
        getattr(request.state, "authenticated_email", None) or "web-user",
        "web",
        "plan.restore",
        plan_id,
        {"name": snapshot.name},
    )
    return RedirectResponse(f"/households/{household_id}/plans", status_code=303)


@app.post("/households/{household_id}/plans/{plan_id}/delete")
async def plan_delete(request: Request, household_id: str, plan_id: str) -> RedirectResponse:
    """保存済みプランを削除."""
    await delete_plan(household_id, plan_id)
    await add_audit_log(
        household_id,
        getattr(request.state, "authenticated_email", None) or "web-user",
        "web",
        "plan.delete",
        plan_id,
    )
    return RedirectResponse(f"/households/{household_id}/plans", status_code=303)


@app.get("/households/{household_id}/audit", response_class=HTMLResponse)
async def audit_history(request: Request, household_id: str) -> HTMLResponse:
    """世帯の変更監査ログ."""
    household = await get_household(household_id)
    if household is None:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request,
        "audit.html",
        {
            "title": "変更履歴",
            "household": household,
            "logs": await list_audit_logs(household_id),
            "active_q": "audit",
        },
    )


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
                "ideco_withdrawal": 0,
                "nisa_withdrawal": 0,
                "survivor_pension": 0,
                "child_allowance": 0,
                "expense": 0,
                "vehicle_sale_income": 0,
                "housing_down_payment": 0,
                "property_tax": 0,
                "repair_expense": 0,
                "vehicle_purchase_expense": 0,
                "vehicle_maintenance": 0,
                "vehicle_tax_repair": 0,
                "vehicle_inspection_expense": 0,
                "living_expense_reduction": 0,
                "tax_si": 0,
                "net": 0,
                "balance_end": 0,
                "ideco_balance_end": 0,
                "nisa_balance_end": 0,
                "total_assets_end": 0,
            },
        )
        summary["income"] += month.total_income
        summary["ideco_withdrawal"] += month.ideco_withdrawal
        summary["nisa_withdrawal"] += month.nisa_withdrawal
        summary["survivor_pension"] += month.survivor_pension
        summary["child_allowance"] += month.child_allowance
        summary["expense"] += month.total_expense
        summary["vehicle_sale_income"] += month.vehicle_sale_income
        summary["housing_down_payment"] += month.housing_down_payment
        summary["property_tax"] += month.property_tax
        summary["repair_expense"] += month.repair_expense
        summary["vehicle_purchase_expense"] += month.vehicle_purchase_expense
        summary["vehicle_maintenance"] += month.vehicle_maintenance
        summary["vehicle_tax_repair"] += month.vehicle_tax_repair
        summary["vehicle_inspection_expense"] += month.vehicle_inspection_expense
        summary["living_expense_reduction"] += next(
            (
                trace.amount
                for trace in month.traces
                if trace.item == "万が一時の生活費削減"
            ),
            0,
        )
        summary["tax_si"] += month.total_tax_si
        summary["net"] += month.net
        summary["balance_end"] = month.balance
        summary["ideco_balance_end"] = month.ideco_balance
        summary["nisa_balance_end"] = month.nisa_balance
        summary["total_assets_end"] = month.total_assets
        summary["age"] = month.age
    return sorted(yearly.values(), key=lambda item: item["year"])


_TRACE_PARAMETER_SOURCES: dict[str, tuple[tuple[str, str], ...]] = {
    "社会保険料": (
        ("日本年金機構: 厚生年金保険料率", "社会保険.厚生年金.料率"),
        ("協会けんぽ: 健康保険・介護保険料率", "社会保険.健康保険.料率"),
        ("厚生労働省: 雇用保険料率", "社会保険.雇用保険.労働者負担率"),
    ),
    "年金収入": (
        ("日本年金機構: 老齢基礎年金", "年金.老齢基礎年金.満額"),
        ("日本年金機構: 老齢厚生年金", "年金.老齢厚生年金.報酬比例乗率"),
    ),
    "遺族年金": (
        ("日本年金機構: 遺族基礎年金", "遺族基礎年金.本体.年額"),
        ("日本年金機構: 遺族厚生年金", "遺族厚生年金.報酬比例.支給率"),
    ),
    "所得税(源泉徴収)": (
        ("国税庁: 給与所得控除", "所得税.給与所得控除.速算表"),
        ("国税庁: 所得税率", "所得税.税率.速算表"),
        ("国税庁: 基礎控除", "所得税.基礎控除.控除額"),
    ),
    "所得税(年末調整)": (
        ("国税庁: 給与所得控除", "所得税.給与所得控除.速算表"),
        ("国税庁: 所得税率", "所得税.税率.速算表"),
        ("国税庁: 基礎控除", "所得税.基礎控除.控除額"),
    ),
    "住民税": (
        ("総務省: 個人住民税", "住民税.所得割.税率"),
        ("総務省: 住民税の徴収サイクル", "住民税.徴収サイクル"),
    ),
    "教育費": (
        ("文部科学省: 子供の学習費調査", "教育費.小学校.公立"),
        ("文部科学省: 大学の教育費", "教育費.大学.国立"),
    ),
    "iDeCo掛金": (
        ("iDeCo公式: 掛金上限", "iDeCo.掛金上限.第2号"),
    ),
    "iDeCo受取": (
        ("iDeCo公式: 受取制度", "iDeCo.受取開始年齢.最小"),
    ),
    "NISA投資": (
        ("金融庁: NISA制度", "NISA.年間投資上限"),
    ),
    "NISA取崩": (
        ("金融庁: NISA非課税保有限度額", "NISA.非課税保有限度額"),
    ),
}

_TRACE_DIRECT_SOURCES: dict[str, tuple[dict[str, str], ...]] = {
    "児童手当": (
        {
            "label": "こども家庭庁: 児童手当",
            "url": "https://www.cfa.go.jp/policies/kokoseido/jidouteate/",
        },
    ),
}


def _trace_source_links(
    store: ParameterStore, trace_item: str, date: datetime.date
) -> list[dict[str, str]]:
    """トレース項目に対応する公式出典リンクを返す."""
    links = list(_TRACE_DIRECT_SOURCES.get(trace_item, ()))
    available_paths = set(store.list_paths())
    seen_urls = {link["url"] for link in links}
    for label, path in _TRACE_PARAMETER_SOURCES.get(trace_item, ()):
        if path not in available_paths:
            continue
        source = store.get_source(path, date)
        if not source.startswith("https://") or source in seen_urls:
            continue
        links.append({"label": label, "url": source})
        seen_urls.add(source)
    return links


@app.get("/households/{household_id}/simulate", response_class=HTMLResponse)
async def simulate_result(
    request: Request,
    household_id: str,
    plan_id: str | None = None,
    display_range: str = "all",
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

    range_options = [
        {"value": "1", "label": "1年"},
        {"value": "3", "label": "3年"},
        {"value": "5", "label": "5年"},
        {"value": "10", "label": "10年"},
        {"value": "all", "label": "全期間"},
    ]
    valid_ranges = {option["value"] for option in range_options}
    if display_range not in valid_ranges:
        display_range = "all"

    display_monthly = result.monthly
    if display_range != "all" and result.monthly:
        display_end_year = result.monthly[0].date.year + int(display_range) - 1
        display_monthly = [
            month for month in result.monthly if month.date.year <= display_end_year
        ]
    yearly_list = _yearly_summary(display_monthly)

    # グラフ用データ
    labels = [f"{m.date.year}/{m.date.month}" for m in display_monthly]
    balances = [m.balance for m in display_monthly]
    ideco_balances = [m.ideco_balance for m in display_monthly]
    nisa_balances = [m.nisa_balance for m in display_monthly]
    display_total_assets = [m.total_assets for m in display_monthly]

    # サマリー指標
    all_balances = [m.balance for m in result.monthly]
    min_balance = min(all_balances) if all_balances else 0
    min_balance_month = (
        result.monthly[all_balances.index(min_balance)].date if all_balances else None
    )
    final_balance = all_balances[-1] if all_balances else 0

    # 生涯の支出内訳(円グラフ用・表示範囲に連動しない)
    lifetime_expense_categories = {
        "生活費": sum(m.living_expense for m in result.monthly),
        "教育費": sum(m.education_expense for m in result.monthly),
        "住宅関連": sum(
            m.housing_down_payment + m.property_tax + m.repair_expense
            for m in result.monthly
        ),
        "乗り物関連": sum(
            m.vehicle_purchase_expense
            + m.vehicle_maintenance
            + m.vehicle_tax_repair
            + m.vehicle_inspection_expense
            for m in result.monthly
        ),
        "ローン返済": sum(m.loan_payment for m in result.monthly),
        "保険料": sum(m.insurance_premium for m in result.monthly),
        "イベント": sum(m.event_expense for m in result.monthly),
        "税・社会保険": sum(m.total_tax_si for m in result.monthly),
        "iDeCo・NISA掛金": sum(
            m.ideco_contribution + m.nisa_investment for m in result.monthly
        ),
    }
    expense_breakdown_labels = [
        label for label, amount in lifetime_expense_categories.items() if amount > 0
    ]
    expense_breakdown_values = [
        amount for amount in lifetime_expense_categories.values() if amount > 0
    ]

    # 生涯の年次収入構成(積み上げ棒グラフ用・表示範囲に連動しない)
    income_years = sorted({m.date.year for m in result.monthly})
    income_by_year: dict[int, list] = {}
    for m in result.monthly:
        yearly_income = income_by_year.setdefault(m.date.year, [0, 0, 0, 0])
        yearly_income[0] += m.salary_income
        yearly_income[1] += m.pension_income + m.survivor_pension
        yearly_income[2] += m.retirement_income
        yearly_income[3] += (
            m.other_income
            + m.death_benefit
            + m.child_allowance
            + m.ideco_withdrawal
            + m.nisa_withdrawal
            + m.vehicle_sale_income
        )
    income_series_labels = ["給与", "年金", "退職金", "その他"]
    income_series_values = [
        [income_by_year[year][i] for year in income_years] for i in range(4)
    ]

    return templates.TemplateResponse(
        request,
        "result.html",
        {
            "title": f"シミュレーション結果 — {simulation_household.name}",
            "household": household,
            "simulation_household": simulation_household,
            "plan_id": plan_id,
            "display_range": display_range,
            "range_options": range_options,
            "yearly": yearly_list,
            "labels": labels,
            "balances": balances,
            "min_balance": min_balance,
            "min_balance_month": min_balance_month,
            "final_balance": final_balance,
            "final_assets": result.monthly[-1].total_assets if result.monthly else 0,
            "ideco_balances": ideco_balances,
            "nisa_balances": nisa_balances,
            "total_assets": display_total_assets,
            "expense_breakdown_labels": expense_breakdown_labels,
            "expense_breakdown_values": expense_breakdown_values,
            "income_years": income_years,
            "income_series_labels": income_series_labels,
            "income_series_values": income_series_values,
            "active_q": "sim",
        },
    )


async def _export_response(
    household_id: str,
    plan_id: str | None,
    granularity: str,
    excel: bool,
) -> Response:
    household = await get_household(household_id)
    if household is None:
        return Response("世帯が見つかりません", status_code=404)
    selected_plan = await get_plan(household_id, plan_id) if plan_id else None
    if plan_id and selected_plan is None:
        return Response("プランが見つかりません", status_code=404)

    from fp_simulator.engine.cashflow import simulate

    simulation_household = selected_plan or household
    rows = _export_rows(simulate(get_store(), simulation_household), granularity)
    suffix = "xlsx" if excel else "csv"
    filename = f"fp-simulator-{granularity}.{suffix}"
    if excel:
        content = _xlsx_bytes(rows)
        media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    else:
        csv_buffer = io.StringIO()
        csv.writer(csv_buffer, lineterminator="\r\n").writerows(rows)
        content = csv_buffer.getvalue().encode("utf-8-sig")
        media_type = "text/csv; charset=utf-8"
    return Response(
        content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/households/{household_id}/export.csv")
async def export_csv(
    household_id: str,
    plan_id: str | None = None,
    granularity: str = "yearly",
) -> Response:
    """キャッシュフローをCSVでダウンロード."""
    if granularity not in {"monthly", "yearly"}:
        return Response("granularity must be monthly or yearly", status_code=400)
    return await _export_response(household_id, plan_id, granularity, excel=False)


@app.get("/households/{household_id}/export.xlsx")
async def export_xlsx(
    household_id: str,
    plan_id: str | None = None,
    granularity: str = "yearly",
) -> Response:
    """キャッシュフローをExcelでダウンロード."""
    if granularity not in {"monthly", "yearly"}:
        return Response("granularity must be monthly or yearly", status_code=400)
    return await _export_response(household_id, plan_id, granularity, excel=True)


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
        negative_month = next(
            (month.date for month in result.monthly if month.balance < 0),
            None,
        )
        return {
            "min_balance": min(balances) if balances else 0,
            "final_balance": balances[-1] if balances else 0,
            "min_balance_month": (
                result.monthly[balances.index(min(balances))].date if balances else None
            ),
            "negative_start_month": negative_month,
            "yearly": _yearly_summary(result.monthly),
        }

    baseline_metrics = metrics(baseline_result)
    alternative_metrics = metrics(alternative_result)
    baseline_by_year = {item["year"]: item for item in baseline_metrics["yearly"]}
    alternative_by_year = {item["year"]: item for item in alternative_metrics["yearly"]}
    years = [
        {
            "year": year,
            "baseline": baseline_by_year.get(year),
            "alternative": alternative_by_year.get(year),
        }
        for year in sorted(set(baseline_by_year) | set(alternative_by_year))
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
            "deltas": {
                "min_balance": alternative_metrics["min_balance"] - baseline_metrics["min_balance"],
                "final_balance": alternative_metrics["final_balance"] - baseline_metrics["final_balance"],
                "negative_start_month": (
                    alternative_metrics["negative_start_month"]
                    if baseline_metrics["negative_start_month"] is None
                    else (
                        None
                        if alternative_metrics["negative_start_month"] is None
                        else (
                            alternative_metrics["negative_start_month"]
                            - baseline_metrics["negative_start_month"]
                        )
                    )
                ),
            },
            "active_q": "compare",
        },
    )


@app.get("/households/{household_id}/disaster", response_class=HTMLResponse)
async def disaster_scenarios(
    request: Request,
    household_id: str,
    deceased_member_id: str | None = None,
    death_age: int | None = None,
    survivor_pension_monthly: int | None = None,
    child_allowance_monthly: int | None = None,
    living_expense_reduction_rate: float = 0.0,
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
    store = get_store()
    scenario_result = None
    baseline_result = None
    if selected_member and death_age is not None:
        if (
            death_age < 0
            or death_age > 120
            or (
                survivor_pension_monthly is not None
                and survivor_pension_monthly < 0
            )
            or (
                child_allowance_monthly is not None
                and child_allowance_monthly < 0
            )
            or not 0 <= living_expense_reduction_rate <= 1
        ):
            return HTMLResponse("万が一シナリオの入力値が不正です", status_code=400)
        baseline_result = simulate(store, household)
        scenario_result = simulate(
            store,
            household,
            DisasterScenario(
                selected_member.id,
                death_age,
                f"{selected_member.name}万が一",
                survivor_pension_monthly=survivor_pension_monthly,
                child_allowance_monthly=child_allowance_monthly,
                living_expense_reduction_rate=living_expense_reduction_rate,
            ),
        )

    death_date = (
        datetime.date(
            selected_member.birth_date.year + death_age,
            selected_member.birth_date.month,
            1,
        )
        if selected_member and death_age is not None
        else None
    )

    def metrics(result, scenario_death_date: datetime.date | None) -> dict | None:
        if result is None:
            return None
        balances = [month.balance for month in result.monthly]
        yearly = _yearly_summary(result.monthly)
        post_death_balances = [
            month.balance
            for month in result.monthly
            if scenario_death_date is not None and month.date >= scenario_death_date
        ]
        pre_death_balances = [
            month.balance
            for month in result.monthly
            if scenario_death_date is not None and month.date < scenario_death_date
        ]
        survivor_trace = next(
            (
                trace
                for month in result.monthly
                for trace in month.traces
                if trace.item == "遺族年金"
            ),
            None,
        )
        post_death_min_balance = (
            min(post_death_balances) if post_death_balances else None
        )
        return {
            "min_balance": min(balances) if balances else 0,
            "final_balance": balances[-1] if balances else 0,
            "min_balance_month": (
                result.monthly[balances.index(min(balances))].date if balances else None
            ),
            "yearly": yearly,
            "yearly_by_year": {item["year"]: item for item in yearly},
            "post_death_min_balance": post_death_min_balance,
            "pre_death_min_balance": min(pre_death_balances) if pre_death_balances else None,
            "required_additional_coverage": (
                max(0, -post_death_min_balance)
                if post_death_min_balance is not None
                else 0
            ),
            "survivor_pension_basis": survivor_trace.basis if survivor_trace else None,
            "survivor_pension_sources": (
                _trace_source_links(store, "遺族年金", scenario_death_date)
                if survivor_trace and scenario_death_date is not None
                else []
            ),
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
            "survivor_pension_monthly": survivor_pension_monthly,
            "child_allowance_monthly": child_allowance_monthly,
            "living_expense_reduction_rate": living_expense_reduction_rate,
            "baseline": metrics(baseline_result, None),
            "scenario": metrics(scenario_result, death_date),
            "active_q": "disaster",
        },
    )


def _monthly_detail_items(month) -> list[tuple[str, int]]:
    """月次表の詳細列を展開表示するための非ゼロ内訳項目."""
    flow_items = [
        ("乗り物売却", month.vehicle_sale_income),
        ("住宅頭金", month.housing_down_payment),
        ("固定資産税", month.property_tax),
        ("修繕費", month.repair_expense),
        ("乗り物購入", month.vehicle_purchase_expense),
        ("乗り物維持費", month.vehicle_maintenance),
        ("乗り物税金・修繕", month.vehicle_tax_repair),
        ("車検", month.vehicle_inspection_expense),
        ("iDeCo受取", month.ideco_withdrawal),
        ("NISA取崩", month.nisa_withdrawal),
    ]
    details = [(label, amount) for label, amount in flow_items if amount != 0]
    if month.ideco_balance or month.nisa_balance:
        details.append(("iDeCo残高", month.ideco_balance))
        details.append(("NISA残高", month.nisa_balance))
        details.append(("金融資産合計", month.total_assets))
    return details


@app.get("/households/{household_id}/simulate/monthly", response_class=HTMLResponse)
async def monthly_simulation_result(
    request: Request, household_id: str, year: int, plan_id: str | None = None
) -> HTMLResponse:
    """指定年の月次キャッシュフローを表示."""
    household = await get_household(household_id)
    if household is None:
        return RedirectResponse("/", status_code=303)
    selected_plan = await get_plan(household_id, plan_id) if plan_id else None
    if plan_id and selected_plan is None:
        return RedirectResponse(f"/households/{household_id}/plans", status_code=303)
    simulation_household = selected_plan or household

    from fp_simulator.engine.cashflow import simulate

    store = get_store()
    result = simulate(store, simulation_household)
    monthly = [m for m in result.monthly if m.date.year == year]
    if not monthly:
        return HTMLResponse("指定された年のシミュレーション結果がありません", status_code=404)
    years = sorted({m.date.year for m in result.monthly})
    year_summary = {
        "income": sum(m.total_income for m in monthly),
        "tax_si": sum(m.total_tax_si for m in monthly),
        "expense": sum(m.total_expense for m in monthly),
        "net": sum(m.net for m in monthly),
        "ending_balance": monthly[-1].balance,
        "ending_assets": monthly[-1].total_assets,
    }
    previous_year = next((candidate for candidate in reversed(years) if candidate < year), None)
    next_year = next((candidate for candidate in years if candidate > year), None)
    trace_source_links = {
        month.date.isoformat(): [
            _trace_source_links(store, trace.item, month.date) for trace in month.traces
        ]
        for month in monthly
    }
    monthly_details = {
        month.date.isoformat(): _monthly_detail_items(month) for month in monthly
    }

    return templates.TemplateResponse(
        request,
        "monthly_result.html",
        {
            "title": f"{year}年 月次キャッシュフロー",
            "household": household,
            "simulation_household": simulation_household,
            "plan_id": plan_id,
            "year": year,
            "monthly": monthly,
            "year_summary": year_summary,
            "years": years,
            "previous_year": previous_year,
            "next_year": next_year,
            "trace_source_links": trace_source_links,
            "monthly_details": monthly_details,
            "active_q": "sim",
        },
    )
