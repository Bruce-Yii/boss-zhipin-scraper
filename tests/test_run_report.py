"""run_report 战报测试：纯本地产物，无 Chrome/网络。"""

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "boss_cdp_raw.py")


def load_module():
    sys.modules.setdefault("websocket", mock.Mock())
    sys.modules.setdefault("requests", mock.Mock())
    spec = importlib.util.spec_from_file_location("boss_cdp_raw_report", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


class RunReportTests(unittest.TestCase):

    def test_no_list_file_reports_empty(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as td:
            out = io.StringIO()
            with redirect_stdout(out):
                code = module.run_report(
                    list_path=os.path.join(td, "nope.json"))
            self.assertEqual(code, 0)
            self.assertIn("无战报", out.getvalue())

    def test_full_report_counts(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as td:
            stamp = "20261027_000000_111.json"
            lp = os.path.join(td, "boss_jobs_" + stamp)
            dp = os.path.join(td, "boss_details_" + stamp)
            _write(lp, {"keyword": "AI产品实习生", "city": "杭州",
                        "jobs": [{"job_id": "a"}, {"job_id": "b"}],
                        "record_counts": {"new": 2, "duplicate": 1},
                        "warnings": ["删库：本次剔除 1 条"]})
            _write(dp, [{"job_id": "a", "jd": "JD" * 30},
                        {"job_id": "b", "jd": ""}])
            _write(dp + ".failures.json",
                   [{"job_id": "b", "reason": "invalid_detail"}])
            out = io.StringIO()
            with redirect_stdout(out):
                code = module.run_report(list_path=lp)
            text = out.getvalue()
            self.assertEqual(code, 0)
            self.assertIn("列表 2 条", text)
            self.assertIn("新增2", text)
            self.assertIn("详情 2 条", text)
            self.assertIn("有JD 1", text)
            self.assertIn("失败 1 条", text)
            self.assertIn("invalid_detail", text)

    def test_failures_file_never_used_as_details(self):
        """failures sidecar 不能被当成详情文件（曾把 2 条失败报成详情）。"""
        module = load_module()
        with tempfile.TemporaryDirectory() as td:
            stamp = "20261027_000000_222.json"
            lp = os.path.join(td, "boss_jobs_" + stamp)
            _write(lp, {"keyword": "k", "city": "杭州", "jobs": []})
            _write(os.path.join(td, "boss_details_" + stamp) + ".failures.json",
                   [{"job_id": "x", "reason": "invalid_detail"}])
            report = module.collect_report(list_path=lp)
            self.assertFalse(
                (report.get("detail_path") or "").endswith(".failures.json"))
            self.assertEqual(report.get("detail_count"), 0)


if __name__ == "__main__":
    unittest.main()
