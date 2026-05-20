import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "tools"))

import create_task


class CreateTaskSmokeTest(unittest.TestCase):
    def test_dry_run_does_not_create_task_directory(self):
        args = create_task.parse_args(
            [
                "Test Task",
                "--slug",
                "test-task",
                "--root",
                str(ROOT),
                "--dry-run",
            ]
        )

        rc = create_task.create_workspace(args)

        self.assertEqual(rc, 0)
        self.assertFalse((ROOT / "tasks" / "test-task").exists())


if __name__ == "__main__":
    unittest.main()
