"""SQLite永続化層.

世帯(Household)をJSONとしてSQLiteに保存する。
シンプルさを優先し、ドメインモデルのJSONシリアライズで永続化する。
"""

from __future__ import annotations

import datetime
import json
import os
import uuid

import aiosqlite

from fp_simulator.engine.models import Household

DB_PATH = os.environ.get("FP_DB_PATH", "fp_simulator.db")

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS households (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    data TEXT NOT NULL,  -- Household の JSON
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

CREATE_PLANS_TABLE = """
CREATE TABLE IF NOT EXISTS plans (
    id TEXT PRIMARY KEY,
    household_id TEXT NOT NULL,
    name TEXT NOT NULL,
    data TEXT NOT NULL,
    parent_plan_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

CREATE_AUDIT_LOGS_TABLE = """
CREATE TABLE IF NOT EXISTS audit_logs (
    id TEXT PRIMARY KEY,
    household_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    source TEXT NOT NULL,
    operation TEXT NOT NULL,
    target_id TEXT,
    details TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

CREATE_MIGRATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS migrations (
    name TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);
"""


async def init_db() -> None:
    """テーブルを初期化."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(CREATE_TABLE)
        await db.execute(CREATE_PLANS_TABLE)
        await db.execute(CREATE_AUDIT_LOGS_TABLE)
        await db.execute(CREATE_MIGRATIONS_TABLE)
        await db.commit()


async def save_household(household: Household) -> Household:
    """世帯を保存(新規または更新)."""
    if not household.id:
        household.id = str(uuid.uuid4())
    now = datetime.datetime.now(datetime.UTC).isoformat()
    data = household.model_dump_json()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO households (id, name, data, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET name=excluded.name, data=excluded.data, updated_at=excluded.updated_at
            """,
            (household.id, household.name, data, now, now),
        )
        await db.commit()
    return household


async def get_household(household_id: str) -> Household | None:
    """世帯をIDで取得."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT data FROM households WHERE id = ?", (household_id,)
        ) as cursor:
            row = await cursor.fetchone()
    if row is None:
        return None
    return Household.model_validate_json(row[0])


async def list_households(owner_email: str | None = None) -> list[dict]:
    """世帯の一覧(id, name, updated_at)を返す."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, name, data, updated_at FROM households ORDER BY updated_at DESC"
        ) as cursor:
            rows = await cursor.fetchall()
    households = []
    for household_id, name, data, updated_at in rows:
        household = Household.model_validate_json(data)
        if owner_email is not None and household.owner_email != owner_email:
            continue
        households.append({"id": household_id, "name": name, "updated_at": updated_at})
    return households


_MIGRATION_OWNER_ASSIGNMENT = "assign_owner_to_unowned"


async def assign_owner_to_unowned(owner_email: str) -> int:
    """所有者未設定の既存世帯を指定メールアドレスへ割り当てる（一度だけ実行）."""
    normalized_email = owner_email.strip().lower()
    if not normalized_email:
        raise ValueError("owner_email must not be empty")

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT name FROM migrations WHERE name = ?", (_MIGRATION_OWNER_ASSIGNMENT,)
        ) as cursor:
            if await cursor.fetchone() is not None:
                return 0

        async with db.execute("SELECT id, data FROM households") as cursor:
            rows = await cursor.fetchall()

        updated_count = 0
        for household_id, data in rows:
            household = Household.model_validate_json(data)
            if household.owner_email is not None:
                continue
            household.owner_email = normalized_email
            await db.execute(
                "UPDATE households SET data = ? WHERE id = ?",
                (household.model_dump_json(), household_id),
            )
            updated_count += 1

        await db.execute(
            "INSERT INTO migrations (name, applied_at) VALUES (?, ?)",
            (
                _MIGRATION_OWNER_ASSIGNMENT,
                datetime.datetime.now(datetime.UTC).isoformat(),
            ),
        )
        await db.commit()
    return updated_count


async def delete_household(household_id: str) -> None:
    """世帯を削除。監査ログは証跡として残す."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM households WHERE id = ?", (household_id,))
        await db.execute("DELETE FROM plans WHERE household_id = ?", (household_id,))
        await db.commit()


async def save_plan_snapshot(
    household_id: str,
    household: Household,
    name: str,
    parent_plan_id: str | None = None,
) -> dict[str, str]:
    """世帯の現在状態を名前付きプランとして保存."""
    plan_id = str(uuid.uuid4())
    now = datetime.datetime.now(datetime.UTC).isoformat()
    snapshot = household.model_copy(deep=True)
    snapshot.name = name.strip() or "保存プラン"
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO plans (id, household_id, name, data, parent_plan_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                plan_id,
                household_id,
                snapshot.name,
                snapshot.model_dump_json(),
                parent_plan_id,
                now,
                now,
            ),
        )
        await db.commit()
    return {
        "id": plan_id,
        "household_id": household_id,
        "name": snapshot.name,
        "parent_plan_id": parent_plan_id or "",
        "created_at": now,
        "updated_at": now,
    }


async def get_plan(household_id: str, plan_id: str) -> Household | None:
    """世帯に属する保存プランを取得."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT data FROM plans WHERE id = ? AND household_id = ?",
            (plan_id, household_id),
        ) as cursor:
            row = await cursor.fetchone()
    if row is None:
        return None
    return Household.model_validate_json(row[0])


async def list_plans(household_id: str) -> list[dict[str, str]]:
    """世帯の保存プラン履歴を新しい順で返す."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT id, name, parent_plan_id, created_at, updated_at
            FROM plans WHERE household_id = ? ORDER BY updated_at DESC
            """,
            (household_id,),
        ) as cursor:
            rows = await cursor.fetchall()
    return [
        {
            "id": plan_id,
            "name": name,
            "parent_plan_id": parent_plan_id or "",
            "created_at": created_at,
            "updated_at": updated_at,
        }
        for plan_id, name, parent_plan_id, created_at, updated_at in rows
    ]


async def delete_plan(household_id: str, plan_id: str) -> None:
    """世帯に属する保存プランを削除."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM plans WHERE id = ? AND household_id = ?",
            (plan_id, household_id),
        )
        await db.commit()


async def add_audit_log(
    household_id: str,
    actor: str,
    source: str,
    operation: str,
    target_id: str | None = None,
    details: dict | None = None,
) -> None:
    """世帯に対する変更操作を監査ログへ記録."""
    now = datetime.datetime.now(datetime.UTC).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO audit_logs
                (id, household_id, actor, source, operation, target_id, details, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                household_id,
                actor.strip() or "unknown",
                source,
                operation,
                target_id,
                json.dumps(details or {}, ensure_ascii=False),
                now,
            ),
        )
        await db.commit()


async def list_audit_logs(household_id: str) -> list[dict]:
    """世帯の監査ログを新しい順で返す."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT id, actor, source, operation, target_id, details, created_at
            FROM audit_logs WHERE household_id = ? ORDER BY created_at DESC
            """,
            (household_id,),
        ) as cursor:
            rows = await cursor.fetchall()
    return [
        {
            "id": log_id,
            "actor": actor,
            "source": source,
            "operation": operation,
            "target_id": target_id or "",
            "details": json.loads(details),
            "created_at": created_at,
        }
        for log_id, actor, source, operation, target_id, details, created_at in rows
    ]
