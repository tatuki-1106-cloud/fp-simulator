"""Webアプリのスモークテスト."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from fp_simulator.db.database import (
    assign_owner_to_unowned,
    delete_household,
    get_household,
    get_plan,
    init_db,
    list_audit_logs,
    list_plans,
    save_household,
)
from fp_simulator.engine.models import Household
from fp_simulator.web.main import app


@pytest.fixture()
async def client() -> AsyncClient:
    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_healthz(client: AsyncClient) -> None:
    r = await client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_index(client: AsyncClient) -> None:
    r = await client.get("/")
    assert r.status_code == 200
    assert "FPシミュレーター" in r.text


async def test_debug_parameters(client: AsyncClient) -> None:
    r = await client.get("/debug/parameters")
    assert r.status_code == 200
    assert "所得税" in r.text


async def test_mcp_requires_api_key_when_not_configured(client: AsyncClient, monkeypatch) -> None:
    """MCPはAPIキー未設定時も公開フォールバックしない."""
    monkeypatch.delenv("FP_MCP_API_KEY", raising=False)
    response = await client.get("/mcp/")
    assert response.status_code == 503


async def test_iap_auth_restricts_households(
    client: AsyncClient, monkeypatch
) -> None:
    """IAP有効時は所有者以外の世帯を閲覧できない."""
    monkeypatch.setenv("FP_REQUIRE_IAP_AUTH", "true")
    await save_household(Household(id="owned-by-a", name="Aの世帯", owner_email="a@example.com"))
    try:
        unauthenticated = await client.get("/")
        assert unauthenticated.status_code == 401

        own = await client.get(
            "/households/owned-by-a/members",
            headers={"x-goog-authenticated-user-email": "accounts.google.com:a@example.com"},
        )
        assert own.status_code == 200

        other = await client.get(
            "/households/owned-by-a/members",
            headers={"x-goog-authenticated-user-email": "accounts.google.com:b@example.com"},
        )
        assert other.status_code == 403
    finally:
        await delete_household("owned-by-a")


async def test_assign_owner_to_unowned_preserves_existing_owner() -> None:
    """所有者移行は未設定世帯だけを更新し、一度だけ実行される."""
    await save_household(Household(id="legacy-household", name="旧世帯"))
    await save_household(
        Household(id="owned-household", name="既存世帯", owner_email="existing@example.com")
    )
    try:
        assert await assign_owner_to_unowned("owner@example.com") >= 1
        legacy = await get_household("legacy-household")
        existing = await get_household("owned-household")
        assert legacy is not None
        assert existing is not None
        assert legacy.owner_email == "owner@example.com"
        assert existing.owner_email == "existing@example.com"
        # 二度目はスキップされる
        assert await assign_owner_to_unowned("other@example.com") == 0
        legacy = await get_household("legacy-household")
        assert legacy is not None
        assert legacy.owner_email == "owner@example.com"
    finally:
        await delete_household("legacy-household")
        await delete_household("owned-household")


async def test_plan_save_copy_restore_and_delete(client: AsyncClient) -> None:
    """保存プランは独立したスナップショットとして扱える."""
    household = Household(id="plan-household", name="元プラン")
    await save_household(household)
    try:
        saved = await client.post(
            "/households/plan-household/plans",
            data={"name": "基準プラン"},
            follow_redirects=False,
        )
        assert saved.status_code == 303
        plans = await list_plans("plan-household")
        assert len(plans) == 1
        plan_id = plans[0]["id"]
        logs = await list_audit_logs("plan-household")
        assert logs[0]["operation"] == "plan.save"

        copied = await client.post(
            f"/households/plan-household/plans/{plan_id}/copy",
            data={"name": "コピー"},
            follow_redirects=False,
        )
        assert copied.status_code == 303
        assert len(await list_plans("plan-household")) == 2
        assert any(log["operation"] == "plan.copy" for log in await list_audit_logs("plan-household"))

        restored = await client.post(
            f"/households/plan-household/plans/{plan_id}/restore",
            follow_redirects=False,
        )
        assert restored.status_code == 303
        restored_household = await get_household("plan-household")
        assert restored_household is not None
        assert restored_household.name == "基準プラン"
        assert await get_plan("plan-household", plan_id) is not None
        assert any(
            log["operation"] == "plan.restore"
            for log in await list_audit_logs("plan-household")
        )

        deleted = await client.post(
            f"/households/plan-household/plans/{plan_id}/delete",
            follow_redirects=False,
        )
        assert deleted.status_code == 303
        assert len(await list_plans("plan-household")) == 1
        assert any(
            log["operation"] == "plan.delete"
            for log in await list_audit_logs("plan-household")
        )
        audit_page = await client.get("/households/plan-household/audit")
        assert audit_page.status_code == 200
        assert "変更履歴" in audit_page.text
    finally:
        await delete_household("plan-household")
