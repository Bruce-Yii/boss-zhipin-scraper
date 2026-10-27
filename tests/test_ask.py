"""交互层（scripts/ask.py）的行为锁定：agent 会话里的只读问答 API。

全离线：临时 HOME/状态目录/产物目录/DB/模板，不碰真实运行态、不发网络请求
（``ask_status`` 一律传 ``probe_cdp=False``），也不写任何产物。

覆盖：
- status/today/why/diff/templates/batches 六条命令；
- 缺文件/缺库/旧 schema（无 schema_meta、无 keyword/city 列）不抛异常；
- ``run_ask`` 统一入口 + ``format_answer`` 的纯 JSON 输出；
- why 的三种删除留痕（dropped_junk / dropped_no_jd / 人工剔除名单）与来源批次；
- diff 的 +新增/−消失/保持及逐条原因（规则剔除/无 JD/上版被剔除后保留）。
"""

import importlib.util
import json
import os
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from datetime import UTC, datetime

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(ROOT))

JD = "负责AI产品规划与设计，参与大模型应用落地。" * 12


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_json(path, payload):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)


def _job(job_id, title="AI产品经理", with_jd=True, **extra):
    payload = {
        "job_id": job_id,
        "title": title,
        "company_name": "某AI公司",
        "salary": "20-30K",
        "location": "杭州",
        "job_link": f"https://x/{job_id}.html",
        "job_type": 4,
        "direction": "ai_pm",
        "direction_evidence": ["AI", "AIGC"],
        "skills": ["SQL"],
        "recruit_type": "intern",
    }
    if with_jd:
        payload["jd"] = JD
    payload.update(extra)
    return payload


# ---------------------------------------------------------------------------
# 临时库
# ---------------------------------------------------------------------------

_V2_SCHEMA = """
CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE runs (
    run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
    mode TEXT, keyword TEXT, city TEXT,
    job_count INTEGER NOT NULL DEFAULT 0, detail_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE jobs (
    job_id TEXT PRIMARY KEY, payload TEXT NOT NULL, first_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL, run_id TEXT, keyword TEXT, city TEXT
);
CREATE TABLE details (
    job_id TEXT PRIMARY KEY, jd TEXT, payload TEXT NOT NULL,
    first_seen_at TEXT NOT NULL, updated_at TEXT NOT NULL, run_id TEXT
);
"""

_V1_SCHEMA = """
CREATE TABLE runs (
    run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
    mode TEXT, keyword TEXT, city TEXT,
    job_count INTEGER NOT NULL DEFAULT 0, detail_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE jobs (
    job_id TEXT PRIMARY KEY, payload TEXT NOT NULL, first_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL, run_id TEXT
);
CREATE TABLE details (
    job_id TEXT PRIMARY KEY, jd TEXT, payload TEXT NOT NULL,
    first_seen_at TEXT NOT NULL, updated_at TEXT NOT NULL, run_id TEXT
);
"""


def _utcnow():
    return datetime.now(UTC).isoformat(timespec="seconds")


def create_db(path, *, v2=True):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(_V2_SCHEMA if v2 else _V1_SCHEMA)
    if v2:
        conn.execute("INSERT INTO schema_meta(key, value) VALUES ('schema_version', '2')")
    conn.commit()
    return conn


def seed_run(conn, run_id="r1", started_at=None, keyword="AI产品经理实习生",
             city="杭州", job_count=2, detail_count=2, mode="single"):
    started_at = started_at or _utcnow()
    conn.execute(
        "INSERT INTO runs (run_id, started_at, finished_at, mode, keyword, city, "
        "job_count, detail_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, started_at, started_at, mode, keyword, city, job_count, detail_count))
    conn.commit()


def seed_job(conn, job, *, run_id="r1", v2=True, first_seen_at=None, keyword="AI产品经理实习生",
             city="杭州"):
    ts = first_seen_at or _utcnow()
    payload = json.dumps(job, ensure_ascii=False)
    if v2:
        conn.execute(
            "INSERT OR REPLACE INTO jobs (job_id, payload, first_seen_at, updated_at, "
            "run_id, keyword, city) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (job["job_id"], payload, ts, ts, run_id, keyword, city))
    else:
        conn.execute(
            "INSERT OR REPLACE INTO jobs (job_id, payload, first_seen_at, updated_at, run_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (job["job_id"], payload, ts, ts, run_id))
    conn.commit()


def seed_detail(conn, job_id, jd=JD, *, run_id="r1"):
    ts = _utcnow()
    conn.execute(
        "INSERT OR REPLACE INTO details (job_id, jd, payload, first_seen_at, updated_at, "
        "run_id) VALUES (?, ?, ?, ?, ?, ?)",
        (job_id, jd, json.dumps({"job_id": job_id, "jd": jd}, ensure_ascii=False),
         ts, ts, run_id))
    conn.commit()


class AskTestBase(unittest.TestCase):

    ENV_KEYS = ("BOSS_STATE_DIR", "BOSS_RESULT_DIR", "BOSS_DB_PATH",
                "BOSS_RUN_STATE_PATH", "BOSS_DAILY_BUDGET_PATH",
                "BOSS_DAILY_PROFILES_DIR", "BOSS_AUDIT_PATH",
                "BOSS_KNOWN_BAD_PATH", "BOSS_BATCH_HEALTH_PATH")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ask-layer-")
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.state_dir = os.path.join(self.dir, "state")
        self.result_dir = os.path.join(self.dir, "job-result")
        self.profiles_dir = os.path.join(self.dir, "profiles")
        self.db_path = os.path.join(self.state_dir, "boss.db")
        self.run_state_path = os.path.join(self.state_dir, "run_state.json")
        self.budget_path = os.path.join(self.state_dir, "daily_budget.json")
        self.audit_path = os.path.join(self.state_dir, "risk_events.jsonl")
        self.known_bad_path = os.path.join(self.state_dir, "known_bad.json")
        self.batch_health_path = os.path.join(self.state_dir, "batch_health.jsonl")
        os.makedirs(self.state_dir, exist_ok=True)
        os.makedirs(self.result_dir, exist_ok=True)
        self._env_backup = {key: os.environ.get(key) for key in self.ENV_KEYS}
        self.addCleanup(self._restore_env)
        os.environ.update({
            "BOSS_STATE_DIR": self.state_dir,
            "BOSS_RESULT_DIR": self.result_dir,
            "BOSS_DB_PATH": self.db_path,
            "BOSS_RUN_STATE_PATH": self.run_state_path,
            "BOSS_DAILY_BUDGET_PATH": self.budget_path,
            "BOSS_DAILY_PROFILES_DIR": self.profiles_dir,
            "BOSS_AUDIT_PATH": self.audit_path,
            "BOSS_KNOWN_BAD_PATH": self.known_bad_path,
            "BOSS_BATCH_HEALTH_PATH": self.batch_health_path,
        })
        self.ask = load_module("ask_under_test", SCRIPTS / "ask.py")

    def _restore_env(self):
        for key, value in self._env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    # ---- 造数据 ----

    def write_run_state(self, **fields):
        payload = {"running": False, "phase": "done", "started_at": _utcnow(),
                   "updated_at": _utcnow(), "message": "完成", "run_id": "",
                   "detail_total": 0, "detail_done": 0, "detail_ok": 0}
        payload.update(fields)
        write_json(self.run_state_path, payload)

    def write_budget(self, used=0):
        write_json(self.budget_path, {
            "date": datetime.now().astimezone().date().isoformat(),
            "detail": used, "jobs": [], "seq": 1})

    def write_lock(self, holders=("999999",), risk=False):
        lines = ["1", *holders]
        if risk:
            lines.append("risk")
        with open(os.path.join(self.state_dir, "scrape.lock"), "w",
                  encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")

    def write_cooldown(self, remaining=300):
        now = datetime.now().timestamp()
        with open(os.path.join(self.state_dir, "cdp.cooldown"), "w",
                  encoding="utf-8") as handle:
            handle.write(f"{now + remaining}\n{now + remaining + 120}\n")

    def deliverable(self, name, jobs, created_at, scope_key="杭州|intern|测试",
                    dropped_junk=None, dropped_no_jd=None, ruleset_version=None,
                    label="测试口径"):
        artifact = {
            "kind": "deliverable", "format_version": 1, "builder": "test",
            "created_at": created_at, "scope_key": scope_key,
            "scope": {"city": "杭州", "keyword": "", "label": label,
                      "recruit_types": ["intern"]},
        }
        if ruleset_version:
            artifact["ruleset_version"] = ruleset_version
        payload = {
            "keyword": "", "city": "杭州", "jobs": jobs,
            "job_count": len(jobs), "total": len(jobs),
            "dropped_junk": dropped_junk or [],
            "dropped_no_jd": dropped_no_jd or [],
            "artifact": artifact,
        }
        write_json(os.path.join(self.result_dir, name), payload)
        return payload

    def seed_db_v2(self, jobs, *, with_details=True):
        conn = create_db(self.db_path, v2=True)
        try:
            seed_run(conn, job_count=len(jobs), detail_count=len(jobs))
            for job in jobs:
                seed_job(conn, job)
                if with_details:
                    seed_detail(conn, job["job_id"])
        finally:
            conn.close()


class AskStatusTests(AskTestBase):

    def test_status_reads_run_budget_lock_cooldown_db_and_deliverable(self):
        self.seed_db_v2([_job("a"), _job("b")])
        self.write_run_state(running=False, phase="detail", detail_total=10,
                             detail_done=5, detail_ok=4, budget_used=7,
                             budget_limit=700, message="详情抓取中")
        self.write_budget(used=7)
        self.write_lock(holders=("999999",))
        self.write_cooldown(remaining=300)
        self.deliverable("boss_jobs_20261027_x.json", [_job("a"), _job("b")],
                         "2026-10-27T20:00:00+08:00",
                         ruleset_version="2026-10-28")

        payload = self.ask.ask_status(probe_cdp=False)

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["command"], "status")
        data = payload["data"]
        self.assertEqual(data["run"]["phase"], "detail")
        self.assertEqual(data["budget"]["used"], 7)
        self.assertEqual(data["budget"]["remaining"], 693)
        self.assertTrue(data["lock"]["held"])
        self.assertTrue(data["cooldown"]["active"])
        self.assertFalse(data["cdp"]["probed"])
        self.assertTrue(data["db"]["ok"])
        self.assertEqual(data["db"]["jobs"], 2)
        self.assertEqual(data["db"]["details_with_jd"], 2)
        self.assertEqual(len(data["deliverables"]), 1)
        self.assertEqual(data["deliverables"][0]["jobs"], 2)
        self.assertIn("待拍板", payload["human"])
        self.assertIn("详情预算 7/700", payload["human"])
        self.assertFalse(data["degraded"])

    def test_status_missing_everything_returns_ok_false_without_raising(self):
        missing_dir = os.path.join(self.dir, "nope")
        missing_db = os.path.join(self.dir, "nope.db")
        os.environ["BOSS_RUN_STATE_PATH"] = os.path.join(self.dir, "nope_state.json")

        payload = self.ask.ask_status(result_dir=missing_dir, db_path=missing_db,
                                      probe_cdp=False)

        self.assertFalse(payload["ok"])
        self.assertTrue(payload["errors"])
        self.assertIn("降级", payload["human"])
        self.assertIn("没有运行状态记录", "；".join(payload["errors"]))

    def test_status_old_db_without_new_columns_does_not_crash(self):
        conn = create_db(self.db_path, v2=False)  # 无 schema_meta / keyword / city
        try:
            seed_run(conn)
            seed_job(conn, _job("old"), v2=False)
        finally:
            conn.close()
        self.deliverable("boss_jobs_old.json", [_job("old")],
                         "2026-10-27T20:00:00+08:00")

        payload = self.ask.ask_status(probe_cdp=False)

        self.assertTrue(payload["ok"])
        self.assertTrue(payload["data"]["db"]["ok"])
        self.assertIsNone(payload["data"]["db"]["schema_version"])
        self.assertIn("schema 版本未标", payload["human"])


class AskTodayTests(AskTestBase):

    def test_today_is_structured_and_counts_new_jobs(self):
        self.seed_db_v2([_job("a", title="AI产品经理"), _job("b", title="ai产品经理")])
        self.write_budget(used=7)
        self.deliverable("boss_jobs_today.json", [_job("a"), _job("b")],
                         "2026-10-27T20:00:00+08:00")

        payload = self.ask.ask_today()

        self.assertTrue(payload["ok"])
        data = payload["data"]
        self.assertEqual(data["new_jobs"]["count"], 2)
        self.assertEqual(data["details"]["with_jd"], 2)
        self.assertEqual(data["budget"]["used"], 7)
        self.assertTrue(data["sources"]["result_dir_exists"])
        self.assertTrue(data["sources"]["db_exists"])
        self.assertIn("今日简报", payload["human"])
        self.assertIn("今日岗位结构", payload["human"])

    def test_today_missing_dir_and_db_degrades_without_crash(self):
        os.environ["BOSS_DAILY_PROFILES_DIR"] = os.path.join(self.dir, "no-profiles")
        payload = self.ask.ask_today(
            result_dir=os.path.join(self.dir, "no-dir"),
            db_path=os.path.join(self.dir, "no.db"))

        self.assertFalse(payload["ok"])
        self.assertTrue(payload["errors"])
        self.assertIn("降级", payload["human"])
        notes = "；".join(payload["data"]["notes"])
        self.assertIn("原材料库不存在", notes)


class AskWhyTests(AskTestBase):

    def test_why_kept_lists_deliverable_evidence_and_ruleset(self):
        job = _job("keep1")
        self.deliverable("boss_jobs_keep.json", [job],
                         "2026-10-27T20:00:00+08:00",
                         ruleset_version="2026-10-28")

        payload = self.ask.ask_why("keep1")

        self.assertTrue(payload["ok"])
        data = payload["data"]
        self.assertEqual(data["decision"], "kept")
        self.assertEqual(data["decision_label"], "保留（最新交付物）")
        self.assertEqual(data["kept_in"][0]["scope_key"], "杭州|intern|测试")
        self.assertEqual(data["job"]["title"], "AI产品经理")
        self.assertIn("AI", "；".join(data["evidence"]))
        self.assertEqual(data["ruleset_version"], "2026-10-28")
        self.assertEqual(data["ruleset_source"], "交付物盖章")
        self.assertIn("保留", payload["human"])

    def test_why_uses_latest_deliverable_when_older_had_it(self):
        """旧交付物里保留、最新口径已删：结论必须是"删除"（2026-10-28 修）。"""
        self.deliverable("boss_jobs_old.json", [_job("x1")],
                         "2026-10-26T10:00:00+08:00",
                         scope_key="杭州|intern|旧")
        self.deliverable("boss_jobs_new.json", [], "2026-10-28T10:00:00+08:00",
                         scope_key="杭州|intern|新",
                         dropped_junk=[{"job_id": "x1", "title": "AI新媒体运营",
                                        "company": "X", "reason": "新媒体岗（标题）"}])

        payload = self.ask.ask_why("x1")

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["decision"], "dropped")
        self.assertIn("删除", payload["human"])
        self.assertIn("最新交付物", payload["human"])

    def test_why_dropped_returns_rule_reason(self):
        self.deliverable(
            "boss_jobs_drop.json", [],
            "2026-10-27T20:00:00+08:00",
            dropped_junk=[{"job_id": "drop1", "title": "销售助理",
                           "company": "某公司", "reason": "销售岗（JD）"}])

        payload = self.ask.ask_why("drop1")

        self.assertTrue(payload["ok"])
        data = payload["data"]
        self.assertEqual(data["decision"], "dropped")
        self.assertEqual(data["dropped_in"][0]["reason"], "销售岗（JD）")
        self.assertIn("销售岗（JD）", payload["human"])

    def test_why_no_jd_and_excluded_and_db_only(self):
        self.deliverable("boss_jobs_nojd.json", [],
                         "2026-10-27T20:00:00+08:00",
                         dropped_no_jd=["nojd1"])
        payload = self.ask.ask_why("nojd1")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["decision"], "no_jd")
        self.assertIn("无 JD", payload["human"])

        exclude_path = os.path.join(self.dir, "exclude.json")
        write_json(exclude_path, {"name": "杂项", "_comment": "人工口径剔除",
                                  "job_ids": ["ex1"]})
        write_json(os.path.join(self.profiles_dir, "hz.json"), {
            "name": "杭州这一套", "city": "杭州", "keywords": ["产品实习生"],
            "deliverable": "boss_jobs_nojd.json",
            "exclude_job_ids_file": exclude_path})
        payload = self.ask.ask_why("ex1")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["decision"], "excluded")
        self.assertEqual(len(payload["data"]["excluded_in"]), 1)
        self.assertIn("人工剔除名单", payload["human"])

        # 旧库（无 schema_meta / keyword / city）：只能从 DB 找到记录
        conn = create_db(self.db_path, v2=False)
        try:
            seed_run(conn, run_id="r9", keyword="旧关键词", city="杭州")
            seed_job(conn, _job("dbonly", title="AI产品经理"), v2=False,
                     run_id="r9")
            seed_detail(conn, "dbonly", run_id="r9")
        finally:
            conn.close()
        payload = self.ask.ask_why("dbonly")
        self.assertTrue(payload["ok"])
        data = payload["data"]
        self.assertEqual(data["decision"], "db_only")
        self.assertTrue(data["db"]["found"])
        self.assertTrue(data["db"]["has_jd"])
        self.assertEqual(data["db"]["run_id"], "r9")
        self.assertEqual(data["db"]["run"]["keyword"], "旧关键词")
        self.assertIn("来源批次", payload["human"])

    def test_why_unknown_job_returns_ok_false_and_no_fabrication(self):
        payload = self.ask.ask_why("does-not-exist")

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["data"]["decision"], "unknown")
        self.assertTrue(payload["errors"])
        self.assertIn("没有编造原因", payload["human"])
        self.assertTrue(payload["data"]["searched"])


class AskDiffTests(AskTestBase):

    def test_diff_two_versions_with_reasons(self):
        previous = [_job("a"), _job("b"), _job("c"), _job("d")]
        current = [_job("b"), _job("d")]
        self.deliverable(
            "boss_jobs_v1.json", previous, "2026-10-26T10:00:00+08:00",
            dropped_junk=[{"job_id": "d", "reason": "噪声（标题无岗名）"}])
        self.deliverable(
            "boss_jobs_v2.json", current, "2026-10-27T10:00:00+08:00",
            dropped_junk=[{"job_id": "c", "reason": "销售岗（JD）"}],
            dropped_no_jd=["a"])

        payload = self.ask.ask_diff(scope_key="杭州|intern|测试")

        self.assertTrue(payload["ok"])
        data = payload["data"]
        self.assertTrue(data["has_previous"])
        self.assertEqual(data["counts"]["added"], 0)
        self.assertEqual(data["counts"]["removed"], 2)
        self.assertEqual(data["counts"]["kept"], 2)
        removed = {item["job_id"]: item["reason"] for item in data["removed"]}
        self.assertIn("无 JD", removed["a"])
        self.assertIn("销售岗（JD）", removed["c"])
        self.assertIn("差异", payload["human"])

    def test_diff_marks_added_job_that_previous_version_had_dropped(self):
        self.deliverable(
            "boss_jobs_a1.json", [_job("x")], "2026-10-26T10:00:00+08:00",
            dropped_junk=[{"job_id": "y", "reason": "跨行业噪声"}])
        self.deliverable("boss_jobs_a2.json", [_job("x"), _job("y")],
                         "2026-10-27T10:00:00+08:00")

        payload = self.ask.ask_diff(scope_key="杭州|intern|测试")

        added = {item["job_id"]: item["reason"] for item in payload["data"]["added"]}
        self.assertIn("上一版按规则剔除", added["y"])
        self.assertEqual(payload["data"]["counts"]["added"], 1)

    def test_diff_single_version_reports_previous_missing(self):
        self.deliverable("boss_jobs_only.json", [_job("a")],
                         "2026-10-27T10:00:00+08:00")

        payload = self.ask.ask_diff(scope_key="杭州|intern|测试")

        self.assertTrue(payload["ok"])
        self.assertFalse(payload["data"]["has_previous"])
        self.assertIsNone(payload["data"]["previous"])
        self.assertIn("上一版", payload["human"])

    def test_diff_missing_dir_is_ok_false(self):
        payload = self.ask.ask_diff(result_dir=os.path.join(self.dir, "missing"))
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["errors"])

    def test_diff_ambiguous_scope_picks_newest(self):
        """模糊口径匹配多个时取最新构建的那个（对话里"杭州这一套"不该被歧义挡住）。"""
        self.deliverable("boss_jobs_s1.json", [_job("a")],
                         "2026-10-26T10:00:00+08:00", scope_key="杭州|intern|A")
        self.deliverable("boss_jobs_s2.json", [_job("b")],
                         "2026-10-27T10:00:00+08:00", scope_key="杭州|intern|B")

        payload = self.ask.ask_diff(scope_key="杭州|intern")

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["scope_key"], "杭州|intern|B")


class AskTemplatesTests(AskTestBase):

    def test_templates_list_alias_scope_exclude_and_last_build(self):
        exclude_path = os.path.join(self.dir, "exclude.json")
        write_json(exclude_path, {"name": "杂项", "job_ids": ["e1", "e2"]})
        write_json(os.path.join(self.profiles_dir, "hz.json"), {
            "name": "杭州11词实习-日常",
            "aliases": ["杭州这一套"],
            "city": "杭州", "keywords": ["产品实习生", "AI产品实习生"],
            "recruit_types": ["intern"], "source": "artifacts",
            "scope": "杭州实习 AI 非技术岗", "scope_key": "杭州|intern|测试",
            "deliverable": "boss_jobs_hz.json",
            "exclude_job_ids_file": exclude_path,
            "comment": "杭州这一套的跑法",
        })
        write_json(os.path.join(self.profiles_dir, "sh.json"), {
            "name": "上海AI实习", "city": "上海", "keywords": ["AI产品实习生"]})
        self.deliverable("boss_jobs_hz.json", [_job("a"), _job("b")],
                         "2026-10-27T20:00:00+08:00")
        self.write_run_state(running=False, phase="done", deliverable="boss_jobs_hz.json",
                             message="日常作业完成")

        payload = self.ask.ask_templates()

        self.assertTrue(payload["ok"])
        data = payload["data"]
        self.assertEqual(data["count"], 2)
        hz = next(item for item in data["profiles"] if item["name"].startswith("杭州"))
        self.assertEqual(hz["aliases"], ["杭州这一套"])
        self.assertEqual(hz["scope_key"], "杭州|intern|测试")
        self.assertEqual(hz["last_deliverable"]["jobs"], 2)
        self.assertEqual(hz["exclude_meta"]["count"], 2)
        self.assertIsNotNone(hz["last_run"])
        self.assertIn("杭州11词实习-日常", payload["human"])
        self.assertIn("人工剔除", payload["human"])

    def test_templates_empty_dir_is_a_valid_empty_answer(self):
        payload = self.ask.ask_templates()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["count"], 0)
        self.assertIn("还没有日常模板", payload["human"])


class AskBatchesTests(AskTestBase):

    def test_batches_reads_recent_and_skips_corrupt_lines(self):
        rows = [
            {"run_id": "r1", "started_at": "2026-10-27T10:00:00+08:00",
             "finished_at": "2026-10-27T10:10:00+08:00", "status": "ok",
             "mode": "daily", "keyword": "AI产品实习生", "city": "杭州",
             "list_pages": 3, "detail_ok": 10, "detail_failed": 1,
             "budget_used": 11, "risk_events": 0},
            {"run_id": "r2", "finished_at": "2026-10-27T11:00:00+08:00",
             "status": "failed", "mode": "daily", "detail_ok": 0,
             "detail_failed": 0, "budget_used": 0, "risk_events": 1},
            {"run_id": "r3", "finished_at": "2026-10-27T12:00:00+08:00",
             "status": "ok", "detail_ok": 5, "detail_failed": 0,
             "budget_used": 5, "risk_events": 0},
        ]
        with open(self.batch_health_path, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            handle.write("not-json\n")

        payload = self.ask.ask_batches(limit=2)

        self.assertTrue(payload["ok"])
        data = payload["data"]
        self.assertTrue(data["available"])
        self.assertEqual(data["count"], 3)
        self.assertEqual(data["skipped"], 1)
        self.assertEqual(len(data["items"]), 2)
        self.assertEqual(data["items"][0]["run_id"], "r3")  # 最新在前
        self.assertEqual(data["summary"]["runs"], 3)
        self.assertEqual(data["summary"]["ok_runs"], 2)
        self.assertEqual(data["summary"]["failed_runs"], 1)
        self.assertEqual(data["summary"]["risk_events"], 1)
        self.assertIn("汇总", payload["human"])

    def test_batches_missing_file_degrades(self):
        payload = self.ask.ask_batches()
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["data"]["available"])
        self.assertIn("还没有批次健康度记录", payload["human"])
        self.assertTrue(payload["errors"])


class RunAskTests(AskTestBase):

    def test_run_ask_dispatch_and_json_output(self):
        self.deliverable("boss_jobs_x.json", [_job("a")],
                         "2026-10-27T20:00:00+08:00")
        payload = self.ask.run_ask("templates")
        self.assertEqual(payload["command"], "templates")
        for key in ("ok", "command", "data", "human", "errors"):
            self.assertIn(key, payload)

        payload = self.ask.run_ask("why", {"job_id": "a"})
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["decision"], "kept")

        payload = self.ask.run_ask("diff", {"scope_key": "杭州|intern|测试"})
        self.assertTrue(payload["ok"])

        text = self.ask.format_answer(payload, as_json=True)
        parsed = json.loads(text)  # 纯 JSON，无横幅
        self.assertEqual(parsed["command"], "diff")
        self.assertTrue(parsed["ok"])
        human = self.ask.format_answer(payload)
        self.assertEqual(human, payload["human"])

    def test_run_ask_never_raises_on_bad_input(self):
        payload = self.ask.run_ask("nope")
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["errors"])
        self.assertIn("未知命令", payload["human"])

        payload = self.ask.run_ask("why", {"job_id": "  "})
        self.assertFalse(payload["ok"])

        payload = self.ask.run_ask("diff", {"limit": "not-a-number"},
                                   result_dir=os.path.join(self.dir, "missing"))
        self.assertFalse(payload["ok"])

        payload = self.ask.run_ask("batches", {"limit": "bad"})
        self.assertTrue(payload["ok"])

        payload = self.ask.run_ask(None)
        self.assertFalse(payload["ok"])

    def test_run_ask_accepts_args_result_dir_and_missing_args(self):
        other_dir = os.path.join(self.dir, "other-result")
        os.makedirs(other_dir, exist_ok=True)
        write_json(os.path.join(other_dir, "d.json"), {
            "jobs": [_job("z")], "job_count": 1,
            "artifact": {"kind": "deliverable", "created_at":
                         "2026-10-27T20:00:00+08:00", "scope_key": "杭州|intern|X",
                         "scope": {"city": "杭州", "label": "X"}}})
        payload = self.ask.run_ask("diff", {"scope_key": "杭州|intern|X",
                                            "result_dir": other_dir, "limit": 5})
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["scope_key"], "杭州|intern|X")

        payload = self.ask.run_ask("status", None, result_dir=other_dir)
        self.assertIn("status", payload["command"])
        self.assertIn("human", payload)

    def test_format_answer_handles_non_dict(self):
        text = self.ask.format_answer("not-a-payload", as_json=True)
        parsed = json.loads(text)
        self.assertFalse(parsed["ok"])
        self.assertIn("非 dict", parsed["human"])


if __name__ == "__main__":
    unittest.main()
