"""并发 / 状态类缺陷的回归测试（2026-10-22 独立审计后的三条修复）。

覆盖三条被独立审计证实的缺陷：

1. **坏页台账把瞬时失败永久拉黑**（`scripts/known_failures.py`）
   审计实测：``job description too short after validation: 38 < 120``（too_short）、
   ``detail page rendered navigation chrome without a JD``（unknown）、
   ``BOSS正在加载中``（shell_only）、HTML 直取无响应 / HTTP 500（unknown）
   这些**瞬时**形态被当成"坏页"永久拉黑 → 该岗位从此永远不出现在任何产出里。
   另外 ``--retry-job`` 的逃生口是**假的**（抓取路径上的 ``is_known()`` 无条件命中）。

2. **日预算跨进程读改写没有文件锁**（`scripts/daily_budget.py`）
   审计实测：4 进程各消费 60 次 → 3 个进程在 ``os.replace`` 上抛
   ``PermissionError: [WinError 5]``，落盘计数只有 **60/240**；该异常又被
   ``_cdp_exception_types()``（含 OSError）吞成 ``reason="cdp_session"``
   → 连续 3 次误判成"CDP 故障"并熔断停手（诊断是错的，预算还少算）。

3. **``dup_group`` 用 ``hash()``**（`scripts/semantics.py`）
   审计实测：同一份数据三个进程跑出 ``dup-10961898`` / ``dup-96218808`` /
   ``dup-52751366``（PYTHONHASHSEED 随机）→ 跨天/跨 run 合并时组号对不上，
   同样输入两次跑出的文件字节不同（不可复现）。

本模块**不需要 Chrome、不需要网络**；状态目录 / 台账 / 预算文件全部指到临时目录
（参照 ``tests/test_chrome_setup.py`` 的隔离做法），绝不写受保护的真实状态。
"""

import contextlib
import importlib.util
import io
import json
import multiprocessing
import os
import pathlib
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"

# 隔离环境变量：状态目录 + 台账 + 日预算（本模块每个用例都重设到自己的临时目录）
_ISOLATED_ENV_KEYS = (
    "HOME",
    "USERPROFILE",
    "BOSS_STATE_DIR",
    "BOSS_DAILY_BUDGET_PATH",
    "BOSS_KNOWN_BAD_PATH",
    "BOSS_RETRY_JOB_IDS",
    "PYTHONHASHSEED",
)


def load_script(name, filename):
    """按文件路径加载 scripts/ 下的模块（与其它单测同款做法）。"""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def duplicate_sample_jobs():
    """两条内容完全相同（= 同源重复发布）的岗位 + 一条无关岗位。"""
    jd = "岗位职责：负责 AI 产品需求分析与设计。" * 10
    return [
        {"job_id": "a1", "jd": jd, "salary": "20-30K", "location": "杭州"},
        {"job_id": "b2", "jd": jd, "salary": "20-30K", "location": "杭州"},
        {"job_id": "c3", "jd": "完全无关的岗位内容" * 30, "salary": "10-15K",
         "location": "上海"},
    ]


# ---------------------------------------------------------------------------
# 子进程入口（必须是模块级函数：spawn 方式要能 pickle）
# ---------------------------------------------------------------------------

def budget_worker(script_path, budget_path, count, prefix, queue):
    """子进程：真实并发消费日预算（每次一个不同的 job_id → 不触发 duplicate）。"""
    try:
        module = load_script("daily_budget_child", script_path)
        consumed = 0
        reasons = {}
        for index in range(count):
            allowed, _used, _limit, reason = module.consume(
                1, 10 ** 9, budget_path, job_id=f"{prefix}-{index}")
            reasons[reason or "ok"] = reasons.get(reason or "ok", 0) + 1
            if allowed and reason == "":
                consumed += 1
        queue.put(("ok", {"consumed": consumed, "reasons": reasons,
                          "warnings": list(module.WARNINGS)[-3:]}))
    except BaseException as exc:  # pragma: no cover - 子进程异常要带回主进程
        queue.put(("error", f"{type(exc).__name__}: {exc}"))


def dup_group_worker(script_path, queue):
    """子进程：算重复组号（跨进程必须一致）。"""
    try:
        module = load_script("semantics_child", script_path)
        jobs = duplicate_sample_jobs()
        module.mark_duplicate_groups(jobs)
        queue.put(("ok", jobs[0]["dup_group"]))
    except BaseException as exc:  # pragma: no cover
        queue.put(("error", f"{type(exc).__name__}: {exc}"))


def record_transient_worker(script_path, ledger_path, job_id, queue):
    """子进程：记一条瞬时失败（下一轮 = 另一个进程 → 应该重新尝试）。"""
    try:
        module = load_script("known_failures_child", script_path)
        module.record(job_id, "too_short",
                      "job description too short after validation: 38 < 120",
                      ledger_path)
        queue.put(("ok", module.is_known(job_id, ledger_path)))
    except BaseException as exc:  # pragma: no cover
        queue.put(("error", f"{type(exc).__name__}: {exc}"))


# ---------------------------------------------------------------------------
# 测试基类：临时 HOME / STATE_DIR 隔离
# ---------------------------------------------------------------------------

class IsolatedStateTestCase(unittest.TestCase):
    """每个用例跑在**自己的**临时 HOME + 状态目录里（不碰真实状态）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="state-concurrency-")
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "home")
        os.makedirs(self.home, exist_ok=True)
        self._env_backup = {key: os.environ.get(key) for key in _ISOLATED_ENV_KEYS}
        os.environ["HOME"] = self.home
        os.environ["USERPROFILE"] = self.home
        os.environ["BOSS_STATE_DIR"] = self.tmp.name
        os.environ["BOSS_DAILY_BUDGET_PATH"] = os.path.join(
            self.tmp.name, "daily_budget.json")
        os.environ["BOSS_KNOWN_BAD_PATH"] = os.path.join(
            self.tmp.name, "known_bad_jobs.json")
        os.environ.pop("BOSS_RETRY_JOB_IDS", None)
        os.environ.pop("PYTHONHASHSEED", None)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        for key, value in self._env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    @property
    def budget_path(self):
        return os.path.join(self.tmp.name, "daily_budget.json")

    @property
    def ledger_path(self):
        return os.path.join(self.tmp.name, "known_bad_jobs.json")


# ---------------------------------------------------------------------------
# Bug 2：日预算跨进程并发
# ---------------------------------------------------------------------------

class DailyBudgetConcurrencyTests(IsolatedStateTestCase):

    def test_four_processes_consume_without_losing_counts(self):
        """4 进程 × 15 次并发消费 → 落盘计数必须**等于**总量 60。

        审计实测的失败形态：3 个进程在 ``os.replace`` 上抛
        ``PermissionError: [WinError 5]``，落盘只剩 60/240。
        """
        script_path = str(SCRIPTS / "daily_budget.py")
        ctx = multiprocessing.get_context("spawn")
        queue = ctx.Queue()
        processes = []
        for index in range(4):
            proc = ctx.Process(
                target=budget_worker,
                args=(script_path, self.budget_path, 15, f"p{index}", queue))
            processes.append(proc)
        for proc in processes:
            proc.start()
        for proc in processes:
            proc.join(timeout=180)
        results = [queue.get(timeout=60) for _ in processes]
        for index, proc in enumerate(processes):
            self.assertEqual(proc.exitcode, 0,
                             f"子进程 {index} 异常退出（exitcode={proc.exitcode}）")
        for status, value in results:
            self.assertEqual(status, "ok", f"子进程报错：{value}")

        with open(self.budget_path, encoding="utf-8") as handle:
            state = json.load(handle)
        consumed = sum(int(value["consumed"]) for _status, value in results)
        self.assertEqual(
            state["detail"], 60,
            f"跨进程并发丢计数：落盘={state['detail']}，"
            f"子进程各自计到={consumed}，"
            f"子进程明细={[value for _s, value in results]}")
        self.assertEqual(len(set(state["jobs"])), 60)
        self.assertGreaterEqual(state["seq"], 60, "seq 必须单调递增")

    def test_sequential_consume_matches_multiprocess_total(self):
        """同一进程内多次消费的计数口径与多进程一致（回归护栏）。"""
        module = load_script("daily_budget_seq", "daily_budget.py")
        for index in range(5):
            allowed, used, _limit, reason = module.consume(
                1, 700, self.budget_path, job_id=f"job-{index}")
            self.assertTrue(allowed)
            self.assertEqual(reason, "")
            self.assertEqual(used, index + 1)
        self.assertEqual(module.read_state(self.budget_path)["detail"], 5)

    def test_duplicate_job_is_not_counted_twice(self):
        """同一岗位当天第二次抓：不消耗额度（去重语义没被并发改造破坏）。"""
        module = load_script("daily_budget_dup", "daily_budget.py")
        module.consume(1, 700, self.budget_path, job_id="job-1")
        allowed, used, _limit, reason = module.consume(
            1, 700, self.budget_path, job_id="job-1")
        self.assertTrue(allowed)
        self.assertEqual(reason, "duplicate")
        self.assertEqual(used, 1)

    def test_retry_job_allows_refetch_of_same_job_today(self):
        """``--retry-job`` 指定的岗位必须能**重抓**（今天已抓不再一刀切跳过）。"""
        module = load_script("daily_budget_retry", "daily_budget.py")
        module.consume(1, 700, self.budget_path, job_id="job-1")
        with mock.patch.object(sys, "argv",
                               ["boss_cdp_raw.py", "--retry-job", "job-1"]):
            allowed, used, _limit, reason = module.consume(
                1, 700, self.budget_path, job_id="job-1")
        self.assertTrue(allowed)
        self.assertEqual(reason, "")
        self.assertEqual(used, 2, "强制重试必须真的再发一次请求")

    def test_sequence_rollback_is_reported(self):
        """版本号回退必须**被发现**（并发写覆盖导致计数丢失不能静默）。"""
        module = load_script("daily_budget_seqcheck", "daily_budget.py")
        module.consume(1, 700, self.budget_path, job_id="job-1")
        with open(self.budget_path, encoding="utf-8") as handle:
            state = json.load(handle)
        # 人为把版本号与计数改小（模拟并发写覆盖 / 文件回滚）
        state["seq"] = 0
        state["detail"] = 0
        with open(self.budget_path, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        module.WARNINGS.clear()
        module.read_state(self.budget_path)
        self.assertTrue(
            any("回退" in text for text in module.WARNINGS),
            f"计数回退必须告警，实际 WARNINGS={module.WARNINGS}")

    def test_unreadable_state_refuses_to_consume(self):
        """计数读不出来 → 保守拒绝（宁可少抓，也不超平台日额度）。"""
        module = load_script("daily_budget_unreadable", "daily_budget.py")
        module.WARNINGS.clear()
        # 造一个"存在但不是合法 JSON"的文件 → corrupt → 按空计数处理（不拒绝）
        with open(self.budget_path, "w", encoding="utf-8") as handle:
            handle.write("{broken")
        allowed, _used, _limit, reason = module.consume(
            1, 700, self.budget_path, job_id="job-1")
        self.assertTrue(allowed)
        self.assertEqual(reason, "")

    def test_write_failure_does_not_raise_oserror(self):
        """写失败**不能抛 OSError**（会被 CDP 异常族吞成"浏览器故障"）。"""
        module = load_script("daily_budget_writefail", "daily_budget.py")
        original_replace = os.replace

        def _boom(src, dst, *args, **kwargs):
            raise PermissionError(5, "Access is denied")

        with mock.patch.object(os, "replace", _boom):
            allowed, used, _limit, reason = module.consume(
                1, 700, self.budget_path, job_id="job-1")
        self.assertIsNotNone(original_replace)
        self.assertTrue(allowed)
        self.assertEqual(reason, "")
        self.assertEqual(used, 1)
        self.assertTrue(any("写入失败" in text for text in module.WARNINGS))


# ---------------------------------------------------------------------------
# Bug 1：坏页台账（结构性 vs 瞬时 / TTL / --retry-job / 跳过提示）
# ---------------------------------------------------------------------------

class KnownFailuresLedgerTests(IsolatedStateTestCase):

    def test_transient_failures_are_not_permanently_blacklisted(self):
        """瞬时失败 → 不进长期台账：本轮跳过、**下一轮重试**。"""
        module = load_script("known_failures_ledger", "known_failures.py")
        transient = [
            ("too_short", "job description too short after validation: 38 < 120"),
            ("unknown", "detail page rendered navigation chrome without a JD"),
            ("shell_only", "BOSS正在加载中"),
            ("unknown", "HTML 直取无响应"),
        ]
        for index, (fingerprint, message) in enumerate(transient):
            job_id = f"transient-{index}"
            module.record(job_id, fingerprint, message, self.ledger_path)
            entry = module.get(job_id, self.ledger_path)
            self.assertEqual(entry["nature"], module.TRANSIENT,
                             f"{fingerprint} 必须按瞬时处理")
            # 本轮内跳过（省一次额度）
            self.assertTrue(module.is_known(job_id, self.ledger_path))
            # 下一轮 → 自动重试（不再是"永远拉黑"）
            module.begin_new_run_scope()
            self.assertFalse(module.is_known(job_id, self.ledger_path),
                             "瞬时失败必须在下一轮重新尝试")

    def test_structural_failure_is_kept_in_long_term_ledger(self):
        """结构性失败（结构不匹配）→ 进长期台账，跨轮仍然跳过。"""
        module = load_script("known_failures_struct", "known_failures.py")
        module.record("job-1", "no_section", "职位描述取不到正文", self.ledger_path)
        entry = module.get("job-1", self.ledger_path)
        self.assertEqual(entry["nature"], module.STRUCTURAL)
        self.assertTrue(module.is_known("job-1", self.ledger_path))
        module.begin_new_run_scope()
        self.assertTrue(module.is_known("job-1", self.ledger_path),
                        "结构性失败跨轮仍应跳过")

    def test_ledger_entry_expires_after_ttl(self):
        """TTL 到期 → 条目失效 → 岗位重新出现（不会永远消失）。"""
        module = load_script("known_failures_ttl", "known_failures.py")
        module.record("job-1", "no_section", "职位描述取不到正文", self.ledger_path)
        self.assertTrue(module.is_known("job-1", self.ledger_path))
        with open(self.ledger_path, encoding="utf-8") as handle:
            data = json.load(handle)
        data["job-1"]["expires_at"] = time.time() - 1
        with open(self.ledger_path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        self.assertFalse(module.is_known("job-1", self.ledger_path),
                         "TTL 到期后条目必须失效")
        self.assertIsNone(module.get("job-1", self.ledger_path),
                          "过期条目应被清理")

    def test_max_skip_runs_invalidates_entry(self):
        """最大跳过轮数用尽 → 条目失效 → 岗位重新出现（误判的安全阀）。"""
        module = load_script("known_failures_maxskip", "known_failures.py")
        module.record("job-1", "no_section", "职位描述取不到正文", self.ledger_path)
        skips = 0
        while module.is_known("job-1", self.ledger_path) and skips < 50:
            skips += 1
            module.begin_new_run_scope()
        self.assertEqual(skips, module.MAX_SKIP_RUNS,
                         "跳过轮数必须受 MAX_SKIP_RUNS 约束")
        self.assertFalse(module.is_known("job-1", self.ledger_path))

    def test_retry_job_bypasses_the_ledger(self):
        """``--retry-job`` 必须真正绕过台账（此前是**假**逃生口）。"""
        module = load_script("known_failures_retry", "known_failures.py")
        module.record("job-1", "no_section", "职位描述取不到正文", self.ledger_path)
        self.assertTrue(module.is_known("job-1", self.ledger_path))
        with mock.patch.object(sys, "argv",
                               ["boss_cdp_raw.py", "--retry-job", "job-1"]):
            self.assertFalse(module.is_known("job-1", self.ledger_path),
                             "--retry-job 必须绕过台账")
        os.environ["BOSS_RETRY_JOB_IDS"] = "job-1"
        try:
            self.assertFalse(module.is_known("job-1", self.ledger_path),
                             "环境变量形式的强制重试名单同样要生效")
        finally:
            os.environ.pop("BOSS_RETRY_JOB_IDS", None)
        self.assertTrue(module.is_known("job-1", self.ledger_path),
                        "不在名单里的岗位仍然要跳过")

    def test_skip_prints_one_explanatory_line(self):
        """跳过必须**打印一行**说明（不能只有计数）。"""
        module = load_script("known_failures_print", "known_failures.py")
        module.record("job-1", "no_section", "职位描述取不到正文", self.ledger_path)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            module.is_known("job-1", self.ledger_path)
        text = buffer.getvalue()
        self.assertIn("坏页", text)
        self.assertIn("已跳过", text)
        self.assertIn("--retry-job", text)
        self.assertIn("job-1", text)

    def test_legacy_entries_without_nature_are_not_permanent(self):
        """旧台账条目（无 nature 字段）→ 不再永久拉黑（历史黑名单自动失效）。"""
        module = load_script("known_failures_legacy", "known_failures.py")
        with open(self.ledger_path, "w", encoding="utf-8") as handle:
            json.dump({"job-legacy": {
                "fingerprint": "unknown", "reason": "老版本写的",
                "first_seen": "2026-10-01", "hits": 9}}, handle)
        self.assertFalse(module.is_known("job-legacy", self.ledger_path),
                         "旧条目不得继续永久拉黑岗位")

    def test_transient_failure_in_another_process_is_retried_next_run(self):
        """另一个进程（= 下一轮）记的瞬时失败，本进程必须重新尝试。"""
        script_path = str(SCRIPTS / "known_failures.py")
        ctx = multiprocessing.get_context("spawn")
        queue = ctx.Queue()
        proc = ctx.Process(target=record_transient_worker,
                           args=(script_path, self.ledger_path, "job-x", queue))
        proc.start()
        proc.join(timeout=60)
        status, value = queue.get(timeout=30)
        self.assertEqual(proc.exitcode, 0)
        self.assertEqual(status, "ok", f"子进程报错：{value}")
        self.assertTrue(value, "同一轮（子进程）内应跳过")

        module = load_script("known_failures_crossproc", "known_failures.py")
        self.assertFalse(module.is_known("job-x", self.ledger_path),
                         "下一轮（另一个进程）必须重新尝试，不能永久拉黑")


# ---------------------------------------------------------------------------
# Bug 3：dup_group 跨进程稳定
# ---------------------------------------------------------------------------

class DupGroupStabilityTests(IsolatedStateTestCase):

    def test_dup_group_is_stable_across_processes(self):
        """不同 PYTHONHASHSEED 的进程必须算出**同一个**组号。"""
        script_path = str(SCRIPTS / "semantics.py")
        ctx = multiprocessing.get_context("spawn")
        queue = ctx.Queue()
        processes = []
        for _index in range(4):
            proc = ctx.Process(target=dup_group_worker, args=(script_path, queue))
            processes.append(proc)
        for index, proc in enumerate(processes):
            # 每个子进程用不同的哈希种子 → 内置 hash() 必然不同（复现旧缺陷）
            os.environ["PYTHONHASHSEED"] = str(index + 1)
            proc.start()
        for proc in processes:
            proc.join(timeout=120)
        results = [queue.get(timeout=30) for _ in processes]
        for proc in processes:
            self.assertEqual(proc.exitcode, 0, "子进程异常退出")
        for status, value in results:
            self.assertEqual(status, "ok", f"子进程报错：{value}")
        group_ids = {value for _status, value in results}
        self.assertEqual(len(group_ids), 1,
                         f"跨进程组号必须一致，实际={group_ids}")

        module = load_script("semantics_stable", "semantics.py")
        jobs = duplicate_sample_jobs()
        module.mark_duplicate_groups(jobs)
        self.assertEqual(jobs[0]["dup_group"], results[0][1])
        self.assertTrue(jobs[0]["dup_group"].startswith("dup-"))

    def test_same_input_yields_same_group_id_within_process(self):
        """同样输入两次跑出同样的组号（可复现）。"""
        module = load_script("semantics_repro", "semantics.py")
        first = duplicate_sample_jobs()
        second = duplicate_sample_jobs()
        module.mark_duplicate_groups(first)
        module.mark_duplicate_groups(second)
        self.assertEqual(first[0]["dup_group"], second[0]["dup_group"])
        self.assertEqual(first[0]["dup_group"], first[1]["dup_group"])
        self.assertNotEqual(first[0]["dup_group"], first[2]["dup_group"])


if __name__ == "__main__":
    unittest.main()
