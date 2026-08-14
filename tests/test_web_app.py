"""Webアプリのスモークテスト."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from fp_simulator.db.database import (
    assign_owner_to_unowned,
    delete_household,
    get_household,
    init_db,
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
    """所有者移行は未設定世帯だけを更新する."""
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
    finally:
        await delete_household("legacy-household")
        await delete_household("owned-household")
