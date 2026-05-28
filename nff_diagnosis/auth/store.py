from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from nff_diagnosis.auth.models import AgentRecord

_DDL = """
CREATE TABLE IF NOT EXISTS agents (
    id              TEXT PRIMARY KEY,
    owner_id        TEXT NOT NULL,
    name            TEXT NOT NULL,
    slug            TEXT UNIQUE NOT NULL,
    description     TEXT,
    avatar_url      TEXT,
    key_hash        TEXT,
    key_prefix      TEXT,
    is_active       INTEGER NOT NULL DEFAULT 1,
    parent_agent_id TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS agents_owner ON agents(owner_id);
CREATE INDEX IF NOT EXISTS agents_key   ON agents(key_hash);
"""

_COLS = "id, owner_id, name, slug, description, avatar_url, key_hash, key_prefix, is_active, parent_agent_id, created_at"


def _row(row: tuple) -> AgentRecord:
    id_, owner_id, name, slug, desc, avatar, key_hash, key_prefix, is_active, parent, created_at = row
    return AgentRecord(
        id=id_,
        owner_id=owner_id,
        name=name,
        slug=slug,
        description=desc,
        avatar_url=avatar,
        key_hash=key_hash,
        key_prefix=key_prefix,
        is_active=bool(is_active),
        parent_agent_id=parent,
        created_at=datetime.fromisoformat(created_at),
    )


class AgentStore:
    def __init__(self, db_path: Path | str) -> None:
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.executescript(_DDL)
        self._conn.commit()

    def create(
        self,
        owner_id: str,
        name: str,
        slug: str,
        description: str | None,
        avatar_url: str | None,
        parent_agent_id: str | None,
    ) -> AgentRecord:
        record = AgentRecord(
            id=str(uuid.uuid4()),
            owner_id=owner_id,
            name=name,
            slug=slug,
            description=description,
            avatar_url=avatar_url,
            is_active=True,
            parent_agent_id=parent_agent_id,
            created_at=datetime.now(timezone.utc),
        )
        self._conn.execute(
            f"INSERT INTO agents({_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                record.id, record.owner_id, record.name, record.slug,
                record.description, record.avatar_url,
                record.key_hash, record.key_prefix,
                int(record.is_active), record.parent_agent_id,
                record.created_at.isoformat(),
            ),
        )
        self._conn.commit()
        return record

    def get_by_id(self, agent_id: str) -> AgentRecord | None:
        row = self._conn.execute(
            f"SELECT {_COLS} FROM agents WHERE id = ?", (agent_id,)
        ).fetchone()
        return _row(row) if row else None

    def get_by_slug(self, slug: str) -> AgentRecord | None:
        row = self._conn.execute(
            f"SELECT {_COLS} FROM agents WHERE slug = ? AND is_active = 1", (slug,)
        ).fetchone()
        return _row(row) if row else None

    def get_by_key_hash(self, key_hash: str) -> AgentRecord | None:
        row = self._conn.execute(
            f"SELECT {_COLS} FROM agents WHERE key_hash = ? AND is_active = 1", (key_hash,)
        ).fetchone()
        return _row(row) if row else None

    def list_by_owner(self, owner_id: str) -> list[AgentRecord]:
        rows = self._conn.execute(
            f"SELECT {_COLS} FROM agents WHERE owner_id = ? AND is_active = 1 ORDER BY created_at DESC",
            (owner_id,),
        ).fetchall()
        return [_row(r) for r in rows]

    def deactivate(self, agent_id: str, owner_id: str) -> bool:
        cur = self._conn.execute(
            "UPDATE agents SET is_active = 0 WHERE id = ? AND owner_id = ? AND is_active = 1",
            (agent_id, owner_id),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def deactivate_all_for_owner(self, owner_id: str) -> None:
        self._conn.execute(
            "UPDATE agents SET is_active = 0 WHERE owner_id = ?", (owner_id,)
        )
        self._conn.commit()

    def count_active_sub_agents(self, parent_agent_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM agents WHERE parent_agent_id = ? AND is_active = 1",
            (parent_agent_id,),
        ).fetchone()
        return row[0] if row else 0

    def upsert_key(self, agent_id: str, key_hash: str, key_prefix: str) -> None:
        self._conn.execute(
            "UPDATE agents SET key_hash = ?, key_prefix = ? WHERE id = ?",
            (key_hash, key_prefix, agent_id),
        )
        self._conn.commit()

    def slug_exists(self, slug: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM agents WHERE slug = ?", (slug,)
        ).fetchone() is not None
