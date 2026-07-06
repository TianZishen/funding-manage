"""SQLite persistence for shared portfolio categories."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


class PortfolioNameExistsError(ValueError):
    pass


class PortfolioStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)

    def _connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS portfolios (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                data_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                sort_order INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(portfolios)")
        }
        if "sort_order" not in columns:
            connection.execute(
                "ALTER TABLE portfolios ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0"
            )
            connection.execute("UPDATE portfolios SET sort_order = rowid")
        return connection

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def list(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, name, created_at, updated_at, sort_order FROM portfolios ORDER BY sort_order, created_at"
            ).fetchall()
        return [dict(row) for row in rows]

    def create(self, name: str, data: Dict[str, Any]) -> Dict[str, Any]:
        clean_name = str(name).strip()
        if not clean_name or len(clean_name) > 40:
            raise ValueError("分类名称应为 1-40 个字符")
        portfolio_id = uuid.uuid4().hex
        now = self._now()
        try:
            with self._connect() as connection:
                next_order = connection.execute(
                    "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM portfolios"
                ).fetchone()[0]
                connection.execute(
                    "INSERT INTO portfolios(id, name, data_json, created_at, updated_at, sort_order) VALUES (?, ?, ?, ?, ?, ?)",
                    (portfolio_id, clean_name, json.dumps(data, ensure_ascii=False), now, now, next_order),
                )
        except sqlite3.IntegrityError as exc:
            raise PortfolioNameExistsError("分类名称已存在") from exc
        result = self.get(portfolio_id)
        if result is None:
            raise RuntimeError("创建分类后无法读取数据")
        return result

    def get(self, portfolio_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id, name, data_json, created_at, updated_at FROM portfolios WHERE id = ?",
                (portfolio_id,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["data"] = json.loads(result.pop("data_json"))
        return result

    def update(self, portfolio_id: str, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        now = self._now()
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE portfolios SET data_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(data, ensure_ascii=False), now, portfolio_id),
            )
        return self.get(portfolio_id) if cursor.rowcount else None

    def delete(self, portfolio_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM portfolios WHERE id = ?", (portfolio_id,)
            )
        return bool(cursor.rowcount)

    def reorder(self, portfolio_ids: List[str]) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            existing = {
                row["id"] for row in connection.execute("SELECT id FROM portfolios")
            }
            if len(portfolio_ids) != len(set(portfolio_ids)) or set(portfolio_ids) != existing:
                raise ValueError("分类顺序必须包含全部分类且不能重复")
            connection.executemany(
                "UPDATE portfolios SET sort_order = ? WHERE id = ?",
                [(index, portfolio_id) for index, portfolio_id in enumerate(portfolio_ids)],
            )
        return self.list()
