"""P4c-2 SQLite 增量层测试：WAL / upsert 幂等 / 时间戳语义 / 续抓读取 / 合规脱敏。

全离线：不依赖真实 Chrome/网络；库文件落在临时目录，不污染用户状态目录。
"""

import contextlib
import io
import json
import os
import pathlib
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


if __name__ == "__main__":
    unittest.main()
