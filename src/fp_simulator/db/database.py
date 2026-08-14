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


async def init_db() -> None:
    """テーブルを初期化."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(CREATE_TABLE)
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


async def assign_owner_to_unowned(owner_email: str) -> int:
    """所有者未設定の既存世帯を指定メールアドレスへ割り当てる."""
    normalized_email = owner_email.strip().lower()
    if not normalized_email:
        raise ValueError("owner_email must not be empty")

    async with aiosqlite.connect(DB_PATH) as db:
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
        await db.commit()
    return updated_count


async def delete_household(household_id: str) -> None:
    """世帯を削除."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM households WHERE id = ?", (household_id,))
        await db.commit()
