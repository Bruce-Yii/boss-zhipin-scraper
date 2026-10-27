"""Agent 日常运行支撑的行为锁定：运行进度 / 今日简报 / 日常作业（issue #189）。

全离线：临时状态路径 + 临时 DB/产物目录，不碰真实运行态与网络。
"""

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(ROOT))

JD = "负责AI产品规划与设计。" * 20


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _job(jid, title="AI产品经理", job_type="4", with_jd=False):
    job = {
        "job_id": jid, "title": title, "company_name": "公司",
        "salary": "20-30K", "location": "杭州",
        "job_link": f"https://x/{jid}.html", "job_type": job_type,
    }
    if with_jd:
        job["jd"] = JD
    return job


def write_json(path, payload):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)


class IsolatedEnvMixin(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="agent-ops-")
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self._env_backup = {}
        for key in ("BOSS_RUN_STATE_PATH", "BOSS_DAILY_BUDGET_PATH",
                    "BOSS_AUDIT_PATH", "BOSS_KNOWN_BAD_PATH", "BOSS_DAILY_LOG_PATH"):
            self._env_backup[key] = os.environ.get(key)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        for key, value in self._env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _set_env(self, **values):
        for key, value in values.items():
            os.environ[key] = str(value)


class RunStateTests(IsolatedEnvMixin):

    def setUp(self):
        super().setUp()
        self.module = load_module("run_state", SCRIPTS / "run_state.py")
        self.state_path = os.path.join(self.dir, "run_state.json")
        self._set_env(BOSS_RUN_STATE_PATH=self.state_path)

    def test_start_update_finish_roundtrip(self):
        self.module.start(phase="list", keywords_total=2, pages_total=3)
        state = self.module.read()
        self.assertTrue(state["running"])
        self.assertEqual(state["phase"], "list")
        self.assertTrue(os.path.exists(self.state_path))
        self.module.update(phase="detail", detail_total=10, detail_done=3,
                           detail_ok=2)
        state = self.module.read()
        self.assertEqual(state["detail_done"], 3)
        self.assertEqual(state["started_at"], state["started_at"])
        self.module.finish(phase="done", message="完成")
        state = self.module.read()
        self.assertFalse(state["running"])
        self.assertEqual(state["phase"], "done")

    def test_progress_view_pct_and_stale(self):
        self.module.start(phase="detail", detail_total=10, detail_ok=0)
        self.module.update(detail_done=5, detail_ok=4)
        view = self.module.progress_view(self.module.read())
        self.assertEqual(view["progress_pct"], 50)
        self.assertEqual(view["state"], "running")
        # 伪造 1 小时未更新 → stale
        state = self.module.read()
        state["updated_at"] = (
            datetime.now().astimezone() - timedelta(hours=1)
        ).isoformat(timespec="seconds")
        stale_view = self.module.progress_view(state, stale_seconds=300)
        self.assertTrue(stale_view["stale"])
        self.assertEqual(stale_view["state"], "stale")

    def test_missing_state_and_json_output(self):
        self.assertIsNone(self.module.read())
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = self.module.run_progress(as_json=True)
        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["state"], "unknown")
        self.assertTrue(payload["stale"])

    def test_write_disabled_under_tests_without_explicit_path(self):
        """测试进程里默认不写真实状态文件；显式 BOSS_RUN_STATE_PATH 才写。"""
        self.assertFalse(self.module._write_disabled())  # setUp 显式给了路径
        os.environ.pop("BOSS_RUN_STATE_PATH", None)
        self.assertTrue(self.module._write_disabled())


class BriefTests(IsolatedEnvMixin):

    def setUp(self):
        super().setUp()
        self.module = load_module("brief", SCRIPTS / "brief.py")
        self.db_store = load_module("db_store_brief", SCRIPTS / "db_store.py")
        self.daily_budget = load_module("daily_budget_brief",
                                        SCRIPTS / "daily_budget.py")
        self.db_path = os.path.join(self.dir, "boss.db")
        self.result_dir = os.path.join(self.dir, "job-result")
        os.makedirs(self.result_dir, exist_ok=True)
        self._set_env(
            BOSS_DAILY_BUDGET_PATH=os.path.join(self.dir, "budget.json"),
            BOSS_AUDIT_PATH=os.path.join(self.dir, "risk_events.jsonl"),
            BOSS_KNOWN_BAD_PATH=os.path.join(self.dir, "known_bad.json"),
        )
        now_local = datetime.now().astimezone()
        self.now_utc = now_local.astimezone(UTC).isoformat(timespec="seconds")
        self.yesterday_utc = (now_local - timedelta(days=1)).astimezone(
            UTC).isoformat(timespec="seconds")
        self.today = now_local.date().isoformat()

    def _seed_db(self):
        conn = self.db_store.open_store(self.db_path)
        try:
            self.db_store.upsert_jobs(
                conn, [_job("a", title="AI产品经理"),
                       _job("b", title="ai产品经理")],
                run_id="r1", keyword="AI产品经理实习生", city="杭州",
                now=self.now_utc)
            self.db_store.upsert_details(
                conn, [{"job_id": "a", "jd": JD}, {"job_id": "b", "jd": JD}],
                run_id="r1", now=self.now_utc)
            self.db_store.upsert_jobs(
                conn, [_job("old", title="AI产品经理")],
                run_id="r0", keyword="旧关键词", city="杭州",
                now=self.yesterday_utc)
            self.db_store.record_run(conn, "r1", mode="single",
                                     keyword="AI产品经理实习生", city="杭州",
                                     job_count=2, detail_count=2)
        finally:
            conn.close()

    def _write_deliverable(self, name, ids, scope_key="杭州|intern|测试"):
        jobs = [_job(jid, with_jd=True) for jid in ids]
        write_json(os.path.join(self.result_dir, name), {
            "keyword": "AI产品经理实习", "city": "杭州", "jobs": jobs,
            "job_count": len(jobs),
            "artifact": {"kind": "deliverable", "created_at": self.now_utc,
                         "scope_key": scope_key,
                         "scope": {"city": "杭州", "keyword": "AI产品经理实习",
                                   "label": "测试", "recruit_types": ["intern"]}},
        })

    def test_collect_counts_titles_and_deliverable_diff(self):
        self._seed_db()
        self._write_deliverable("old_deliverable.json", ["a", "b"])
        self._write_deliverable("new_deliverable.json", ["b", "c"])
        write_json(os.path.join(self.dir, "budget.json"), {
            "date": self.today, "detail": 7, "jobs": [], "seq": 1})
        write_json(os.path.join(self.dir, "known_bad.json"), {"job-x": {}})
        with open(os.environ["BOSS_AUDIT_PATH"], "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"ts": self.now_utc, "event": "alert"},
                                    ensure_ascii=False) + "\n")

        report = self.module.collect(result_dir=self.result_dir,
                                     db_path=self.db_path)
        self.assertEqual(report["date"], self.today)
        self.assertEqual(report["new_jobs"]["count"], 2)
        # AI产品经理 / ai产品经理 归一化后归为一组，展示名取最常见原始标题
        self.assertEqual(report["new_jobs"]["by_title"][0]["key"], "ai产品经理")
        self.assertEqual(report["new_jobs"]["by_title"][0]["count"], 2)
        self.assertEqual(report["new_jobs"]["by_recruit"].get("intern"), 2)
        self.assertEqual(report["details"]["with_jd"], 2)
        self.assertEqual(report["budget"]["used"], 7)
        self.assertEqual(report["budget"]["remaining"], 693)
        self.assertEqual(report["anomalies"]["risk_events"], 1)
        self.assertEqual(report["anomalies"]["known_bad"], 1)
        # 同口径两版：当前版 {b,c} vs 上一版 {a,b} → +1/-1/保持 1
        scope = report["deliverables"][0]
        self.assertEqual(scope["scope_key"], "杭州|intern|测试")
        self.assertEqual(scope["new_today"], 2)
        self.assertIsNotNone(scope["changes"])
        self.assertEqual(scope["changes"]["added"], 1)
        self.assertEqual(scope["changes"]["removed"], 1)
        self.assertEqual(scope["changes"]["kept"], 1)

    def test_format_and_json_are_stable(self):
        self._seed_db()
        self._write_deliverable("d.json", ["a", "b"])
        text = self.module.format_brief(
            self.module.collect(result_dir=self.result_dir, db_path=self.db_path))
        self.assertIn("今日简报", text)
        self.assertIn("岗位结构", text)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = self.module.run_brief(result_dir=self.result_dir,
                                         db_path=self.db_path, as_json=True)
        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue())
        for key in ("date", "runs", "new_jobs", "details", "budget",
                    "deliverables", "anomalies"):
            self.assertIn(key, payload)


class DailyTests(IsolatedEnvMixin):

    def setUp(self):
        super().setUp()
        self.module = load_module("daily", SCRIPTS / "daily.py")
        self.config_path = os.path.join(self.dir, "daily.json")
        self._set_env(BOSS_RUN_STATE_PATH=os.path.join(self.dir, "run_state.json"),
                      BOSS_DAILY_LOG_PATH=os.path.join(self.dir, "daily.log"))
        self.config = {
            "_comment": "测试配置",
            "name": "测试日常",
            "city": "杭州",
            "keywords": ["产品实习生", "AI产品实习生"],
            "pages": 2,
            "recruit_types": ["intern"],
            "scope": "测试口径",
            "scope_key": "杭州|intern|测试",
            "deliverable": "boss_jobs_日常.json",
        }
        write_json(self.config_path, self.config)

    def test_load_config_and_argv(self):
        cfg = self.module.load_config(self.config_path)
        self.assertEqual(cfg["city"], "杭州")
        self.assertEqual(len(cfg["keywords"]), 2)
        scrape = self.module.build_scrape_argv(cfg, python="python")
        self.assertIn("产品实习生,AI产品实习生", scrape)
        self.assertIn("--recruit-type", scrape)
        deliver = self.module.build_deliverable_argv(cfg, python="python",
                                                     result_dir=self.dir)
        self.assertIn("--from-db", deliver)
        self.assertIn("--scope-key", deliver)
        with self.assertRaises(ValueError):
            self.module.load_config(os.path.join(self.dir, "nope.json"))
        write_json(os.path.join(self.dir, "bad.json"),
                   {"city": "杭州", "deliverable": "x.json"})
        with self.assertRaises(ValueError):
            self.module.load_config(os.path.join(self.dir, "bad.json"))

    def test_run_daily_success(self):
        fake = mock.Mock(returncode=0, stdout="ok", stderr="")
        with mock.patch.object(self.module.subprocess, "run", return_value=fake) as run, \
                mock.patch.object(self.module._artifacts, "refresh_index",
                                  return_value={"latest": {}}), \
                mock.patch.object(self.module._brief, "collect",
                                  return_value={"new_jobs": {"count": 1}}):
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = self.module.run_daily(self.config_path, as_json=True)
        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(len(payload["steps"]), 2)
        self.assertEqual(payload["deliverable"]["target"], "boss_jobs_日常.json")
        self.assertEqual(run.call_count, 2)
        state = json.load(open(os.environ["BOSS_RUN_STATE_PATH"], encoding="utf-8"))
        self.assertFalse(state["running"])
        self.assertEqual(state["phase"], "done")

    def test_run_daily_scrape_failure(self):
        fake = mock.Mock(returncode=1, stdout="", stderr="boom")
        with mock.patch.object(self.module.subprocess, "run", return_value=fake):
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = self.module.run_daily(self.config_path, as_json=True)
        self.assertEqual(code, 1)
        payload = json.loads(buffer.getvalue())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["stage"], "scrape")

    def test_profiles_listing_and_name_resolution(self):
        """多套模板：不带名字只列模板；按名字/路径选；重名报错。"""
        profiles = os.path.join(self.dir, "profiles")
        os.makedirs(profiles, exist_ok=True)
        first = dict(self.config, name="杭州11词实习", city="杭州")
        second = dict(first, name="上海AI实习", city="上海",
                      deliverable="boss_jobs_上海.json")
        write_json(os.path.join(profiles, "hz.json"), first)
        write_json(os.path.join(profiles, "sh.json"), second)
        old = os.environ.get("BOSS_DAILY_PROFILES_DIR")
        os.environ["BOSS_DAILY_PROFILES_DIR"] = profiles
        self.addCleanup(self._restore_profiles_env, old)
        self.assertEqual(len(self.module.list_profiles()), 2)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = self.module.run_daily(None, as_json=True)
        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(len(payload["profiles"]), 2)
        self.assertTrue(self.module.resolve_config("杭州11词实习").endswith("hz.json"))
        self.assertTrue(self.module.resolve_config("sh").endswith("sh.json"))
        with self.assertRaises(ValueError):
            self.module.resolve_config("实习")  # 两个模板都含"实习" → 歧义

    def _restore_profiles_env(self, old):
        if old is None:
            os.environ.pop("BOSS_DAILY_PROFILES_DIR", None)
        else:
            os.environ["BOSS_DAILY_PROFILES_DIR"] = old

    def test_source_artifacts_argv(self):
        """source=artifacts 时用产物并集重建，并带上排除名单。"""
        cfg = self.module.load_config(self.config_path)
        cfg["source"] = "artifacts"
        cfg["exclude_job_ids_file"] = "~/exclude.json"
        deliver = self.module.build_deliverable_argv(cfg, python="python",
                                                     result_dir=self.dir)
        self.assertIn("--from-artifacts", deliver)
        self.assertNotIn("--from-db", deliver)
        self.assertIn("--exclude-job-ids-file", deliver)


if __name__ == "__main__":
    unittest.main()
