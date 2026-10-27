"""爬取层低风险三项的行为锁定：

1. 批次健康度：每次 run 结束 append 一行 ``batch_health.jsonl`` + ``--batches`` 只读命令；
2. 详情断点续跑：``pending/details_<run_id>.json`` 的写入/恢复/清理（强杀安全）；
3. 模板预算：daily 的 ``max_details`` / ``detail_budget`` / ``daily_detail_budget`` 透传。

全离线：临时 HOME/状态目录/结果目录，不联网、不抓起真实 Chrome。
"""

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(ROOT))

ENV_KEYS = (
    "BOSS_STATE_DIR", "BOSS_RUN_STATE_PATH", "BOSS_AUDIT_PATH",
    "BOSS_DAILY_BUDGET_PATH", "BOSS_BATCH_HEALTH_PATH", "BOSS_PENDING_DIR",
    "BOSS_DAILY_PROFILES_DIR", "BOSS_DAILY_LOG_PATH",
)


def load_boss(name="boss_cdp_raw_batch_health"):
    spec = importlib.util.spec_from_file_location(
        name, SCRIPTS / "boss_cdp_raw.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_daily(name="daily_budget_template"):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / "daily.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class IsolatedEnvMixin(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="batch-health-")
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.state_dir = os.path.join(self.dir, "state")
        self.result_dir = os.path.join(self.dir, "job-result")
        os.makedirs(self.result_dir, exist_ok=True)
        self._env_backup = {key: os.environ.get(key) for key in ENV_KEYS}
        self.addCleanup(self._restore_env)
        os.environ["BOSS_STATE_DIR"] = self.state_dir
        os.environ["BOSS_RUN_STATE_PATH"] = os.path.join(
            self.state_dir, "run_state.json")
        os.environ["BOSS_AUDIT_PATH"] = os.path.join(
            self.state_dir, "risk_events.jsonl")
        os.environ["BOSS_DAILY_BUDGET_PATH"] = os.path.join(
            self.state_dir, "daily_budget.json")
        os.environ["BOSS_BATCH_HEALTH_PATH"] = os.path.join(
            self.state_dir, "batch_health.jsonl")
        os.environ["BOSS_PENDING_DIR"] = os.path.join(
            self.state_dir, "pending")

    def _restore_env(self):
        for key, value in self._env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def write_json(self, path, payload):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)


class BatchHealthRecordTests(IsolatedEnvMixin):

    def setUp(self):
        super().setUp()
        self.boss = load_boss()
        self.addCleanup(sys.modules.pop, "boss_cdp_raw_batch_health", None)
        patcher = mock.patch.object(self.boss, "DEFAULT_RESULT_DIR",
                                    self.result_dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.record = {
            "run_id": "run-1",
            "started_at": "2026-10-28T10:00:00+08:00",
            "finished_at": "2026-10-28T10:20:00+08:00",
            "mode": "single",
            "keyword": "AI产品实习生",
            "city": "杭州",
            "scope_key": "杭州|intern|AI产品实习生",
            "list_pages": 3,
            "detail_ok": 12,
            "detail_failed": 1,
            "budget_used": 45,
            "risk_events": 0,
            "status": "success",
        }

    def test_append_and_read_roundtrip(self):
        self.assertTrue(self.boss.append_batch_health(self.record))
        path = self.boss.batch_health_path()
        self.assertTrue(os.path.exists(path))
        with open(path, encoding="utf-8") as handle:
            lines = [line for line in handle if line.strip()]
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0]), self.record)
        records = self.boss.read_batch_health()
        self.assertEqual(records, [self.record])
        self.assertEqual(tuple(self.record.keys()),
                         self.boss.BATCH_HEALTH_FIELDS)

    def test_read_skips_bad_lines_and_applies_limit(self):
        path = self.boss.batch_health_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("not json\n")
            for index in range(3):
                handle.write(json.dumps(
                    dict(self.record, run_id=f"r{index}")) + "\n")
            handle.write("\n")
        self.assertEqual(len(self.boss.read_batch_health()), 3)
        tail = self.boss.read_batch_health(2)
        self.assertEqual([item["run_id"] for item in tail], ["r1", "r2"])

    def test_append_failure_is_best_effort(self):
        blocker = os.path.join(self.dir, "blocked")
        os.makedirs(blocker, exist_ok=True)
        os.environ["BOSS_BATCH_HEALTH_PATH"] = blocker  # 路径是目录 → 写失败
        self.assertFalse(self.boss.append_batch_health(self.record))

    def test_format_batches_human_and_empty(self):
        self.assertIn("还没有批次健康度记录",
                      self.boss.format_batches([]))
        text = self.boss.format_batches([self.record], total=1)
        self.assertIn("批次健康度", text)
        self.assertIn("✅", text)
        self.assertIn("杭州|intern|AI产品实习生", text)
        self.assertIn("预算 45", text)


class BatchHealthLifecycleTests(IsolatedEnvMixin):
    """run 结束（成功/失败/中断）都要 append 一行 —— 走 _run_cli_with_db_cleanup。"""

    def setUp(self):
        super().setUp()
        self.boss = load_boss("boss_cdp_raw_health_lifecycle")
        self.addCleanup(sys.modules.pop, "boss_cdp_raw_health_lifecycle", None)
        today = time.strftime("%Y-%m-%d", time.localtime())
        self.write_json(os.environ["BOSS_DAILY_BUDGET_PATH"],
                        {"date": today, "detail": 12, "jobs": [], "seq": 2})

    def _args(self):
        return types.SimpleNamespace(
            input=None, keyword="AI产品实习生", city="杭州",
            recruit_type="intern")

    def _run_wrapper(self, impl):
        args = self._args()
        with mock.patch.object(self.boss, "_run_cli_impl", side_effect=impl):
            try:
                self.boss._run_cli_with_db_cleanup(None, args)
            except BaseException as exc:  # noqa: BLE001 - 测试要观察中断类型
                return args, type(exc).__name__
        return args, None

    def _last_record(self):
        records = self.boss.read_batch_health()
        self.assertTrue(records, "收尾应 append 一条批次健康度")
        return records[-1]

    def test_success_run_appends_success_record(self):
        boss = self.boss

        def impl(_parser, args):
            boss._begin_batch_health(args)
            boss._update_batch_health(args, list_pages=3)
            return "ok"

        _, exc_name = self._run_wrapper(impl)
        self.assertIsNone(exc_name)
        record = self._last_record()
        self.assertEqual(record["status"], "success")
        self.assertEqual(record["mode"], "single")
        self.assertEqual(record["keyword"], "AI产品实习生")
        self.assertEqual(record["city"], "杭州")
        self.assertEqual(record["scope_key"], "杭州|intern|AI产品实习生")
        self.assertEqual(record["list_pages"], 3)
        self.assertEqual(record["budget_used"], 12)
        self.assertIsNotNone(record["finished_at"])
        self.assertEqual(record["run_id"], self._last_record()["run_id"])

    def test_failed_run_appends_failed_record(self):
        boss = self.boss

        def impl(_parser, args):
            boss._begin_batch_health(args)
            raise SystemExit(1)

        _, exc_name = self._run_wrapper(impl)
        self.assertEqual(exc_name, "SystemExit")
        self.assertEqual(self._last_record()["status"], "failed")

    def test_interrupted_run_appends_aborted_record(self):
        boss = self.boss

        def impl(_parser, args):
            boss._begin_batch_health(args)
            raise KeyboardInterrupt()

        _, exc_name = self._run_wrapper(impl)
        self.assertEqual(exc_name, "KeyboardInterrupt")
        self.assertEqual(self._last_record()["status"], "aborted")

    def test_risk_events_count_this_run_only(self):
        boss = self.boss
        boss.record_audit_event("alert", note="before-run")  # 基线前已有 1 条

        def impl(_parser, args):
            boss._begin_batch_health(args)
            boss.record_audit_event("alert", note="login")
            boss.record_audit_event("cooldown", note="risk")
            return "ok"

        self._run_wrapper(impl)
        self.assertEqual(self._last_record()["risk_events"], 2)

    def test_no_record_for_readonly_run(self):
        """未 begin 的 run（只读命令）不应产生健康度行。"""
        self.assertIsNone(self.boss._finalize_batch_health(
            types.SimpleNamespace()))
        self.assertEqual(self.boss.read_batch_health(), [])


class BatchesCliTests(IsolatedEnvMixin):

    def setUp(self):
        super().setUp()
        self.boss = load_boss("boss_cdp_raw_batches_cli")
        self.addCleanup(sys.modules.pop, "boss_cdp_raw_batches_cli", None)
        patcher = mock.patch.object(self.boss, "DEFAULT_RESULT_DIR",
                                    self.result_dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        for index in range(3):
            self.boss.append_batch_health({
                "run_id": f"r{index}", "started_at": f"t{index}",
                "finished_at": f"t{index}", "mode": "single",
                "keyword": "AI", "city": "杭州", "scope_key": "杭州|all|AI",
                "list_pages": 1, "detail_ok": index, "detail_failed": 0,
                "budget_used": index, "risk_events": 0, "status": "success"})

    def _run_cli(self, argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        code = None
        with mock.patch.object(sys, "argv", ["boss_cdp_raw.py", *argv]), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            try:
                self.boss.run_cli()
            except SystemExit as exc:
                code = 0 if exc.code is None else exc.code
        return code, stdout.getvalue(), stderr.getvalue()

    def test_batches_json_default_and_limit(self):
        with mock.patch.object(self.boss, "acquire_scrape_lock") as lock:
            code, out, err = self._run_cli(["--batches", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        payload = json.loads(out)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["total"], 3)
        self.assertEqual(payload["count"], 3)
        lock.assert_not_called()

        code, out, _ = self._run_cli(["--batches", "2", "--json"])
        payload = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(payload["count"], 2)
        self.assertEqual([item["run_id"] for item in payload["batches"]],
                         ["r1", "r2"])

    def test_batches_human_output(self):
        code, out, _ = self._run_cli(["--batches"])
        self.assertEqual(code, 0)
        self.assertIn("批次健康度", out)
        self.assertIn("杭州|all|AI", out)

    def test_batches_invalid_values_exit_2(self):
        code, _, err = self._run_cli(["--batches", "abc"])
        self.assertEqual(code, 2)
        code, _, err = self._run_cli(["--batches", "0"])
        self.assertEqual(code, 2)
        self.assertIn("正整数", err)

    def test_batches_parser_const_default(self):
        args = self.boss.build_parser().parse_args(["--batches"])
        self.assertEqual(args.batches, 10)
        self.assertIsNone(self.boss.build_parser().parse_args([]).batches)


class DetailResumeQueueTests(IsolatedEnvMixin):

    def setUp(self):
        super().setUp()
        self.boss = load_boss("boss_cdp_raw_resume_queue")
        self.addCleanup(sys.modules.pop, "boss_cdp_raw_resume_queue", None)
        self.list_data = {
            "keyword": "产品实习生",
            "city": "杭州",
            "jobs": [
                {"job_id": "j0", "job_link": "https://x/0.html"},
                {"job_id": "j1", "job_link": "https://x/1.html"},
                {"job_id": "j2", "job_link": "https://x/2.html"},
            ],
        }

    def _pending_files(self):
        directory = self.boss.detail_pending_dir()
        try:
            return sorted(name for name in os.listdir(directory)
                          if name.endswith(".json"))
        except OSError:
            return []

    def test_prepare_writes_expected_payload(self):
        queue = self.boss.prepare_detail_resume_queue(
            self.list_data, run_id="r1", recruit_type="intern")
        self.assertEqual(os.path.basename(queue.path), "details_r1.json")
        with open(queue.path, encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertEqual(data["format_version"], 1)
        self.assertEqual(data["run_id"], "r1")
        self.assertEqual(data["keyword"], "产品实习生")
        self.assertEqual(data["city"], "杭州")
        self.assertEqual(data["recruit_type"], "intern")
        self.assertEqual(data["scope_key"], "杭州|intern|产品实习生")
        self.assertEqual(data["job_ids"], ["j0", "j1", "j2"])
        self.assertTrue(data["created_at"])
        self.assertFalse([name for name in self._pending_files()
                          if ".tmp" in name])

    def test_mark_done_removes_and_finish_deletes_file(self):
        queue = self.boss.DetailResumeQueue(
            run_id="r1", scope_key="杭州|intern|产品实习生",
            keyword="产品实习生", city="杭州", recruit_type="intern",
            job_ids=["j0", "j1"])
        queue.save()
        self.assertTrue(os.path.exists(queue.path))
        self.assertTrue(queue.mark_done("j0"))
        with open(queue.path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["job_ids"], ["j1"])
        self.assertFalse(queue.mark_done("not-in-queue"))
        self.assertTrue(queue.mark_done("j1"))
        self.assertFalse(os.path.exists(queue.path), "全部完成应删除断点文件")

    def test_killed_run_resumes_from_remaining_ids(self):
        """模拟强杀：第一条成功、第二条没跑完 → 下次同 scope 优先抓剩下的。"""
        first = self.boss.prepare_detail_resume_queue(
            self.list_data, run_id="r1", recruit_type="intern")
        first.mark_done("j0")  # 进程在 j1 之前被强杀
        first.mark_done("j1")
        # 进程已死（没有 finish），文件应仍留在磁盘
        self.assertTrue(os.path.exists(first.path))
        second = self.boss.prepare_detail_resume_queue(
            self.list_data, run_id="r2", recruit_type="intern")
        self.assertEqual(second.pending_ids, ["j2", "j0", "j1"])
        self.assertFalse(os.path.exists(first.path), "旧断点应在合并后清理")
        self.assertEqual(self.boss.load_detail_resume(
            "杭州|intern|产品实习生")[0], ["j2", "j0", "j1"])

    def test_existing_jd_ids_are_not_queued(self):
        queue = self.boss.prepare_detail_resume_queue(
            self.list_data, run_id="r1", recruit_type="intern",
            existing_ids={"j0", "j2"})
        self.assertEqual(queue.pending_ids, ["j1"])

    def test_other_scope_pending_is_untouched(self):
        intern = self.boss.prepare_detail_resume_queue(
            self.list_data, run_id="r1", recruit_type="intern")
        intern.mark_done("j0")
        intern_path = intern.path
        campus = self.boss.prepare_detail_resume_queue(
            self.list_data, run_id="r2", recruit_type="campus")
        self.assertEqual(campus.pending_ids, ["j0", "j1", "j2"])
        self.assertTrue(os.path.exists(intern_path), "不同 scope 不应互相消费")
        with open(intern_path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["job_ids"], ["j1", "j2"])

    def test_previous_ids_missing_from_current_list_are_dropped(self):
        first = self.boss.prepare_detail_resume_queue(
            self.list_data, run_id="r1", recruit_type="intern")
        first.mark_done("j0")
        smaller = dict(self.list_data, jobs=[self.list_data["jobs"][0]])
        second = self.boss.prepare_detail_resume_queue(
            smaller, run_id="r2", recruit_type="intern")
        self.assertEqual(second.pending_ids, ["j0"])

    def test_corrupt_pending_file_is_ignored(self):
        directory = self.boss.detail_pending_dir()
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "details_broken.json"), "w",
                  encoding="utf-8") as handle:
            handle.write("{not json")
        queue = self.boss.prepare_detail_resume_queue(
            self.list_data, run_id="r1", recruit_type="intern")
        self.assertEqual(queue.pending_ids, ["j0", "j1", "j2"])

    def test_order_jobs_for_resume_keeps_queue_priority(self):
        jobs = self.list_data["jobs"]
        ordered = self.boss.order_jobs_for_resume(jobs, ["j2", "j0"])
        self.assertEqual([job["job_id"] for job in ordered],
                         ["j2", "j0", "j1"])
        self.assertEqual(self.boss.order_jobs_for_resume(jobs, []),
                         jobs)
        self.assertEqual(self.boss.order_jobs_for_resume(jobs, ["nope"]),
                         jobs)


class ScrapeDetailsResumeTests(IsolatedEnvMixin):
    """详情抓取主循环接线：断点优先 + 成功一条移除 + 结束清理。"""

    def setUp(self):
        super().setUp()
        self.boss = load_boss("boss_cdp_raw_resume_scrape")
        self.addCleanup(sys.modules.pop, "boss_cdp_raw_resume_scrape", None)
        self.jobs = [
            {"job_id": f"j{index}", "title": f"T{index}",
             "job_link": f"https://www.zhipin.com/job_detail/x{index}.html",
             "boss_name": "C"}
            for index in range(3)
        ]
        self.list_data = {"keyword": "产品实习生", "city": "杭州",
                          "jobs": self.jobs}

    def _fake_one(self, processed, failures=()):
        def fake(job, cdp_port, stop_event=None, limiter=None, verbose=False,
                 security_id=None, api_session=None, city_code="",
                 search_keyword="", id_mode="security", dom_session=None):
            job_id = job["job_id"]
            processed.append(job_id)
            if job_id in failures:
                return {"ok": False, "detail": None, "job_id": job_id,
                        "reason": "invalid_detail", "message": "坏页"}
            return {"ok": True,
                    "detail": {"job_id": job_id, "title": job["title"],
                               "jd": "x" * 200},
                    "job_id": job_id, "reason": "", "message": "",
                    "channel": "dom"}
        return fake

    def _scrape(self, queue, processed, failures=()):
        out = os.path.join(self.dir, "details.json")
        tab = [mock.Mock(), "t", "s", 0]
        with mock.patch.object(self.boss, "_scrape_one_detail",
                               new=self._fake_one(processed, failures)), \
                mock.patch.object(self.boss, "_open_dom_tab",
                                  return_value=tab), \
                mock.patch.object(self.boss, "_close_api_tab"), \
                mock.patch.object(self.boss, "load_existing_detail_ids",
                                  return_value={"j1"}), \
                mock.patch.object(self.boss, "load_pending_ids",
                                  return_value={}), \
                mock.patch.object(self.boss.time, "sleep"), \
                contextlib.redirect_stdout(io.StringIO()):
            results = self.boss.scrape_details(
                self.list_data, output_path=out, cdp_port=9222,
                concurrency=1, detail_channel="dom", resume_queue=queue)
        return results

    def test_queue_ids_go_first_and_all_done_deletes_file(self):
        queue = self.boss.DetailResumeQueue(
            run_id="r1", scope_key="杭州|intern|产品实习生",
            keyword="产品实习生", city="杭州", recruit_type="intern",
            job_ids=["j2", "j0"])
        queue.save()
        processed = []
        results = self._scrape(queue, processed)
        # 断点内 j2/j0 优先；j1 已在库里 → 跳过（不抓也不占队列）
        self.assertEqual(processed, ["j2", "j0"])
        self.assertEqual(len(results), 2)
        self.assertFalse(os.path.exists(queue.path))
        self.assertEqual(self.boss.LAST_DETAIL_RUN_STATS["ok"], 2)

    def test_failed_job_stays_in_queue_for_next_run(self):
        queue = self.boss.DetailResumeQueue(
            run_id="r1", scope_key="杭州|intern|产品实习生",
            keyword="产品实习生", city="杭州", recruit_type="intern",
            job_ids=["j0", "j2"])
        queue.save()
        processed = []
        self._scrape(queue, processed, failures={"j0"})
        self.assertEqual(processed, ["j0", "j2"])
        self.assertTrue(os.path.exists(queue.path), "未完成的断点必须保留")
        with open(queue.path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["job_ids"], ["j0"])
        self.assertEqual(self.boss.LAST_DETAIL_RUN_STATS["ok"], 1)
        self.assertEqual(self.boss.LAST_DETAIL_RUN_STATS["failed"], 1)


class DailyTemplateBudgetTests(IsolatedEnvMixin):

    def setUp(self):
        super().setUp()
        self.daily = load_daily()
        self.addCleanup(sys.modules.pop, "daily_budget_template", None)
        self.config_path = os.path.join(self.dir, "profile.json")
        self.base = {
            "name": "测试日常", "city": "杭州",
            "keywords": ["产品实习生"], "pages": 2,
            "recruit_types": ["intern"],
            "deliverable": "boss_jobs_日常.json",
        }

    def _load(self, **extra):
        self.write_json(self.config_path, dict(self.base, **extra))
        return self.daily.load_config(self.config_path)

    def _argv(self, cfg):
        return self.daily.build_scrape_argv(cfg, python="python")

    def test_unconfigured_budget_keeps_default_argv(self):
        cfg = self._load()
        argv = self._argv(cfg)
        self.assertNotIn("--max-details", argv)
        self.assertNotIn("--daily-detail-budget", argv)
        self.assertIsNone(cfg["max_details"])
        self.assertIsNone(cfg["detail_budget"])
        self.assertIsNone(cfg["daily_detail_budget"])

    def test_max_details_is_passed_through(self):
        cfg = self._load(max_details=8)
        self.assertEqual(cfg["max_details"], 8)
        argv = self._argv(cfg)
        index = argv.index("--max-details")
        self.assertEqual(argv[index + 1], "8")

    def test_detail_budget_alias_is_passed_through(self):
        cfg = self._load(detail_budget="5")
        self.assertEqual(cfg["detail_budget"], 5)
        argv = self._argv(cfg)
        index = argv.index("--max-details")
        self.assertEqual(argv[index + 1], "5")

    def test_daily_detail_budget_is_passed_through(self):
        cfg = self._load(daily_detail_budget=300)
        argv = self._argv(cfg)
        index = argv.index("--daily-detail-budget")
        self.assertEqual(argv[index + 1], "300")

    def test_ambiguous_or_invalid_budget_raises(self):
        with self.assertRaises(ValueError):
            self._load(max_details=3, detail_budget=5)
        with self.assertRaises(ValueError):
            self._load(max_details=0)
        with self.assertRaises(ValueError):
            self._load(max_details="abc")
        with self.assertRaises(ValueError):
            self._load(daily_detail_budget=-1)


if __name__ == "__main__":
    unittest.main()
