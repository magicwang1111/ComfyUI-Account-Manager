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
        return sqlite3.connect(self.database, timeout=10)

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
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
