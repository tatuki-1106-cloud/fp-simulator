"""MCPツールの直接呼び出しテスト.

stdio/HTTPの通信レイヤーではなく、ツール関数の振る舞いを直接検証する。
(実際のMCP接続はClaude等のクライアントから行う)
"""

from __future__ import annotations

import json
import os
import tempfile

import pytest

os.environ["FP_DB_PATH"] = tempfile.mktemp(suffix=".db")

from fp_simulator.db.database import init_db
from fp_simulator.mcp_server.server import mcp


@pytest.fixture()
async def setup_db():
    await init_db()


async def test_list_tools(setup_db) -> None:
    """ツール一覧."""
    tools = await mcp.list_tools()
    names = [t.name for t in tools]
    assert "list_households" in names
    assert "get_household" in names
    assert "run_simulation" in names
    assert "get_cashflow" in names
    assert "explain_amount" in names
    assert "get_tax_parameter" in names
    assert "update_household" in names
    assert "list_tax_parameters" in names


async def test_get_tax_parameter(setup_db) -> None:
    """税制パラメータの取得."""
    result = await mcp.call_tool("get_tax_parameter", {"path": "所得税.基礎控除.控除額", "date": "2025-01-01"})
    content = result[1][0].text if isinstance(result, tuple) else result.content[0].text
    data = json.loads(content)
    assert data["value"] == 580000


async def test_list_households_empty(setup_db) -> None:
    """世帯一覧(空)."""
    result = await mcp.call_tool("list_households", {})
    content = result[1][0].text if isinstance(result, tuple) else result.content[0].text
    assert json.loads(content) == []


async def test_full_flow(setup_db) -> None:
    """世帯作成→シミュレーション→CF取得→説明."""
    household_json = json.dumps({
        "id": "test-mcp",
        "name": "MCPテスト世帯",
        "members": [
            {"id": "h", "name": "たろう", "relationship": "世帯主",
             "birth_date": "1996-04-01", "life_expectancy_age": 90, "prefecture": "東京都"}
        ],
        "incomes": [
            {"id": "i1", "member_id": "h", "name": "会社員",
             "social_insurance_type": "給与(厚生年金)",
             "start_age": 29, "end_age": 60, "monthly_amount": 300000,
             "bonus_months": [6, 12], "bonus_amount": 500000,
             "retirement_allowance": 20000000, "retirement_age": 60}
        ],
        "pension_records": [
            {"member_id": "h", "kokumin_months": 480, "kousei_months": 456,
             "avg_standard_remuneration": 300000, "kousei_months_after_2003_04": 456, "start_age": 65}
        ],
        "expenses": [{"id": "e1", "name": "生活費", "monthly_amount": 200000, "cycle": "monthly", "start_age": 0}],
        "accounts": [{"id": "a1", "name": "普通預金", "balance": 3000000, "interest_rate": 0.0}],
        "assumptions": {"base_year": 2026, "base_month": 1},
    })

    # 作成
    result = await mcp.call_tool("update_household", {"household_id": "test-mcp", "household_json": household_json})
    content = result[1][0].text if isinstance(result, tuple) else result.content[0].text
    assert json.loads(content)["status"] == "updated"

    # シミュレーション
    result = await mcp.call_tool("run_simulation", {"household_id": "test-mcp"})
    content = result[1][0].text if isinstance(result, tuple) else result.content[0].text
    sim = json.loads(content)
    assert sim["depleted"] is False
    assert sim["min_balance"] > 0

    # CF取得
    result = await mcp.call_tool("get_cashflow", {"household_id": "test-mcp", "year": 2026, "month": 1})
    content = result[1][0].text if isinstance(result, tuple) else result.content[0].text
    cf = json.loads(content)
    assert cf["income"]["salary"] == 300000

    # 説明
    result = await mcp.call_tool("explain_amount", {"household_id": "test-mcp", "year": 2026, "month": 1, "item": "社会保険料"})
    content = result[1][0].text if isinstance(result, tuple) else result.content[0].text
    explanation = json.loads(content)
    assert len(explanation) > 0
    assert explanation[0]["item"] == "社会保険料"
    assert "厚生年金" in explanation[0]["basis"]
