import importlib.util
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path


module_path = Path(__file__).parents[1] / "utils" / "users_db.py"
spec = importlib.util.spec_from_file_location("users_db", module_path)
users_db_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(users_db_module)
UsersDB = users_db_module.UsersDB


class UsersDBTests(unittest.TestCase):
    def test_concurrent_writers_do_not_drop_accounts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = os.path.join(temp_dir, "users.json")
            errors = []

            def add(index):
                try:
                    UsersDB(database).add_user(
                        f"id-{index}", f"user_{index}", "password123", index == 0
                    )
                except Exception as error:
                    errors.append(error)

            threads = [threading.Thread(target=add, args=(index,)) for index in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual([], errors)
            with open(database, "r", encoding="utf-8") as handle:
                users = json.load(handle)
            self.assertEqual(8, len(users))

    def test_duplicate_username_is_rejected_inside_lock(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = os.path.join(temp_dir, "users.json")
            store = UsersDB(database)
            store.add_user("one", "same_user", "password123", True)
            with self.assertRaises(ValueError):
                UsersDB(database).add_user(
                    "two", "same_user", "password456", False
                )


if __name__ == "__main__":
    unittest.main()
