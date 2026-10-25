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

    def test_cross_run_delta_recorded_in_list_meta(self):
        """跨 run 对账（#175）：第二次 run 的列表 meta 记 added/removed/kept。"""
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(pathlib.Path(tmp) / "boss.db")
            out = pathlib.Path(tmp) / "jobs.json"

            def make_side_effect(job_ids):
                def side_effect(*args, **kwargs):
                    payload = {
                        "keyword": "实际关键词", "city": "杭州",
                        "city_code": "101210100",
                        "jobs": [{
                            "job_id": jid, "title": "岗位", "location": "杭州",
                            "job_link": f"https://www.zhipin.com/job_detail/{jid}.html",
                            "company_name": "公司",
                        } for jid in job_ids],
                        "output_path": str(out),
                    }
                    out.write_text(json.dumps(payload, ensure_ascii=False),
                                   encoding="utf-8")
                    return payload
                return side_effect

            first_patches = self._patches(module, db_path, make_side_effect(["j1"]))
            with first_patches[0], first_patches[1], first_patches[2], \
                    first_patches[3], first_patches[4], first_patches[5], \
                    first_patches[6], first_patches[7], first_patches[8], \
                    first_patches[9], first_patches[10], first_patches[11]:
                module.run_cli()
            first = json.loads(out.read_text(encoding="utf-8"))
            self.assertTrue(first["cross_run"]["baseline"])

            second_patches = self._patches(
                module, db_path, make_side_effect(["j1", "j2"]))
            with second_patches[0], second_patches[1], second_patches[2], \
                    second_patches[3], second_patches[4], second_patches[5], \
                    second_patches[6], second_patches[7], second_patches[8], \
                    second_patches[9], second_patches[10], second_patches[11]:
                module.run_cli()
            second = json.loads(out.read_text(encoding="utf-8"))
            delta = second["cross_run"]
            self.assertFalse(delta["baseline"])
            self.assertEqual(delta["scope_city"], "杭州")
            self.assertEqual(delta["added"], ["j2"])
            self.assertEqual(delta["removed"], [])
            self.assertEqual(delta["kept_count"], 1)

    def test_no_db_flag_disables_store(self):
        """--no-db：默认启用的库被显式关闭，不建文件。"""
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(pathlib.Path(tmp) / "boss.db")
            argv = [
                "boss_cdp_raw.py", "--keyword", "关键词", "--city", "上海",
                "--pages", "1", "--no-detail", "--no-db",
            ]
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(module, "require_runtime_dependencies", return_value=True), \
                    mock.patch.object(module, "cleanup_stale_tmp_files"), \
                    mock.patch.object(module, "cleanup_security_sidecars"), \
                    mock.patch.object(module, "resolve_city", return_value=("上海", "101020100")), \
                    mock.patch.object(module, "check_cdp_cooldown", return_value=None), \
                    mock.patch.object(module, "acquire_scrape_lock", return_value=True), \
                    mock.patch.object(module, "release_scrape_lock"), \
                    mock.patch.object(module, "check_login_state",
                                      return_value=module.LoginProbeResult(
                                          module.LoginProbeStatus.AVAILABLE)), \
                    mock.patch.object(module, "close_orphan_dock_tabs", return_value=0), \
                    mock.patch.object(module, "close_orphan_detail_tabs", return_value=0), \
                    mock.patch.object(module, "scrape_list", return_value={
                        "keyword": "关键词", "city": "上海", "jobs": [],
                        "output_path": str(pathlib.Path(tmp) / "jobs.json")}), \
                    mock.patch.object(module, "DEFAULT_DB_PATH",
                                      str(pathlib.Path(tmp) / "boss.db")):
                module.run_cli()
            self.assertFalse(pathlib.Path(db_path).exists(),
                             "--no-db 不应创建 SQLite 库")


if __name__ == "__main__":
    unittest.main()
