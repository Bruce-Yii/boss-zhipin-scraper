"""坏页台账（scripts/known_failures.py）的单测。

背景（2026-10-22）：实测每天固定有 3 个岗位的详情页抽不出 JD（invalid_detail），
工具每天都重试、每天白花 3 次详情页额度，而结果每次都一样 → 根因是"这类页面的解析
路径不适用"，不是"数据缺失"。台账把这类页面沉淀下来，命中即跳过、不发请求、不耗额度。
"""

import importlib.util
import os
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "known_failures.py"


def load_module():
    spec = importlib.util.spec_from_file_location("known_failures", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class KnownFailuresTests(unittest.TestCase):

    def setUp(self):
        self.module = load_module()
        self.tmp = tempfile.TemporaryDirectory(prefix="known-bad-test-")
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "known_bad_jobs.json")

    def test_record_then_is_known(self):
        self.assertFalse(self.module.is_known("job-1", self.path))
        self.module.record("job-1", "no_section", "职位描述取不到正文", self.path)
        self.assertTrue(self.module.is_known("job-1", self.path))
        self.assertFalse(self.module.is_known("job-2", self.path))

    def test_record_keeps_fingerprint_and_counts(self):
        self.module.record("job-1", "no_section", "x", self.path)
        self.module.record("job-1", "no_section", "x", self.path)
        entry = self.module.get("job-1", self.path)
        self.assertEqual(entry["fingerprint"], "no_section")
        self.assertEqual(entry["hits"], 2)
        self.assertTrue(entry["first_seen"])

    def test_fingerprint_counts_group_by_reason(self):
        self.module.record("job-1", "no_section", "x", self.path)
        self.module.record("job-2", "no_section", "x", self.path)
        self.module.record("job-3", "too_short", "x", self.path)
        counts = self.module.fingerprint_counts(self.path)
        self.assertEqual(counts["no_section"], 2)
        self.assertEqual(counts["too_short"], 1)
        self.assertEqual(self.module.summary(self.path)["total"], 3)

    def test_corrupt_file_is_treated_as_empty(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{broken")
        self.assertFalse(self.module.is_known("job-1", self.path))
        self.module.record("job-1", "no_section", "x", self.path)
        self.assertTrue(self.module.is_known("job-1", self.path))

    def test_empty_job_id_is_ignored(self):
        self.module.record("", "no_section", "x", self.path)
        self.assertEqual(self.module.summary(self.path)["total"], 0)

    def test_clear_empties_the_ledger(self):
        self.module.record("job-1", "no_section", "x", self.path)
        self.module.clear(self.path)
        self.assertFalse(self.module.is_known("job-1", self.path))

    def test_env_override_changes_path(self):
        old = os.environ.get("BOSS_KNOWN_BAD_PATH")
        os.environ["BOSS_KNOWN_BAD_PATH"] = self.path
        try:
            self.assertEqual(self.module.known_bad_path(), self.path)
        finally:
            if old is None:
                os.environ.pop("BOSS_KNOWN_BAD_PATH", None)
            else:
                os.environ["BOSS_KNOWN_BAD_PATH"] = old


if __name__ == "__main__":
    unittest.main()
