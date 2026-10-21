"""详情页访问日预算（scripts/daily_budget.py）的单测。

背景（2026-10-21 实测）：平台侧对**详情页访问次数**有日累计预算——09-23 在累计
~700 / ~994 两处撞墙，同日 20:14 前后的实跑在累计 693 处撞到「账户受限 + 定时
恢复」这一更严的档。此前工具只有**单次 run** 的 500 请求上限，一天跑多次实跑
（各自独立计数）全程没有任何一处拦得住。

本模块是纯逻辑、无 CDP 依赖，因此按项目约定单独成文件测。
"""

import atexit
import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "daily_budget.py"

# import 期重定向：默认计数路径在仓库外的状态目录里，单测不该写那里
_TEST_BUDGET_TMP = tempfile.TemporaryDirectory(prefix="daily-budget-test-")
atexit.register(_TEST_BUDGET_TMP.cleanup)
os.environ["BOSS_DAILY_BUDGET_PATH"] = os.path.join(
    _TEST_BUDGET_TMP.name, "daily_budget.json")


def load_module():
    spec = importlib.util.spec_from_file_location("daily_budget", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class DailyBudgetTests(unittest.TestCase):

    def setUp(self):
        self.module = load_module()
        self.tmp = tempfile.TemporaryDirectory(prefix="daily-budget-test-")
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "daily_budget.json")

    def test_default_budget_is_700(self):
        """日预算默认 700（我方实测撞到的第一道墙位置）。"""
        self.assertEqual(self.module.DEFAULT_DAILY_DETAIL_BUDGET, 700)

    def test_consume_increments_and_persists(self):
        allowed, used, limit, reason = self.module.consume(1, 700, self.path)
        self.assertTrue(allowed)
        self.assertEqual(used, 1)
        self.assertEqual(limit, 700)
        self.assertEqual(reason, "")
        # 落盘内容可读，且计数正确
        with open(self.path, encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertEqual(data["detail"], 1)
        self.assertEqual(data["date"], self.module.today_key())

    def test_consume_refuses_over_limit_without_writing(self):
        self.module.consume(3, 700, self.path)
        allowed, used, limit, reason = self.module.consume(1, 3, self.path)
        self.assertFalse(allowed)
        self.assertEqual(used, 3)
        self.assertEqual(limit, 3)
        self.assertEqual(reason, "exhausted")
        # 超额尝试**不落盘**（不放大计数误差）
        with open(self.path, encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertEqual(data["detail"], 3)

    def test_zero_limit_means_unlimited(self):
        allowed, used, limit, reason = self.module.consume(5, 0, self.path)
        self.assertTrue(allowed)
        self.assertEqual(limit, 0)
        self.assertEqual(reason, "")
        self.assertEqual(self.module.remaining(0, self.path), -1)

    def test_remaining_counts_down(self):
        self.module.consume(4, 700, self.path)
        self.assertEqual(self.module.remaining(700, self.path), 696)

    def test_stale_date_resets_counter(self):
        """跨自然日自动归零（平台侧计数器也是按日）。"""
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump({"date": "2020-01-01", "detail": 650}, handle)
        self.assertEqual(self.module.read_state(self.path)["detail"], 0)
        allowed, used, _, reason = self.module.consume(1, 700, self.path)
        self.assertTrue(allowed)
        self.assertEqual(used, 1)
        self.assertEqual(reason, "")

    def test_corrupt_file_is_treated_as_empty(self):
        """文件损坏不应中断抓取（按空计数继续）。"""
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.assertEqual(self.module.read_state(self.path)["detail"], 0)
        allowed, used, _, reason = self.module.consume(1, 700, self.path)
        self.assertTrue(allowed)
        self.assertEqual(used, 1)
        self.assertEqual(reason, "")

    def test_missing_file_is_treated_as_empty(self):
        self.assertEqual(self.module.read_state(self.path)["detail"], 0)

    def test_same_job_today_is_skipped_without_consuming_budget(self):
        """同一岗位当天第二次抓：不消耗额度，返回 reason="duplicate"。

        2026-10-21 实测教训：多次实跑各写独立 --detail-output 文件，按输出文件
        去重完全失效，同一批 97 个岗位被抓了约 7.5 次，700 的日额度只换来 97 个
        岗位。有了台账，日额度必然换来互不重复的岗位。
        """
        allowed, used, limit, reason = self.module.consume(
            1, 700, self.path, job_id="job-1")
        self.assertTrue(allowed)
        self.assertEqual(reason, "")
        self.assertEqual(used, 1)

        allowed, used, limit, reason = self.module.consume(
            1, 700, self.path, job_id="job-1")
        self.assertTrue(allowed)   # 允许继续，但**不重复计费**
        self.assertEqual(reason, "duplicate")
        self.assertEqual(used, 1)  # 计数没涨
        self.assertTrue(self.module.already_fetched("job-1", self.path))
        self.assertFalse(self.module.already_fetched("job-2", self.path))

    def test_ledger_resets_next_day(self):
        """跨自然日台账归零（昨天的岗位今天可以再抓）。"""
        self.module.consume(1, 700, self.path, job_id="job-1")
        self.assertTrue(self.module.already_fetched("job-1", self.path))
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump({"date": "2020-01-01", "detail": 1, "jobs": ["job-1"]}, handle)
        self.assertFalse(self.module.already_fetched("job-1", self.path))

    def test_ledger_survives_corrupt_file(self):
        """文件损坏 → 台账与计数都按空处理（不中断抓取）。"""
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{broken")
        self.assertFalse(self.module.already_fetched("job-1", self.path))
        allowed, used, _, reason = self.module.consume(
            1, 700, self.path, job_id="job-1")
        self.assertTrue(allowed)
        self.assertEqual(reason, "")
        self.assertEqual(used, 1)

    def test_duplicate_does_not_consume_limit(self):
        """重复岗位不应把额度推向超限（否则等于没去重）。"""
        self.module.consume(1, 1, self.path, job_id="job-1")
        allowed, used, limit, reason = self.module.consume(
            1, 1, self.path, job_id="job-1")
        self.assertTrue(allowed)
        self.assertEqual(reason, "duplicate")
        self.assertEqual(used, 1)
    def test_env_override_changes_path(self):
        """BOSS_DAILY_BUDGET_PATH 可覆盖（测试/多环境隔离）。"""
        with mock_env("BOSS_DAILY_BUDGET_PATH", self.path):
            self.assertEqual(self.module.budget_path(), self.path)

    def test_budget_path_defaults_under_state_dir(self):
        """默认落在仓库外的状态目录里。"""
        with mock_env("BOSS_DAILY_BUDGET_PATH", None):
            with mock_env("BOSS_STATE_DIR", self.tmp.name):
                path = self.module.budget_path()
        self.assertTrue(path.startswith(self.tmp.name))
        self.assertTrue(path.endswith("daily_budget.json"))


class mock_env:
    """临时设置/清除环境变量的上下文管理器。"""

    def __init__(self, name, value):
        self.name = name
        self.value = value
        self.old = None
        self.had = False

    def __enter__(self):
        self.had = self.name in os.environ
        self.old = os.environ.get(self.name)
        if self.value is None:
            os.environ.pop(self.name, None)
        else:
            os.environ[self.name] = self.value
        return self

    def __exit__(self, *exc):
        if self.had:
            os.environ[self.name] = self.old
        else:
            os.environ.pop(self.name, None)
        return False


if __name__ == "__main__":
    unittest.main()
