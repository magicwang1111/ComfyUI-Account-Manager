import bcrypt
import json
import os
import hashlib
import tempfile
from pathlib import Path

from filelock import FileLock


class UsersDB:
    def __init__(self, database: str | Path):
        self.database = database
        self._lock = FileLock(f"{self.database}.lock", timeout=15)

        self.users = {}
        self.admin_user = (None, {})

        self._database_hash = None

        self.load_users()

    @staticmethod
    def hash_password(password: str) -> str:
        """Hash a password using bcrypt."""
        return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

    def calculate_file_hash(self) -> str:
        """Calculate the SHA256 hash of the database file."""
        if os.path.exists(self.database):
            with open(self.database, "rb") as f:
                file_data = f.read()
                return hashlib.sha256(file_data).hexdigest()
        return ""

    def load_users(self) -> dict:
        """Load users from the database if it has changed."""
        current_hash = self.calculate_file_hash()
        if current_hash != self._database_hash:
            if os.path.exists(self.database):
                with open(self.database, "r") as f:
                    try:
                        self.users = json.load(f)
                        self._database_hash = current_hash
                    except json.JSONDecodeError:
                        self.users = {}
        return self.users

    def save_users(self, users: dict) -> None:
        """Save users to the database and update the hash."""
        with self._lock:
            self._save_users_unlocked(users)

    def _save_users_unlocked(self, users: dict) -> None:
        database_path = Path(self.database)
        database_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_path = tempfile.mkstemp(
            prefix=f".{database_path.name}.",
            suffix=".tmp",
            dir=database_path.parent,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(users, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, database_path)
        finally:
            if os.path.exists(temporary_path):
                os.unlink(temporary_path)
        self.users = dict(users)
        self._database_hash = self.calculate_file_hash()

    def add_user(self, id: str, username: str, password: str, admin: bool) -> None:
        """Add a user to the database."""
        with self._lock:
            users = {}
            if os.path.exists(self.database):
                with open(self.database, "r", encoding="utf-8") as handle:
                    try:
                        users = json.load(handle)
                    except json.JSONDecodeError:
                        users = {}
            if any(user.get("username") == username for user in users.values()):
                raise ValueError("Username already exists")
            user = {"username": username, "password": self.hash_password(password)}
            if admin:
                user["admin"] = admin
            users[id] = user
            self._save_users_unlocked(users)

    def get_user(self, username: str = "", user_id: str = "") -> tuple[str, dict]:
        """Retrieve a user by username or ID."""
        self.load_users()

        if user_id:
            user = self.users.get(user_id)
            if user:
                return user_id, user
            return None, {}

        for uid, user_data in self.users.items():
            if user_data["username"] == username:
                return uid, user_data

        return None, {}

    def check_username_password(self, username: str, password: str) -> bool:
        """Check if the username and password match."""
        user_id, user_data = self.get_user(username)
        if not user_id:
            return False

        return bcrypt.checkpw(
            password.encode("utf-8"), user_data["password"].encode("utf-8")
        )

    def get_admin_user(self) -> tuple[str, dict] | None:
        """Get the admin user from the database."""
        self.load_users()
        for uid, user_data in self.users.items():
            if user_data.get("admin"):
                self.admin_user = (uid, user_data)

        return self.admin_user
