"""审计日志递归脱敏、保留字段与并发写入回归测试。"""

import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "audit.py"


def load_module():
    spec = importlib.util.spec_from_file_location("audit_hardening", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class AuditHardeningTests(unittest.TestCase):
    def test_recursively_sanitizes_fields_and_reserves_metadata(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "events.jsonl"
            ok = module.record_event(
                "alert", path=str(path),
                payload={"profile": {"token": "nested-secret", "safe": "keep"},
                         "headers": {"Authorization": "Bearer bearer-secret"}},
                cookie="cookie-secret", ts="caller-ts")
            self.assertTrue(ok)
            event = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(event["event"], "alert")
        self.assertNotEqual(event["ts"], "caller-ts")
        self.assertNotIn("cookie", event)
        self.assertNotIn("Authorization", event["payload"]["headers"])
        self.assertNotIn("cookie-secret", json.dumps(event))
        self.assertNotIn("nested-secret", json.dumps(event))
        self.assertNotIn("bearer-secret", json.dumps(event))
        self.assertEqual(event["payload"]["profile"]["safe"], "keep")

    def test_non_serializable_values_are_safe_markers(self):
        module = load_module()

        class BadValue:
            pass

        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "events.jsonl"
            self.assertTrue(module.record_event(
                "alert", path=str(path), bad=BadValue()))
            event = json.loads(path.read_text(encoding="utf-8"))
        self.assertIsInstance(event["bad"], str)
        self.assertIn("BadValue", event["bad"])

    def test_rotation_and_concurrent_append_keep_all_events(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "events.jsonl"
            path.write_text("x" * (module.MAX_BYTES + 1), encoding="utf-8")
            with mock.patch.object(module, "MAX_BYTES", 2000):
                with ThreadPoolExecutor(max_workers=4) as pool:
                    results = list(pool.map(
                        lambda i: module.record_event(
                            "alert", path=str(path), index=i),
                        range(20)))
            self.assertTrue(all(results))
            self.assertTrue(path.with_name(path.name + ".1").exists())
            events = module.read_events(str(path))
        self.assertEqual(len(events), 20)
        self.assertEqual({event["index"] for event in events}, set(range(20)))


if __name__ == "__main__":
    unittest.main()
