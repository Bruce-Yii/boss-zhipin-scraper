"""P4c-2 SQLite 增量层测试：WAL / upsert 幂等 / 时间戳语义 / 续抓读取 / 合规脱敏。

全离线：不依赖真实 Chrome/网络；库文件落在临时目录，不污染用户状态目录。
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

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_db_store():
    """以 scripts 命名空间包导入 db_store（与安装后的 scripts.db_store 一致）。"""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from scripts import db_store
    return db_store


class DbStoreTests(unittest.TestCase):
    def setUp(self):
        self.db = load_db_store()
        self.tmp = tempfile.TemporaryDirectory(prefix="dbstore-")
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "boss.db")
        self.conn = self.db.open_store(self.path)

    def tearDown(self):
        self.conn.close()

    def test_open_store_uses_wal_and_creates_tables(self):
        mode = self.conn.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(str(mode).lower(), "wal")
        names = {row[0] for row in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"schema_meta", "runs", "jobs", "details"} <= names)

    def test_upsert_jobs_idempotent_and_counts(self):
        jobs = [{"job_id": "a", "title": "T1"}, {"job_id": "b", "title": "T2"}]
        self.assertEqual(self.db.upsert_jobs(self.conn, jobs, run_id="r1"), (2, 0))
        self.assertEqual(self.db.upsert_jobs(self.conn, jobs, run_id="r2"), (0, 2))
        self.assertEqual(self.db.stats(self.conn)["jobs"], 2)

    def test_first_seen_at_preserved_updated_at_advances(self):
        self.db.upsert_jobs(self.conn, [{"job_id": "a", "title": "v1"}],
                            now="2026-01-01T00:00:00+00:00")
        self.db.upsert_jobs(self.conn, [{"job_id": "a", "title": "v2"}],
                            now="2026-02-02T00:00:00+00:00")
        row = self.conn.execute(
            "SELECT first_seen_at, updated_at, payload FROM jobs WHERE job_id='a'"
        ).fetchone()
        self.assertEqual(row[0], "2026-01-01T00:00:00+00:00")
        self.assertEqual(row[1], "2026-02-02T00:00:00+00:00")
        self.assertEqual(json.loads(row[2])["title"], "v2")

    def test_upsert_skips_records_without_job_id(self):
        self.assertEqual(
            self.db.upsert_jobs(self.conn, [{"title": "x"}, "junk", None, 5]), (0, 0))

    def test_detail_ids_require_nonempty_jd(self):
        self.db.upsert_details(self.conn, [
            {"job_id": "a", "jd": "hello"},
            {"job_id": "b", "jd": "   "},
            {"job_id": "c"},
        ])
        self.assertEqual(self.db.load_detail_ids(self.conn), {"a"})

    def test_load_details_filters_by_job_ids(self):
        self.db.upsert_details(self.conn, [
            {"job_id": "a", "jd": "A", "tags": ["x"]},
            {"job_id": "b", "jd": "B"},
        ])
        got = self.db.load_details(self.conn, ["a", "missing"])
        self.assertEqual([d["job_id"] for d in got], ["a"])
        self.assertEqual(got[0]["tags"], ["x"])

    def test_load_details_all_when_no_filter(self):
        self.db.upsert_details(self.conn, [{"job_id": "a", "jd": "A"}])
        self.assertEqual(len(self.db.load_details(self.conn)), 1)

    def test_sanitize_strips_credentials_and_security_id(self):
        self.db.upsert_jobs(self.conn, [{
            "job_id": "a", "title": "T",
            "security_id": "SECRET-SID", "cookie": "c", "token": "t",
            "encrypt_job_id": "E", "lid": "L",
        }])
        payload = self.conn.execute(
            "SELECT payload FROM jobs WHERE job_id='a'").fetchone()[0]
        for banned in ("SECRET-SID", "security_id", "encrypt_job_id", "lid"):
            self.assertNotIn(banned, payload)
        self.assertIn("T", payload)

    def test_record_run_upserts_and_stats_reports_path(self):
        self.db.record_run(self.conn, "r1", keyword="k", city="上海",
                           job_count=3, detail_count=2,
                           now="2026-03-03T00:00:00+00:00")
        self.db.record_run(self.conn, "r1", job_count=5, detail_count=4,
                           now="2026-03-03T01:00:00+00:00")
        info = self.db.stats(self.conn)
        self.assertEqual(info["runs"], 1)
        self.assertEqual(str(info["db_path"]).replace("\\", "/"),
                         self.path.replace("\\", "/"))

    def test_open_store_rejects_future_schema_without_rewriting_version(self):
        future = os.path.join(self.tmp.name, "future.db")
        conn = sqlite3.connect(future)
        conn.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO schema_meta(key, value) VALUES ('schema_version', '999')")
        conn.commit()
        conn.close()
        with self.assertRaises(self.db.DbSchemaError):
            self.db.open_store(future)
        check = sqlite3.connect(future)
        value = check.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0]
        check.close()
        self.assertEqual(value, "999")

    def test_readonly_store_does_not_create_or_mutate_missing_file(self):
        missing = os.path.join(self.tmp.name, "readonly-missing.db")
        with self.assertRaises(FileNotFoundError):
            self.db.open_store_readonly(missing)
        self.assertFalse(os.path.exists(missing))
        readonly = self.db.open_store_readonly(self.path)
        try:
            self.assertEqual(readonly.execute("PRAGMA query_only").fetchone()[0], 1)
        finally:
            readonly.close()

    def test_stats_reads_persisted_schema_version(self):
        self.conn.execute(
            "UPDATE schema_meta SET value='7' WHERE key='schema_version'")
        self.conn.commit()
        self.assertEqual(self.db.stats(self.conn)["schema_version"], 7)

    def test_record_run_can_leave_finished_at_open_for_later_finalize(self):
        self.db.record_run(self.conn, "r-open", mode="single", keyword="AI",
                           city="上海", finished_at=None)
        row = self.conn.execute(
            "SELECT finished_at FROM runs WHERE run_id='r-open'").fetchone()
        self.assertIsNone(row[0])
        self.db.record_run(self.conn, "r-open", mode="single", keyword="AI",
                           city="上海", job_count=2, detail_count=1)
        row = self.conn.execute(
            "SELECT finished_at, job_count, detail_count FROM runs WHERE run_id='r-open'"
        ).fetchone()
        self.assertIsNotNone(row[0])
        self.assertEqual((row[1], row[2]), (2, 1))

    def test_main_reports_stats_and_handles_missing_db(self):
        self.db.upsert_jobs(self.conn, [{"job_id": "a", "title": "T"}])
        self.conn.close()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = self.db._main([self.path])
        self.assertEqual(rc, 0)
        self.assertIn("列表: 1 条", out.getvalue())
        self.conn = self.db.open_store(self.path)  # 供 tearDown 关闭

        out_missing = io.StringIO()
        with contextlib.redirect_stdout(out_missing):
            rc_missing = self.db._main([os.path.join(self.tmp.name, "nope.db")])
        self.assertEqual(rc_missing, 1)
        self.assertIn("数据库不存在", out_missing.getvalue())


class CrossRunDeltaTests(unittest.TestCase):
    """跨 run 对账（#175）：城市口径记录、COALESCE 保口径、baseline/delta。"""

    def setUp(self):
        self.db = load_db_store()
        self.tmp = tempfile.TemporaryDirectory(prefix="dbstore-delta-")
        self.addCleanup(self.tmp.cleanup)
        self.conn = self.db.open_store(os.path.join(self.tmp.name, "boss.db"))

    def tearDown(self):
        self.conn.close()

    def _job(self, job_id):
        return {"job_id": job_id, "title": "岗位", "company_name": "公司",
                "job_link": f"https://x/{job_id}.html"}

    def test_upsert_records_city_and_conflict_preserves_scope(self):
        ins, upd = self.db.upsert_jobs(self.conn, [self._job("j1")], city="杭州")
        self.assertEqual((ins, upd), (1, 0))
        # 再次入库不带口径 → COALESCE 保住已有 city，不洗成 NULL
        self.db.upsert_jobs(self.conn, [self._job("j1")])
        row = self.conn.execute(
            "SELECT city, keyword FROM jobs WHERE job_id='j1'").fetchone()
        self.assertEqual(row["city"], "杭州")
        self.assertIsNone(row["keyword"])

    def test_load_city_job_ids_scopes_by_city(self):
        self.db.upsert_jobs(self.conn, [self._job("a"), self._job("b")],
                            city="杭州")
        self.db.upsert_jobs(self.conn, [self._job("c")], city="上海")
        self.db.upsert_jobs(self.conn, [self._job("legacy")])  # 无口径旧行
        self.assertEqual(self.db.load_city_job_ids(self.conn, "杭州"), {"a", "b"})
        self.assertEqual(self.db.load_city_job_ids(self.conn, ""), set())

    def test_delta_baseline_then_added_removed_kept(self):
        first = self.db.cross_run_delta(self.conn, "杭州", ["a", "b"])
        self.assertTrue(first["baseline"])
        self.assertEqual(first["added"], ["a", "b"])
        self.db.upsert_jobs(self.conn, [self._job("a"), self._job("b")],
                            city="杭州")
        second = self.db.cross_run_delta(self.conn, "杭州", ["b", "c"])
        self.assertFalse(second["baseline"])
        self.assertEqual(second["added"], ["c"])
        self.assertEqual(second["removed"], ["a"])
        self.assertEqual(second["kept_count"], 1)

    def test_delta_without_city_returns_none(self):
        self.assertIsNone(self.db.cross_run_delta(self.conn, "", ["a"]))


class SchemaMigrationTests(unittest.TestCase):
    """v1 → v2 迁移：补列、版本前进、旧数据不动、幂等。"""

    def setUp(self):
        self.db = load_db_store()

    def _make_v1_db(self, path):
        conn = sqlite3.connect(path)
        conn.executescript(
            "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
            "CREATE TABLE runs (run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL,"
            " finished_at TEXT, mode TEXT, keyword TEXT, city TEXT,"
            " job_count INTEGER NOT NULL DEFAULT 0,"
            " detail_count INTEGER NOT NULL DEFAULT 0);"
            "CREATE TABLE jobs (job_id TEXT PRIMARY KEY, payload TEXT NOT NULL,"
            " first_seen_at TEXT NOT NULL, updated_at TEXT NOT NULL, run_id TEXT);"
            "CREATE TABLE details (job_id TEXT PRIMARY KEY, jd TEXT,"
            " payload TEXT NOT NULL, first_seen_at TEXT NOT NULL,"
            " updated_at TEXT NOT NULL, run_id TEXT);"
            "INSERT INTO schema_meta VALUES ('schema_version', '1');"
            "INSERT INTO jobs (job_id, payload, first_seen_at, updated_at)"
            " VALUES ('old-row', '{}', 't1', 't1');")
        conn.commit()
        conn.close()

    def test_v1_database_migrates_and_keeps_rows(self):
        with tempfile.TemporaryDirectory(prefix="dbstore-mig-") as tmp:
            path = os.path.join(tmp, "boss.db")
            self._make_v1_db(path)
            conn = self.db.open_store(path)
            try:
                cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
                self.assertIn("keyword", cols)
                self.assertIn("city", cols)
                row = conn.execute(
                    "SELECT job_id, city FROM jobs").fetchone()
                self.assertEqual(row["job_id"], "old-row")
                self.assertIsNone(row["city"])
                version = conn.execute(
                    "SELECT value FROM schema_meta WHERE key='schema_version'"
                ).fetchone()[0]
                self.assertEqual(version, str(self.db.SCHEMA_VERSION))
            finally:
                conn.close()
            # 迁移后的库再次打开：幂等，不再改数据
            conn = self.db.open_store(path)
            try:
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)
            finally:
                conn.close()

    def test_fresh_store_starts_at_current_version_with_scope_columns(self):
        with tempfile.TemporaryDirectory(prefix="dbstore-fresh-") as tmp:
            conn = self.db.open_store(os.path.join(tmp, "boss.db"))
            try:
                cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
                self.assertIn("city", cols)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
