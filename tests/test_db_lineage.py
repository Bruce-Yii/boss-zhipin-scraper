"""SQLite run lineage 与列表元数据回归测试。"""

import importlib.util
import json
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "boss_cdp_raw.py"


def load_module():
    sys.modules.setdefault("websocket", mock.Mock())
    sys.modules.setdefault("requests", mock.Mock())
    spec = importlib.util.spec_from_file_location("boss_db_lineage", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class DbLineageTests(unittest.TestCase):
    def _patches(self, module, db_path, list_side_effect):
        argv = [
            "boss_cdp_raw.py", "--keyword", "CLI关键词", "--city", "上海",
            "--pages", "1", "--no-detail", "--db", db_path,
        ]
        return (
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(module, "require_runtime_dependencies", return_value=True),
            mock.patch.object(module, "cleanup_stale_tmp_files"),
            mock.patch.object(module, "cleanup_security_sidecars"),
            mock.patch.object(module, "resolve_city", return_value=("上海", "101020100")),
            mock.patch.object(module, "check_cdp_cooldown", return_value=None),
            mock.patch.object(module, "acquire_scrape_lock", return_value=True),
            mock.patch.object(module, "release_scrape_lock"),
            mock.patch.object(module, "check_login_state",
                              return_value=module.LoginProbeResult(module.LoginProbeStatus.AVAILABLE)),
            mock.patch.object(module, "close_orphan_dock_tabs", return_value=0),
            mock.patch.object(module, "close_orphan_detail_tabs", return_value=0),
            mock.patch.object(module, "scrape_list", side_effect=list_side_effect),
        )

    def test_run_starts_before_list_and_finalizes_with_actual_metadata(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(pathlib.Path(tmp) / "boss.db")
            observed = []

            def list_side_effect(*args, **kwargs):
                conn = sqlite3.connect(db_path)
                observed.append(conn.execute(
                    "SELECT finished_at FROM runs").fetchone())
                conn.close()
                return {
                    "keyword": "实际关键词", "city": "杭州",
                    "city_code": "101210100",
                    "jobs": [{
                        "job_id": "j1", "title": "岗位", "location": "杭州",
                        "job_link": "https://www.zhipin.com/job_detail/j1.html",
                        "company_name": "公司",
                    }],
                    "output_path": str(pathlib.Path(tmp) / "jobs.json"),
                    "security_map": {"j1": "sid"},
                }

            patches = self._patches(module, db_path, list_side_effect)
            with patches[0], patches[1], patches[2], patches[3], patches[4], \
                    patches[5], patches[6], patches[7], patches[8], patches[9], \
                    patches[10], patches[11]:
                module.run_cli()
            conn = sqlite3.connect(db_path)
            row = conn.execute(
                "SELECT started_at, finished_at, keyword, city, job_count, detail_count "
                "FROM runs").fetchone()
            conn.close()
        self.assertEqual(observed[0][0], None)
        self.assertIsNotNone(row[1])
        self.assertEqual((row[2], row[3], row[4], row[5]),
                         ("实际关键词", "杭州", 1, 0))
        self.assertIsNotNone(row[0])

    def test_input_run_uses_loaded_list_metadata_for_final_row(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            db_path = str(root / "boss.db")
            input_path = root / "input.json"
            input_path.write_text(json.dumps({
                "keyword": "文件关键词", "city": "南京",
                "jobs": [{
                    "job_id": "j1", "title": "岗位", "location": "南京",
                    "job_link": "https://www.zhipin.com/job_detail/j1.html",
                    "company_name": "公司",
                }],
            }), encoding="utf-8")
            argv = [
                "boss_cdp_raw.py", "--input", str(input_path),
                "--no-detail", "--db", db_path,
            ]
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(module, "require_runtime_dependencies", return_value=True), \
                    mock.patch.object(module, "cleanup_stale_tmp_files"), \
                    mock.patch.object(module, "cleanup_security_sidecars"), \
                    mock.patch.object(module, "check_cdp_cooldown", return_value=None), \
                    mock.patch.object(module, "acquire_scrape_lock", return_value=True), \
                    mock.patch.object(module, "release_scrape_lock"):
                module.run_cli()
            conn = sqlite3.connect(db_path)
            row = conn.execute(
                "SELECT keyword, city, job_count FROM runs").fetchone()
            conn.close()
        self.assertEqual(row, ("文件关键词", "南京", 1))

    def test_run_exception_leaves_open_lineage_and_closes_db(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(pathlib.Path(tmp) / "boss.db")
            patches = self._patches(module, db_path,
                                     mock.Mock(side_effect=RuntimeError("boom")))
            with patches[0], patches[1], patches[2], patches[3], patches[4], \
                    patches[5], patches[6], patches[7], patches[8], patches[9], \
                    patches[10], patches[11]:
                with self.assertRaisesRegex(RuntimeError, "boom"):
                    module.run_cli()
            conn = sqlite3.connect(db_path)
            row = conn.execute(
                "SELECT finished_at, job_count, detail_count FROM runs").fetchone()
            conn.close()
        self.assertIsNone(row[0])
        self.assertEqual((row[1], row[2]), (0, 0))


if __name__ == "__main__":
    unittest.main()
