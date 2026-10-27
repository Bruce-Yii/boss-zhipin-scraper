"""资产层 schema v3 测试：规范化列抽取 / JD 版本 / 标记 / 迁移回填 / 幂等。

全离线：不依赖真实 Chrome / 网络 / 用户状态目录；库与产物目录都落在临时目录。
覆盖（设计稿 §3 / task-1）：
- 临时库建表（v3 列 + job_versions/job_flags + runs 元数据）；
- 写入时从 payload 抽取规范化列（含薪资/地点/招聘类型/身份指纹）；
- 版本追加（同 JD 不重复、JD 变化追加、只带 job_id+jd 的快照不抖动）；
- 旧库（v1/v2）迁移与回填、（只补差异的）幂等；
- assets_migrate 的 dry-run 不写库、真实迁移、产物目录回填、CLI JSON/环境变量。
"""

import contextlib
import io
import json
import os
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]

JD = "负责AI产品需求分析、原型设计与跨团队协作。" * 12
JD_V2 = JD + "新增：数据分析与指标体系。"
FIXED_NOW = "2026-10-28T00:00:00+00:00"
OLDER_NOW = "2026-10-27T10:00:00+00:00"
STALE_NOW = "2026-09-01T00:00:00+00:00"


def load_db_store():
    """以 scripts 命名空间包导入 db_store（与安装后的 scripts.db_store 一致）。"""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from scripts import db_store
    return db_store


def load_assets_migrate():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from scripts import assets_migrate
    return assets_migrate


def make_job(job_id="j1", **overrides):
    """一条列表侧 payload（字段名与真实导出对齐）。"""
    job = {
        "job_id": job_id,
        "title": "AI产品实习生",
        "company_name": "某智能科技",
        "location": "杭州·余杭区·仓前",
        "salary": "20-30K·15薪",
        "experience": "1-3年",
        "education": "本科",
        "job_type": "4",
        "company_industry": "人工智能",
        "company_scale": "100-499人",
        "job_link": f"https://www.zhipin.com/job_detail/{job_id}.html",
        "skills": ["产品", "AI"],
        "link_verified_at": "2026-10-27T10:00:00+08:00",
    }
    job.update(overrides)
    return job


def make_detail(job_id="j1", jd=JD, **overrides):
    """一条详情侧 payload（含 JD/技能/风险标记/方向判定）。"""
    detail = dict(
        make_job(job_id), jd=jd, skill_tags=["产品"],
        risk_flags=["no_district"], direction="ai_pm", is_ai_pm=True)
    detail.update(overrides)
    return detail


_V1_SCHEMA = """
CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE runs (run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
    mode TEXT, keyword TEXT, city TEXT, job_count INTEGER NOT NULL DEFAULT 0,
    detail_count INTEGER NOT NULL DEFAULT 0);
CREATE TABLE jobs (job_id TEXT PRIMARY KEY, payload TEXT NOT NULL,
    first_seen_at TEXT NOT NULL, updated_at TEXT NOT NULL, run_id TEXT);
CREATE TABLE details (job_id TEXT PRIMARY KEY, jd TEXT, payload TEXT NOT NULL,
    first_seen_at TEXT NOT NULL, updated_at TEXT NOT NULL, run_id TEXT);
INSERT INTO schema_meta VALUES ('schema_version', '1');
"""

_V2_SCHEMA = """
CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE runs (run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
    mode TEXT, keyword TEXT, city TEXT, job_count INTEGER NOT NULL DEFAULT 0,
    detail_count INTEGER NOT NULL DEFAULT 0);
CREATE TABLE jobs (job_id TEXT PRIMARY KEY, payload TEXT NOT NULL,
    first_seen_at TEXT NOT NULL, updated_at TEXT NOT NULL, run_id TEXT,
    keyword TEXT, city TEXT);
CREATE TABLE details (job_id TEXT PRIMARY KEY, jd TEXT, payload TEXT NOT NULL,
    first_seen_at TEXT NOT NULL, updated_at TEXT NOT NULL, run_id TEXT);
INSERT INTO schema_meta VALUES ('schema_version', '2');
"""


def make_v2_db(path, *, job=None, detail=None, updated_at=OLDER_NOW,
               run_id="r1", job_city="杭州"):
    """造一个 v2 老库（jobs 只有 payload/时间戳/口径列）。"""
    job = job if job is not None else make_job()
    detail = detail if detail is not None else make_detail()
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_V2_SCHEMA)
        conn.execute(
            "INSERT INTO jobs (job_id, payload, first_seen_at, updated_at, run_id,"
            " keyword, city) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (job["job_id"], json.dumps(job, ensure_ascii=False), updated_at,
             updated_at, run_id, "AI产品实习生", job_city))
        if detail is not None:
            conn.execute(
                "INSERT INTO details (job_id, jd, payload, first_seen_at, updated_at,"
                " run_id) VALUES (?, ?, ?, ?, ?, ?)",
                (detail["job_id"], detail.get("jd"),
                 json.dumps(detail, ensure_ascii=False), updated_at, updated_at,
                 run_id))
        conn.execute(
            "INSERT INTO runs (run_id, started_at, finished_at, mode, keyword, city,"
            " job_count, detail_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, updated_at, updated_at, "single", "AI产品实习生", job_city, 1, 1))
        conn.commit()
    finally:
        conn.close()
    return job


def write_json(directory, name, payload):
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    return path


class AssetSchemaTests(unittest.TestCase):
    """临时库建表 + 写入抽取。"""

    def setUp(self):
        self.db = load_db_store()
        self.tmp = tempfile.TemporaryDirectory(prefix="assets-schema-")
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "boss.db")
        self.conn = self.db.open_store(self.path)

    def tearDown(self):
        self.conn.close()

    def test_fresh_store_is_v3_with_asset_columns_and_tables(self):
        info = self.db.stats(self.conn)
        self.assertEqual(self.db.SCHEMA_VERSION, 3)
        self.assertEqual(info["schema_version"], 3)
        tables = {row[0] for row in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"schema_meta", "runs", "jobs", "details",
                         "job_versions", "job_flags"} <= tables)
        job_columns = {row[1] for row in self.conn.execute("PRAGMA table_info(jobs)")}
        self.assertTrue({
            "title", "company", "city", "district", "salary_min", "salary_max",
            "salary_desc", "experience", "education", "recruit_type", "industry",
            "company_size", "url", "first_seen_at", "last_seen_at", "state",
            "content_hash", "canonical_id",
        } <= job_columns)
        run_columns = {row[1] for row in self.conn.execute("PRAGMA table_info(runs)")}
        self.assertTrue({
            "scope_key", "template", "ruleset_version", "list_pages", "detail_ok",
            "detail_failed", "budget_used", "artifact_paths",
        } <= run_columns)
        # 重复打开：幂等，不重复迁移
        again = self.db.open_store(self.path)
        try:
            self.assertEqual(self.db.stats(again)["schema_version"], 3)
        finally:
            again.close()

    def test_upsert_jobs_extracts_normalized_columns(self):
        inserted, updated = self.db.upsert_jobs(
            self.conn, [make_job()], run_id="r1", now=FIXED_NOW)
        self.assertEqual((inserted, updated), (1, 0))
        row = self.conn.execute("SELECT * FROM jobs WHERE job_id='j1'").fetchone()
        self.assertEqual(row["title"], "AI产品实习生")
        self.assertEqual(row["company"], "某智能科技")
        self.assertEqual(row["city"], "杭州")
        self.assertEqual(row["district"], "余杭区")
        self.assertEqual((row["salary_min"], row["salary_max"]), (20000, 30000))
        self.assertEqual(row["salary_desc"], "20-30K·15薪")
        self.assertEqual(row["experience"], "1-3年")
        self.assertEqual(row["education"], "本科")
        self.assertEqual(row["recruit_type"], "intern")
        self.assertEqual(row["industry"], "人工智能")
        self.assertEqual(row["company_size"], "100-499人")
        self.assertEqual(row["url"],
                         "https://www.zhipin.com/job_detail/j1.html")
        self.assertEqual(row["first_seen_at"], FIXED_NOW)
        self.assertEqual(row["last_seen_at"], FIXED_NOW)
        self.assertEqual(row["state"], "new")
        self.assertTrue(str(row["canonical_id"]).startswith("cn-"))
        self.assertEqual(len(str(row["content_hash"])), 32)

    def test_day_salary_and_negotiable_salary(self):
        self.db.upsert_jobs(self.conn, [make_job("day", salary="150-200元/天")],
                            now=FIXED_NOW)
        row = self.conn.execute(
            "SELECT salary_min, salary_max FROM jobs WHERE job_id='day'").fetchone()
        self.assertEqual((row["salary_min"], row["salary_max"]), (150, 200))
        self.db.upsert_jobs(self.conn, [make_job("talk", salary="面议")],
                            now=FIXED_NOW)
        row = self.conn.execute(
            "SELECT salary_min, salary_max, salary_desc FROM jobs "
            "WHERE job_id='talk'").fetchone()
        self.assertIsNone(row["salary_min"])
        self.assertIsNone(row["salary_max"])
        self.assertEqual(row["salary_desc"], "面议")

    def test_city_scope_wins_and_payload_city_fallback(self):
        # 显式口径城市优先（保持 v2 cross_run 语义）
        self.db.upsert_jobs(self.conn, [make_job("scoped", location="")],
                            city="上海", now=FIXED_NOW)
        row = self.conn.execute(
            "SELECT city, district FROM jobs WHERE job_id='scoped'").fetchone()
        self.assertEqual(row["city"], "上海")
        self.assertIsNone(row["district"])
        # 再次入库不带口径 → COALESCE 保住已有值
        self.db.upsert_jobs(self.conn, [make_job("scoped", location="")],
                            now=FIXED_NOW)
        row = self.conn.execute(
            "SELECT city FROM jobs WHERE job_id='scoped'").fetchone()
        self.assertEqual(row["city"], "上海")
        # 没给口径时用 payload 的城市
        self.db.upsert_jobs(self.conn, [make_job("bj", location="北京·海淀区")],
                            now=FIXED_NOW)
        row = self.conn.execute(
            "SELECT city, district FROM jobs WHERE job_id='bj'").fetchone()
        self.assertEqual((row["city"], row["district"]), ("北京", "海淀区"))

    def test_state_new_active_then_stale_then_active(self):
        self.db.upsert_jobs(self.conn, [make_job()], now=OLDER_NOW)
        state = self.conn.execute(
            "SELECT state FROM jobs WHERE job_id='j1'").fetchone()[0]
        self.assertEqual(state, "new")
        self.db.upsert_jobs(self.conn, [make_job()], now=FIXED_NOW)
        state = self.conn.execute(
            "SELECT state FROM jobs WHERE job_id='j1'").fetchone()[0]
        self.assertEqual(state, "active")
        # 手动把最后出现时间拨旧 → refresh 标 stale
        self.conn.execute(
            "UPDATE jobs SET last_seen_at=? WHERE job_id='j1'", (STALE_NOW,))
        self.conn.commit()
        updated = self.db.refresh_job_states(
            self.conn, now=FIXED_NOW, stale_days=14)
        self.assertEqual(updated, 1)
        self.assertEqual(self.conn.execute(
            "SELECT state FROM jobs WHERE job_id='j1'").fetchone()[0], "stale")
        # 重新出现 → active
        self.db.upsert_jobs(self.conn, [make_job()], now=FIXED_NOW)
        self.assertEqual(self.conn.execute(
            "SELECT state FROM jobs WHERE job_id='j1'").fetchone()[0], "active")

    def test_canonical_id_groups_identity_and_content_hash_tracks_jd(self):
        base = self.db.extract_job_columns(make_job("a"))
        other_id = self.db.extract_job_columns(make_job("b"))
        different = self.db.extract_job_columns(make_job("c", title="运营专员"))
        self.assertEqual(base["canonical_id"], other_id["canonical_id"])
        self.assertNotEqual(base["canonical_id"], different["canonical_id"])
        with_jd = self.db.extract_job_columns(make_job("a", jd=JD))
        with_jd_again = self.db.extract_job_columns(make_job("a", jd=JD))
        self.assertEqual(with_jd["canonical_id"], base["canonical_id"])
        self.assertNotEqual(with_jd["content_hash"], base["content_hash"])
        self.assertEqual(with_jd["content_hash"], with_jd_again["content_hash"])


class VersionAndFlagTests(unittest.TestCase):
    """JD 版本追加 + 派生标记。"""

    def setUp(self):
        self.db = load_db_store()
        self.tmp = tempfile.TemporaryDirectory(prefix="assets-version-")
        self.addCleanup(self.tmp.cleanup)
        self.conn = self.db.open_store(os.path.join(self.tmp.name, "boss.db"))

    def tearDown(self):
        self.conn.close()

    def test_details_append_version_only_when_different(self):
        self.db.upsert_details(self.conn, [make_detail()], run_id="r1",
                               now=FIXED_NOW)
        versions = self.db.load_job_versions(self.conn, "j1")
        self.assertEqual(len(versions), 1)
        first = versions[0]
        self.assertEqual(first["version"], 1)
        self.assertEqual(len(first["jd_hash"]), 32)
        self.assertEqual(first["captured_at"], FIXED_NOW)
        self.assertEqual(first["run_id"], "r1")
        self.assertEqual(first["source"], "detail")
        self.assertEqual(json.loads(first["skill_tags"]), ["产品"])
        # 同一份详情再入库：不重复追加
        self.db.upsert_details(self.conn, [make_detail()], run_id="r1",
                               now=FIXED_NOW)
        self.assertEqual(len(self.db.load_job_versions(self.conn, "j1")), 1)
        # JD 变化 → 追加 version 2
        self.db.upsert_details(self.conn, [make_detail(jd=JD_V2)], run_id="r2",
                               now=FIXED_NOW)
        versions = self.db.load_job_versions(self.conn, "j1")
        self.assertEqual([item["version"] for item in versions], [1, 2])
        self.assertIn("数据分析", versions[1]["jd"])
        self.assertNotEqual(versions[0]["jd_hash"], versions[1]["jd_hash"])

    def test_jd_only_snapshot_does_not_churn_versions(self):
        """只有 job_id+jd 的快照（产物详情文件）不该反复追空字段版本。"""
        self.db.upsert_details(self.conn, [make_detail()], run_id="r1",
                               now=FIXED_NOW)
        snapshot = {"job_id": "j1", "jd": JD}
        for _ in range(2):
            appended = self.db.append_version_from_payload(
                self.conn, snapshot, captured_at=FIXED_NOW, source="artifact:details")
            self.assertIsNone(appended)
        versions = self.db.load_job_versions(self.conn, "j1")
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0]["title"], "AI产品实习生")

    def test_replaying_old_jd_does_not_append_duplicate_version(self):
        """产物目录按时间重放历史 JD：同一 JD 指纹只留一个版本（重跑幂等）。"""
        first = self.db.append_job_version(
            self.conn, "j1", jd=JD, title="AI产品实习生", salary_desc="20-30K",
            captured_at=OLDER_NOW, source="artifact")
        second = self.db.append_job_version(
            self.conn, "j1", jd=JD_V2, title="AI产品实习生", salary_desc="25-35K",
            captured_at=FIXED_NOW, source="artifact")
        self.assertEqual((first, second), (1, 2))
        # 再按时间顺序重放一遍：版本数不变
        self.assertIsNone(self.db.append_job_version(
            self.conn, "j1", jd=JD, captured_at=OLDER_NOW, source="artifact"))
        self.assertIsNone(self.db.append_job_version(
            self.conn, "j1", jd=JD_V2, captured_at=FIXED_NOW, source="artifact"))
        versions = self.db.load_job_versions(self.conn, "j1")
        self.assertEqual([item["version"] for item in versions], [1, 2])

    def test_same_jd_with_title_fills_metadata_without_new_version(self):
        """同一 JD 先只有正文、后带上标题：补元数据，不新增版本。"""
        self.assertEqual(self.db.append_job_version(self.conn, "j1", jd=JD), 1)
        self.assertIsNone(self.db.append_job_version(
            self.conn, "j1", jd=JD, title="AI产品实习生",
            salary_desc="20-30K·15薪"))
        self.assertEqual(len(self.db.load_job_versions(self.conn, "j1")), 1)
        version = self.db.load_job_versions(self.conn, "j1")[0]
        self.assertEqual(version["title"], "AI产品实习生")
        self.assertEqual(version["salary_desc"], "20-30K·15薪")

    def test_upsert_jobs_with_jd_appends_version(self):
        self.db.upsert_jobs(self.conn, [make_job("list-jd", jd=JD)], run_id="r1",
                            now=FIXED_NOW)
        versions = self.db.load_job_versions(self.conn, "list-jd")
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0]["source"], "list")
        self.db.upsert_jobs(self.conn, [make_job("list-jd", jd=JD)], run_id="r1",
                            now=FIXED_NOW)
        self.assertEqual(len(self.db.load_job_versions(self.conn, "list-jd")), 1)

    def test_flags_from_payload_are_idempotent(self):
        detail = make_detail(risk_flags=["stale", "part_time"], post_type="agency")
        self.db.upsert_details(self.conn, [detail], run_id="r1", now=FIXED_NOW)
        flags = {item["flag"]: item for item in
                 self.db.load_job_flags(self.conn, "j1")}
        self.assertTrue({"stale", "part_time", "ai_pm", "post:agency",
                         "recruit:intern"} <= set(flags))
        self.assertEqual(flags["stale"]["rule_version"],
                         self.db.JOB_FLAG_RULE_VERSION)
        self.assertEqual(flags["stale"]["run_id"], "r1")
        before = self.db.asset_stats(self.conn)["job_flags"]
        self.db.upsert_details(self.conn, [detail], run_id="r1", now=FIXED_NOW)
        self.assertEqual(self.db.asset_stats(self.conn)["job_flags"], before)
        # 单个标记写入：变化才 True
        self.assertTrue(self.db.record_job_flag(self.conn, "j1", "manual"))
        self.assertFalse(self.db.record_job_flag(self.conn, "j1", "manual"))
        self.assertIn("manual", {item["flag"] for item in
                                 self.db.load_job_flags(self.conn, "j1")})

    def test_company_name_heuristic_does_not_create_post_flag(self):
        """公司名"某…"只是匿名启发式：不当平台标注写 post 标记（避免与详情侧冲突）。"""
        flags = self.db.extract_job_flags(make_job(company_name="某大型互联网公司"))
        self.assertFalse([name for name, _value in flags if name.startswith("post:")])
        flagged = self.db.extract_job_flags(make_job(proxy_job="1"))
        self.assertIn("post:agency", [name for name, _value in flagged])


class RunMetadataTests(unittest.TestCase):
    """runs 批次健康度 / 口径血缘列。"""

    def setUp(self):
        self.db = load_db_store()
        self.tmp = tempfile.TemporaryDirectory(prefix="assets-runs-")
        self.addCleanup(self.tmp.cleanup)
        self.conn = self.db.open_store(os.path.join(self.tmp.name, "boss.db"))

    def tearDown(self):
        self.conn.close()

    def test_record_run_persists_asset_metadata(self):
        self.db.record_run(
            self.conn, "r1", mode="single", keyword="AI产品实习生", city="杭州",
            started_at=OLDER_NOW, job_count=3, detail_count=2,
            scope_key="杭州|intern|全量干净", template="杭州11词实习-日常",
            ruleset_version="2026-10-28", list_pages=3, detail_ok=2,
            detail_failed=1, budget_used=2, artifact_paths=["a.json"],
            now=FIXED_NOW)
        row = self.conn.execute(
            "SELECT * FROM runs WHERE run_id='r1'").fetchone()
        self.assertEqual(row["scope_key"], "杭州|intern|全量干净")
        self.assertEqual(row["template"], "杭州11词实习-日常")
        self.assertEqual(row["ruleset_version"], "2026-10-28")
        self.assertEqual((row["list_pages"], row["detail_ok"],
                          row["detail_failed"], row["budget_used"]),
                         (3, 2, 1, 2))
        self.assertEqual(json.loads(row["artifact_paths"]), ["a.json"])
        self.assertIsNotNone(row["finished_at"])

    def test_partial_update_keeps_metadata_and_finalizes(self):
        self.db.record_run(self.conn, "r1", scope_key="杭州|intern|全量",
                           template="tpl", finished_at=None, now=OLDER_NOW)
        self.db.record_run(self.conn, "r1", job_count=9, detail_count=4,
                           now=FIXED_NOW)
        row = self.conn.execute(
            "SELECT * FROM runs WHERE run_id='r1'").fetchone()
        self.assertEqual(row["scope_key"], "杭州|intern|全量")
        self.assertEqual(row["template"], "tpl")
        self.assertIsNotNone(row["finished_at"])
        self.assertEqual((row["job_count"], row["detail_count"]), (9, 4))

    def test_recent_runs_returns_dicts(self):
        self.db.record_run(self.conn, "r1", mode="single", now=OLDER_NOW)
        self.db.record_run(self.conn, "r2", mode="single", now=FIXED_NOW)
        runs = self.db.recent_runs(self.conn, limit=1)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["run_id"], "r2")


class AssetQueryTests(unittest.TestCase):
    """规范化列查询 / 资产视图 / 统计。"""

    def setUp(self):
        self.db = load_db_store()
        self.tmp = tempfile.TemporaryDirectory(prefix="assets-query-")
        self.addCleanup(self.tmp.cleanup)
        self.conn = self.db.open_store(os.path.join(self.tmp.name, "boss.db"))
        self.db.upsert_jobs(self.conn, [
            make_job("hz-intern"),
            make_job("hz-social", title="AI产品经理", job_type=""),
            make_job("sh-intern", location="上海·浦东新区"),
        ], run_id="r1", now=OLDER_NOW)
        self.conn.execute(
            "UPDATE jobs SET city='上海' WHERE job_id='sh-intern'")
        self.conn.execute(
            "UPDATE jobs SET last_seen_at=? WHERE job_id='hz-social'", (STALE_NOW,))
        self.conn.commit()
        self.db.refresh_job_states(self.conn, now=FIXED_NOW, stale_days=14)

    def tearDown(self):
        self.conn.close()

    def test_query_jobs_filters(self):
        ids = {row["job_id"] for row in self.db.query_jobs(self.conn, city="杭州")}
        self.assertEqual(ids, {"hz-intern", "hz-social"})
        interns = {row["job_id"] for row in
                   self.db.query_jobs(self.conn, recruit_type="intern")}
        self.assertEqual(interns, {"hz-intern", "sh-intern"})
        stale = {row["job_id"] for row in
                 self.db.query_jobs(self.conn, state="stale")}
        self.assertEqual(stale, {"hz-social"})
        self.assertEqual(len(self.db.query_jobs(self.conn, limit=1)), 1)

    def test_load_job_asset_and_missing(self):
        self.db.upsert_details(self.conn, [make_detail("hz-intern")],
                               run_id="r1", now=FIXED_NOW)
        asset = self.db.load_job_asset(self.conn, "hz-intern")
        self.assertEqual(asset["title"], "AI产品实习生")
        self.assertEqual(asset["payload"]["job_id"], "hz-intern")
        self.assertEqual(len(asset["versions"]), 1)
        self.assertTrue(asset["flags"])
        self.assertIsNone(self.db.load_job_asset(self.conn, "missing"))

    def test_asset_stats_and_stats_shape(self):
        info = self.db.stats(self.conn)
        self.assertEqual(info["jobs"], 3)
        self.assertIn("job_versions", info)
        self.assertIn("job_flags", info)
        self.assertIn("states", info)
        asset = self.db.asset_stats(self.conn)
        self.assertEqual(asset["jobs_with_canonical"], 3)
        self.assertGreaterEqual(asset["canonical_groups"], 1)
        # 身份 = title+company+city：两个标题 + 两个城市 → 三组
        self.assertEqual(asset["canonical_groups"], 3)


class LegacyDatabaseTests(unittest.TestCase):
    """v2 老库迁移 / 回填 / 只读降级。"""

    def setUp(self):
        self.db = load_db_store()
        self.tmp = tempfile.TemporaryDirectory(prefix="assets-legacy-")
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "boss.db")

    def test_v2_migrates_to_v3_and_keeps_rows(self):
        make_v2_db(self.path)
        conn = self.db.open_store(self.path)
        try:
            self.assertEqual(self.db.stored_schema_version(conn), 3)
            job_columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
            self.assertIn("canonical_id", job_columns)
            self.assertIn("keyword", job_columns)  # v1→v2 的列也补齐
            row = conn.execute(
                "SELECT job_id, first_seen_at, city, title FROM jobs").fetchone()
            self.assertEqual(row["job_id"], "j1")
            self.assertEqual(row["first_seen_at"], OLDER_NOW)
            self.assertEqual(row["city"], "杭州")
            self.assertIsNone(row["title"])  # 迁移只加列，回填是下一步
            tables = {item[0] for item in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue({"job_versions", "job_flags"} <= tables)
        finally:
            conn.close()
        # 重复打开幂等
        conn = self.db.open_store(self.path)
        try:
            self.assertEqual(self.db.stored_schema_version(conn), 3)
        finally:
            conn.close()

    def test_v1_migrates_to_v3(self):
        conn = sqlite3.connect(self.path)
        try:
            conn.executescript(_V1_SCHEMA)
            conn.execute(
                "INSERT INTO jobs (job_id, payload, first_seen_at, updated_at)"
                " VALUES ('old-row', '{}', ?, ?)", (OLDER_NOW, OLDER_NOW))
            conn.commit()
        finally:
            conn.close()
        conn = self.db.open_store(self.path)
        try:
            self.assertEqual(self.db.stored_schema_version(conn), 3)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
            self.assertTrue({"keyword", "city", "canonical_id", "state"} <= columns)
        finally:
            conn.close()

    def test_backfill_jobs_fills_and_is_idempotent(self):
        make_v2_db(self.path)
        conn = self.db.open_store(self.path)
        try:
            first = self.db.backfill_jobs(conn)
            self.assertEqual(first["scanned"], 1)
            self.assertEqual(first["backfilled"], 1)
            self.assertEqual(first["failed"], 0)
            self.assertEqual(first["columns_filled"]["title"], 1)
            self.assertEqual(first["columns_filled"]["salary_min"], 1)
            row = conn.execute("SELECT * FROM jobs WHERE job_id='j1'").fetchone()
            self.assertEqual(row["title"], "AI产品实习生")
            self.assertEqual(row["district"], "余杭区")
            self.assertEqual((row["salary_min"], row["salary_max"]), (20000, 30000))
            self.assertEqual(row["state"], "active")
            self.assertEqual(row["last_seen_at"], OLDER_NOW)
            second = self.db.backfill_jobs(conn)
            self.assertEqual(second["backfilled"], 0)
            self.assertEqual(second["skipped"], 1)
        finally:
            conn.close()

    def test_backfill_marks_old_rows_stale(self):
        make_v2_db(self.path, updated_at=STALE_NOW)
        conn = self.db.open_store(self.path)
        try:
            self.db.backfill_jobs(conn, now=FIXED_NOW, stale_days=14)
            state = conn.execute(
                "SELECT state FROM jobs WHERE job_id='j1'").fetchone()[0]
            self.assertEqual(state, "stale")
        finally:
            conn.close()

    def test_backfill_versions_flags_runs_idempotent(self):
        make_v2_db(self.path)
        conn = self.db.open_store(self.path)
        try:
            versions_first = self.db.backfill_job_versions(conn)
            self.assertEqual(versions_first["scanned"], 1)
            self.assertEqual(versions_first["backfilled"], 1)
            self.assertEqual(
                self.db.backfill_job_versions(conn)["backfilled"], 0)
            self.assertEqual(len(self.db.load_job_versions(conn, "j1")), 1)
            flags_first = self.db.backfill_job_flags(conn)
            self.assertGreaterEqual(flags_first["backfilled"], 1)
            self.assertEqual(self.db.backfill_job_flags(conn)["backfilled"], 0)
            self.assertTrue(self.db.load_job_flags(conn, "j1"))
            runs = self.db.backfill_runs(conn)
            self.assertEqual(runs["scanned"], 1)
            self.assertEqual(runs["backfilled"], 0)
        finally:
            conn.close()

    def test_corrupt_payload_counts_as_failed(self):
        make_v2_db(self.path)
        conn = sqlite3.connect(self.path)
        conn.execute(
            "INSERT INTO jobs (job_id, payload, first_seen_at, updated_at)"
            " VALUES ('bad', '{not json', ?, ?)", (OLDER_NOW, OLDER_NOW))
        conn.commit()
        conn.close()
        conn = self.db.open_store(self.path)
        try:
            stats = self.db.backfill_jobs(conn)
            self.assertEqual(stats["failed"], 1)
        finally:
            conn.close()

    def test_readonly_legacy_database_degrades_gracefully(self):
        make_v2_db(self.path)
        conn = self.db.open_store_readonly(self.path)
        try:
            self.assertFalse(self.db.asset_schema_ready(conn))
            with self.assertRaises(self.db.DbSchemaError):
                self.db.query_jobs(conn)
            with self.assertRaises(self.db.DbSchemaError):
                self.db.load_job_asset(conn, "j1")
            self.assertEqual(self.db.load_job_versions(conn, "j1"), [])
            self.assertEqual(self.db.load_job_flags(conn), [])
            info = self.db.stats(conn)
            self.assertEqual(info["schema_version"], 2)
            self.assertEqual(info["jobs"], 1)
            self.assertEqual(info["job_versions"], 0)
            # 旧读取路径不受影响
            self.assertEqual(self.db.load_detail_ids(conn), {"j1"})
            self.assertEqual(
                self.db.cross_run_delta(conn, "杭州", ["j1"])["added"], [])
        finally:
            conn.close()
        self.assertEqual(self.db.schema_version_at(self.path), 2)


class AssetsMigrateTests(unittest.TestCase):
    """assets_migrate：dry-run 不写库 / 真实迁移 / 产物回填 / CLI。"""

    def setUp(self):
        self.db_store = load_db_store()
        self.assets = load_assets_migrate()
        self.tmp = tempfile.TemporaryDirectory(prefix="assets-migrate-")
        self.addCleanup(self.tmp.cleanup)
        self.db_path = os.path.join(self.tmp.name, "boss.db")
        self.result_dir = os.path.join(self.tmp.name, "job-result")
        os.makedirs(self.result_dir)
        make_v2_db(self.db_path)
        write_json(self.result_dir, "boss_jobs_20261027_120000_1.json", {
            "keyword": "AI产品实习生", "city": "杭州", "format_version": 2,
            "jobs": [make_job("j1"), make_job("ghost")],
        })
        write_json(self.result_dir, "boss_details_20261027_120000_1.json",
                   [{"job_id": "j1", "jd": JD}])

    def _columns(self, table="jobs"):
        conn = sqlite3.connect(self.db_path)
        try:
            return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        finally:
            conn.close()

    def test_dry_run_reports_without_writing(self):
        report = self.assets.run_migration(self.db_path, self.result_dir,
                                          dry_run=True)
        self.assertTrue(report["ok"], report["errors"])
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["schema"]["before"], 2)
        self.assertEqual(report["schema"]["after"], 2)
        self.assertFalse(report["schema"]["migrated"])
        self.assertTrue(report["schema"]["needs_migration"])
        jobs_backfill = report["db"]["jobs_backfill"]
        self.assertEqual(jobs_backfill["scanned"], 1)
        self.assertEqual(jobs_backfill["backfilled"], 1)
        self.assertEqual(jobs_backfill["columns_filled"]["title"], 1)
        # dry-run 用影子版本表：详情同一份 JD 的快照不会被重复计成追加
        self.assertEqual(report["db"]["versions_backfill"]["backfilled"], 1)
        art = report["artifacts"]
        self.assertFalse(art["error"])
        self.assertEqual(art["files_scanned"], 2)
        self.assertEqual(art["records_scanned"], 3)
        self.assertEqual(art["jobs_matched"], 1)
        self.assertEqual(art["not_in_db"], 1)
        # DB 阶段已模拟补齐所有可抽列 → 产物阶段不应重复计成"回填"
        self.assertEqual(art["jobs_backfilled"], 0)
        self.assertEqual(art["flags_backfilled"], 0)
        self.assertEqual(art["versions_backfilled"], 0)
        # 没写库：版本仍是 v2、没有 v3 列
        self.assertEqual(self.db_store.schema_version_at(self.db_path), 2)
        self.assertNotIn("canonical_id", self._columns())
        self.assertNotIn("job_versions", self._columns("runs"))

    def test_real_run_migrates_backfills_and_is_idempotent(self):
        first = self.assets.run_migration(self.db_path, self.result_dir)
        self.assertTrue(first["ok"], first["errors"])
        self.assertEqual(first["schema"]["before"], 2)
        self.assertEqual(first["schema"]["after"], 3)
        self.assertTrue(first["schema"]["migrated"])
        self.assertEqual(first["db"]["jobs_backfill"]["backfilled"], 1)
        self.assertEqual(first["db"]["versions_backfill"]["backfilled"], 1)
        self.assertGreaterEqual(first["db"]["flags_backfill"]["backfilled"], 1)
        self.assertEqual(first["artifacts"]["files_scanned"], 2)
        self.assertEqual(first["artifacts"]["jobs_matched"], 1)
        self.assertEqual(first["artifacts"]["not_in_db"], 1)
        # 回填后的真实值
        conn = self.db_store.open_store_readonly(self.db_path)
        try:
            row = conn.execute("SELECT * FROM jobs WHERE job_id='j1'").fetchone()
            self.assertEqual(row["title"], "AI产品实习生")
            self.assertEqual(row["salary_min"], 20000)
            self.assertEqual(row["state"], "active")
            self.assertTrue(row["canonical_id"])
            self.assertEqual(len(self.db_store.load_job_versions(conn, "j1")), 1)
            self.assertTrue(self.db_store.load_job_flags(conn, "j1"))
        finally:
            conn.close()
        # 幂等：第二次全部 0 变化
        second = self.assets.run_migration(self.db_path, self.result_dir)
        self.assertTrue(second["ok"], second["errors"])
        self.assertEqual(second["db"]["jobs_backfill"]["backfilled"], 0)
        self.assertEqual(second["db"]["versions_backfill"]["backfilled"], 0)
        self.assertEqual(second["db"]["flags_backfill"]["backfilled"], 0)
        self.assertEqual(second["artifacts"]["jobs_backfilled"], 0)
        self.assertEqual(second["artifacts"]["versions_backfilled"], 0)
        self.assertEqual(second["artifacts"]["flags_backfilled"], 0)
        self.assertEqual(second["artifacts"]["not_in_db"], 1)

    def test_main_json_uses_env_overrides(self):
        with mock.patch.dict(os.environ, {
                "BOSS_DB_PATH": self.db_path,
                "BOSS_RESULT_DIR": self.result_dir}):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = self.assets.main(["--dry-run", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["db_path"], os.path.abspath(self.db_path))
        self.assertEqual(payload["result_dir"], os.path.abspath(self.result_dir))
        self.assertTrue(payload["dry_run"])
        self.assertIn("elapsed_ms", payload)

    def test_main_missing_db_returns_1(self):
        missing = os.path.join(self.tmp.name, "nope.db")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = self.assets.main(["--db", missing, "--json"])
        self.assertEqual(code, 1)
        payload = json.loads(out.getvalue())
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["errors"])
        self.assertFalse(os.path.exists(missing))

    def test_main_human_output_and_no_artifacts(self):
        second_db = os.path.join(self.tmp.name, "second.db")
        make_v2_db(second_db)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = self.assets.main(["--db", second_db, "--no-artifacts"])
        text = out.getvalue()
        self.assertEqual(code, 0, text)
        self.assertIn("jobs 规范化列", text)
        self.assertIn("产物目录: 已跳过", text)
        self.assertIn("schema: v2 → v3", text)
        self.assertIn("完成", text)

    def test_merge_job_columns_only_fills_nulls(self):
        conn = self.db_store.open_store(self.db_path)
        try:
            conn.execute("UPDATE jobs SET title='已有标题' WHERE job_id='j1'")
            conn.commit()
            merged = self.db_store.merge_job_columns(
                conn, make_job("j1", title="文件里的标题"))
            self.assertTrue(merged["matched"])
            self.assertGreater(merged["updated"], 0)
            row = conn.execute(
                "SELECT title, company FROM jobs WHERE job_id='j1'").fetchone()
            self.assertEqual(row["title"], "已有标题")
            self.assertEqual(row["company"], "某智能科技")
            ghost = self.db_store.merge_job_columns(conn, make_job("ghost"))
            self.assertFalse(ghost["matched"])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
