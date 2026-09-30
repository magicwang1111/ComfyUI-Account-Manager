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

    def test_live_query_filters_owner_and_prompt(self):
        self.store.save("one", self.item("user-a", 1), 10)
        self.store.save("two", self.item("user-b", 2), 10)

        self.assertEqual(["one"], list(self.store.query(owner_id="user-a")))
        self.assertEqual(["two"], list(self.store.query(prompt_id="two")))

    def test_jobs_query_omits_large_inputs_but_preserves_detail_and_metadata(self):
        item = self.item("user-a", 1)
        item["prompt"][2] = {"node": {"inputs": {"image": "x" * 100000}}}
        item["prompt"][3] = {
            "create_time": 123,
            "extra_pnginfo": {"workflow": {"id": "workflow-a", "nodes": ["large"]}},
        }
        item["status"] = {
            "status_str": "error",
            "completed": False,
            "messages": [
                ["execution_start", {"timestamp": 124}],
                ["execution_error", {
                    "node_id": "node", "exception_message": "failed",
                    "timestamp": 125, "traceback": ["trace"],
                    "current_inputs": {"image": "x" * 100000},
                    "current_outputs": ["node"],
                }],
            ],
        }
        self.store.save("one", item, 10)
        self.store.save("two", self.item("user-b", 2), 10)

        result = self.store.query(owner_id="user-a", for_jobs=True)

        self.assertEqual(["one"], list(result))
        summary = result["one"]
        self.assertEqual({}, summary["prompt"][2])
        self.assertEqual(123, summary["prompt"][3]["create_time"])
        self.assertEqual("workflow-a", summary["prompt"][3]["extra_pnginfo"]["workflow"]["id"])
        self.assertEqual(item["outputs"], summary["outputs"])
        self.assertEqual(item["status"]["messages"][0], summary["status"]["messages"][0])
        error = summary["status"]["messages"][1][1]
        self.assertEqual("failed", error["exception_message"])
        self.assertEqual(["trace"], error["traceback"])
        self.assertNotIn("current_inputs", error)
        self.assertNotIn("current_outputs", error)
        self.assertEqual(item, self.store.query(prompt_id="one")["one"])

    def test_jobs_query_preserves_owner_pagination(self):
        for value in range(4):
            self.store.save(str(value), self.item("user-a", value), 10)
        self.store.save("other", self.item("user-b", 5), 10)

        self.assertEqual(
            ["1", "2"],
            list(self.store.query(owner_id="user-a", offset=1, max_items=2, for_jobs=True)),
        )


if __name__ == "__main__":
    unittest.main()
