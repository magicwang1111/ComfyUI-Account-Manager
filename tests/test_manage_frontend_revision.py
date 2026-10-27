import os
from pathlib import Path
import re
import shutil
import subprocess
import unittest


BASH = os.environ.get("BASH_BINARY") or shutil.which("bash")
if os.name == "nt" and Path("D:/git/Git/bin/bash.exe").is_file():
    BASH = "D:/git/Git/bin/bash.exe"
ROOT = Path(__file__).parents[1]


@unittest.skipUnless(BASH, "bash is required")
class ManageFrontendRevisionTests(unittest.TestCase):
    def launch(self, action, revision=""):
        source = (ROOT / "manage_comfyui.sh").read_text(encoding="utf-8")
        # Exercise target expansion and launch construction without starting services.
        source = source.rsplit('main "$@"', 1)[0]
        source += '''
require_cmd() { :; }
detect_conda_sh() { echo /dev/null; }
start_one() { build_launch_command "$(port_for_index "$1")" "$1" /tmp/test.log; echo; }
restart_one() { start_one "$1"; }
main "$@"
'''
        env = dict(os.environ, APP_DIR=".", INSTANCE_COUNT="4", GPU_WORKER_COUNT="0",
                   GPU_WORKER_INDICES="", ACCOUNT_MANAGER_FRONTEND_REVISION=revision)
        return subprocess.run([BASH, "-s", "--", action, "all"], input=source,
                              text=True, capture_output=True, env=env, cwd=ROOT, timeout=15)

    def test_batch_start_and_restart_share_one_revision(self):
        for action in ("start", "restart"):
            with self.subTest(action=action):
                result = self.launch(action)
                self.assertEqual(result.returncode, 0, result.stderr)
                revisions = re.findall(r"export ACCOUNT_MANAGER_FRONTEND_REVISION=(\d+)", result.stdout)
                self.assertEqual(len(revisions), 4)
                self.assertEqual(len(set(revisions)), 1)
                self.assertGreater(int(revisions[0]), 10**18)

    def test_explicit_revision_is_reused_for_rolling_restarts(self):
        for _ in range(2):
            result = self.launch("restart", "1234567890123456789")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.count("export ACCOUNT_MANAGER_FRONTEND_REVISION=1234567890123456789"), 4)

    def test_invalid_revision_stops_before_starting_workers(self):
        result = self.launch("restart", "bad value")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be numeric", result.stderr)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
