import json
import os
import sqlite3
from contextlib import closing


class HistoryStore:
    """Persist completed ComfyUI history entries in a small SQLite database."""

    def __init__(self, database: str):
        self.database = os.fspath(database)
        parent = os.path.dirname(os.path.abspath(self.database))
        os.makedirs(parent, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=15)
        connection.execute("PRAGMA busy_timeout=15000")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            with connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS history (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        prompt_id TEXT NOT NULL UNIQUE,
                        owner_id TEXT NOT NULL,
                        data TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS history_owner_id ON history(owner_id)"
                )

    def load(self, max_items: int) -> dict:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT prompt_id, owner_id, data
                FROM history
                ORDER BY sequence DESC
                LIMIT ?
                """,
                (max_items,),
            ).fetchall()

        history = {}
        for prompt_id, owner_id, data in reversed(rows):
            item = json.loads(data)
            if not isinstance(item, dict):
                continue
            item["user_id"] = owner_id
            history[prompt_id] = item
        return history

    def query(
        self,
        max_items: int = None,
        offset: int = -1,
        owner_id: str = None,
        prompt_id: str = None,
    ) -> dict:
        """Read current history directly from SQLite for all instances."""
        conditions = []
        params = []
        if owner_id is not None:
            conditions.append("owner_id = ?")
            params.append(owner_id)
        if prompt_id is not None:
            conditions.append("prompt_id = ?")
            params.append(prompt_id)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        count_sql = f"SELECT COUNT(*) FROM history {where}"
        with closing(self._connect()) as connection:
            total = connection.execute(count_sql, tuple(params)).fetchone()[0]
            normalized_offset = int(offset)
            if normalized_offset < 0:
                normalized_offset = max(0, total - int(max_items or total))

            sql = f"""
                SELECT prompt_id, owner_id, data
                FROM history
                {where}
                ORDER BY sequence ASC
            """
            query_params = list(params)
            if max_items is not None:
                sql += " LIMIT ? OFFSET ?"
                query_params.extend((int(max_items), normalized_offset))
            elif normalized_offset:
                sql += " LIMIT -1 OFFSET ?"
                query_params.append(normalized_offset)
            rows = connection.execute(sql, tuple(query_params)).fetchall()

        result = {}
        for current_prompt_id, current_owner_id, data in rows:
            item = json.loads(data)
            if isinstance(item, dict):
                item["user_id"] = current_owner_id
                result[current_prompt_id] = item
        return result

    def save(self, prompt_id: str, item: dict, max_items: int) -> None:
        owner_id = item.get("user_id") or ""
        data = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO history (prompt_id, owner_id, data)
                    VALUES (?, ?, ?)
                    ON CONFLICT(prompt_id) DO UPDATE SET
                        owner_id = excluded.owner_id,
                        data = excluded.data
                    """,
                    (prompt_id, owner_id, data),
                )
                connection.execute(
                    """
                    DELETE FROM history
                    WHERE sequence IN (
                        SELECT sequence
                        FROM history
                        ORDER BY sequence DESC
                        LIMIT -1 OFFSET ?
                    )
                    """,
                    (max_items,),
                )

    def delete(self, prompt_id: str) -> None:
        with closing(self._connect()) as connection:
            with connection:
                connection.execute("DELETE FROM history WHERE prompt_id = ?", (prompt_id,))

    def delete_owner(self, owner_id: str) -> None:
        with closing(self._connect()) as connection:
            with connection:
                connection.execute("DELETE FROM history WHERE owner_id = ?", (owner_id,))

    def clear(self) -> None:
        with closing(self._connect()) as connection:
            with connection:
                connection.execute("DELETE FROM history")
