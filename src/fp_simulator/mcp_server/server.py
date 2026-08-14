"""MCPサーバー(FPシミュレーター).

AIエージェントがライフプランの参照・計算・更新をツールとして呼べるようにする。
Streamable HTTP transport でFastAPIアプリに同居させる。
"""

from __future__ import annotations

import datetime
import json

from mcp.server.mcpserver import MCPServer

from fp_simulator.db.database import (
    get_household as db_get_household,
    list_households as db_list_households,
    save_household as db_save_household,
)
from fp_simulator.engine.cashflow import simulate
from fp_simulator.engine.models import Household
from fp_simulator.parameters.loader import get_store

mcp = MCPServer(
    name="fp-simulator",
    title="FPシミュレーター",
    description="日本のライフプラン・キャッシュフローシミュレーター。税制・社会保険・年金の月次計算と、AIによる参照・更新・説明を提供する。",
    version="0.1.0",
)


# --- 参照系ツール ---

@mcp.tool()
async def list_households() -> str:
    """世帯の一覧を返す(ID・名前・更新日時)."""
    households = await db_list_households()
    return json.dumps(households, ensure_ascii=False, indent=2)


@mcp.tool()
async def get_household(household_id: str) -> str:
    """世帯の詳細(家族・収入・年金・支出・資産)を返す."""
    household = await db_get_household(household_id)
    if household is None:
        return json.dumps({"error": "世帯が見つかりません"}, ensure_ascii=False)
    return household.model_dump_json(indent=2)


@mcp.tool()
async def list_tax_parameters() -> str:
    """利用可能な税制パラメータの一覧を返す."""
    store = get_store()
    paths = store.list_paths()
    return json.dumps(paths, ensure_ascii=False, indent=2)


@mcp.tool()
async def get_tax_parameter(path: str, date: str | None = None) -> str:
    """税制パラメータの値と出典を返す.

    Args:
        path: パラメータパス(例: "所得税.基礎控除.控除額")
        date: 適用日(YYYY-MM-DD)。省略時は今日
    """
    store = get_store()
    d = datetime.date.fromisoformat(date) if date else datetime.date.today()
    try:
        value = store.get(path, d)
        source = store.get_source(path, d)
    except KeyError:
        return json.dumps({"error": f"パラメータが見つかりません: {path}"}, ensure_ascii=False)
    return json.dumps(
        {"path": path, "date": d.isoformat(), "value": value, "source": source},
        ensure_ascii=False,
        indent=2,
    )


# --- 計算・更新系ツール ---

@mcp.tool()
async def run_simulation(household_id: str) -> str:
    """ライフプランのシミュレーションを実行し、サマリーを返す.

    Returns:
        最低貯蓄残高・最終残高・枯渇の有無・年次サマリ(最初の数年)
    """
    household = await db_get_household(household_id)
    if household is None:
        return json.dumps({"error": "世帯が見つかりません"}, ensure_ascii=False)

    store = get_store()
    result = simulate(store, household)

    balances = [m.balance for m in result.monthly]
    min_balance = min(balances) if balances else 0
    final_balance = balances[-1] if balances else 0
    depleted = min_balance < 0

    # 年次サマリ(最初の5年)
    yearly: dict[int, dict] = {}
    for m in result.monthly:
        y = m.date.year
        if y not in yearly:
            yearly[y] = {"year": y, "income": 0, "expense": 0, "tax_si": 0, "net": 0, "balance_end": 0}
        yearly[y]["income"] += m.total_income
        yearly[y]["expense"] += m.total_expense
        yearly[y]["tax_si"] += m.total_tax_si
        yearly[y]["net"] += m.net
        yearly[y]["balance_end"] = m.balance
    yearly_list = sorted(yearly.values(), key=lambda x: x["year"])[:5]

    return json.dumps(
        {
            "household": household.name,
            "months_simulated": len(result.monthly),
            "min_balance": min_balance,
            "final_balance": final_balance,
            "depleted": depleted,
            "yearly_summary_first5": yearly_list,
        },
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool()
async def get_cashflow(household_id: str, year: int, month: int | None = None) -> str:
    """指定年(または年月)のキャッシュフロー明細を返す.

    Args:
        household_id: 世帯ID
        year: 対象年
        month: 対象月(省略時は年間サマリ)
    """
    household = await db_get_household(household_id)
    if household is None:
        return json.dumps({"error": "世帯が見つかりません"}, ensure_ascii=False)

    store = get_store()
    result = simulate(store, household)

    if month is not None:
        target = datetime.date(year, month, 1)
        m = next((m for m in result.monthly if m.date == target), None)
        if m is None:
            return json.dumps({"error": "該当月のデータがありません"}, ensure_ascii=False)
        return json.dumps(
            {
                "date": m.date.isoformat(),
                "age": m.age,
                "income": {"salary": m.salary_income, "pension": m.pension_income, "retirement": m.retirement_income},
                "tax_si": {"social_insurance": m.social_insurance, "income_tax": m.income_tax, "resident_tax": m.resident_tax},
                "expense": {"living": m.living_expense, "event": m.event_expense},
                "net": m.net,
                "balance": m.balance,
                "traces": [{"item": t.item, "amount": t.amount, "basis": t.basis} for t in m.traces],
            },
            ensure_ascii=False,
            indent=2,
        )

    # 年間サマリ
    year_data = [m for m in result.monthly if m.date.year == year]
    if not year_data:
        return json.dumps({"error": "該当年のデータがありません"}, ensure_ascii=False)
    return json.dumps(
        {
            "year": year,
            "total_income": sum(m.total_income for m in year_data),
            "total_expense": sum(m.total_expense for m in year_data),
            "total_tax_si": sum(m.total_tax_si for m in year_data),
            "net": sum(m.net for m in year_data),
            "balance_end": year_data[-1].balance,
        },
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool()
async def explain_amount(household_id: str, year: int, month: int, item: str) -> str:
    """特定の金額の計算根拠を返す(トレーサビリティ).

    Args:
        household_id: 世帯ID
        year: 年
        month: 月
        item: 項目名(例: "所得税", "社会保険料", "年金収入")
    """
    household = await db_get_household(household_id)
    if household is None:
        return json.dumps({"error": "世帯が見つかりません"}, ensure_ascii=False)

    store = get_store()
    result = simulate(store, household)
    target = datetime.date(year, month, 1)
    m = next((m for m in result.monthly if m.date == target), None)
    if m is None:
        return json.dumps({"error": "該当月のデータがありません"}, ensure_ascii=False)

    matches = [t for t in m.traces if item in t.item]
    if not matches:
        return json.dumps({"error": f"項目 '{item}' の根拠が見つかりません", "available": [t.item for t in m.traces]}, ensure_ascii=False)

    return json.dumps(
        [{"item": t.item, "amount": t.amount, "basis": t.basis} for t in matches],
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool()
async def update_household(household_id: str, household_json: str) -> str:
    """世帯データを更新する(AIによる編集用).

    Args:
        household_id: 世帯ID
        household_json: 更新後の世帯データ(JSON文字列、Householdモデルに準拠)
    """
    try:
        updated = Household.model_validate_json(household_json)
    except Exception as e:
        return json.dumps({"error": f"JSONの検証に失敗しました: {e}"}, ensure_ascii=False)
    existing = await db_get_household(household_id)
    if existing is not None:
        updated.owner_email = existing.owner_email
    else:
        updated.owner_email = None
    updated.id = household_id
    await db_save_household(updated)
    return json.dumps({"status": "updated", "id": household_id, "name": updated.name}, ensure_ascii=False)
