import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from single_instance import SingleInstance


class SingleInstanceTest(unittest.TestCase):
    def test_second_instance_requests_show_instead_of_starting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            primary = SingleInstance(data_dir)
            self.assertTrue(primary.acquire())
            shown = threading.Event()
            primary.set_show_callback(shown.set)
            primary.start()
            try:
                code = """
import os, sys
sys.path.insert(0, os.getcwd())
from single_instance import SingleInstance
instance = SingleInstance(sys.argv[1])
acquired = instance.acquire()
print("acquired", acquired)
if not acquired:
    print("requested", instance.request_show())
"""
                result = subprocess.run(
                    [sys.executable, "-c", code, str(data_dir)],
                    cwd=str(Path(__file__).resolve().parents[1]),
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("acquired False", result.stdout)
                self.assertIn("requested True", result.stdout)
                self.assertTrue(shown.wait(3.0), "主实例没有收到显示请求")
                self.assertFalse((data_dir / "instance.show").exists())
            finally:
                primary.close()
