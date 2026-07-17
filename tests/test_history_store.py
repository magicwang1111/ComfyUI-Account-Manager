import importlib.util
import os
import tempfile
import unittest
from pathlib import Path


module_path = Path(__file__).parents[1] / "utils" / "history_store.py"
spec = importlib.util.spec_from_file_location("history_store", module_path)
history_store = importlib.util.module_from_spec(spec)
spec.loader.exec_module(history_store)
HistoryStore = history_store.HistoryStore


class HistoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = os.path.join(self.temp_dir.name, "history.sqlite3")
        self.store = HistoryStore(self.database)

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def item(owner_id, value):
        return {
            "prompt": [0, f"prompt-{value}", {}, {}, []],
            "outputs": {"1": {"images": [{"filename": f"{value}.png"}]}},
            "status": {"status_str": "success", "completed": True, "messages": []},
            "user_id": owner_id,
        }

    def test_history_survives_new_store_instance(self):
        self.store.save("one", self.item("user-a", 1), 10)

        restored = HistoryStore(self.database).load(10)

        self.assertEqual(["one"], list(restored))
        self.assertEqual("user-a", restored["one"]["user_id"])
        self.assertEqual("1.png", restored["one"]["outputs"]["1"]["images"][0]["filename"])

    def test_limit_and_owner_delete(self):
        self.store.save("one", self.item("user-a", 1), 2)
        self.store.save("two", self.item("user-b", 2), 2)
        self.store.save("three", self.item("user-a", 3), 2)

        self.assertEqual(["two", "three"], list(self.store.load(10)))

        self.store.delete_owner("user-a")

        self.assertEqual(["two"], list(self.store.load(10)))

    def test_delete_and_clear(self):
        self.store.save("one", self.item("user-a", 1), 10)
        self.store.save("two", self.item("user-b", 2), 10)

        self.store.delete("one")
        self.assertEqual(["two"], list(self.store.load(10)))

        self.store.clear()
        self.assertEqual({}, self.store.load(10))


if __name__ == "__main__":
    unittest.main()
