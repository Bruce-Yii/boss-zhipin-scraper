"""产物层统一（scripts/artifacts.py）与交付物盖章的行为锁定（issue #186）。

全离线：临时目录里造 list/details/交付物/sidecar，验证分类、口径、取代关系、
时效标记、缓存重建，以及 build_deliverable 写下的 artifact 身份。
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

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"

JD = "负责AI产品规划与设计。" * 20


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _job(jid, with_jd=False, title="AI产品经理"):
    job = {
        "job_id": jid, "title": title, "company_name": "公司",
        "salary": "20-30K", "location": "杭州",
        "job_link": f"https://x/{jid}.html", "job_type": "4",
    }
    if with_jd:
        job["jd"] = JD
    return job


def write_json(directory, name, payload):
    path = os.path.join(directory, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    return path


class ArtifactIndexTests(unittest.TestCase):

    def setUp(self):
        self.module = load_module("artifacts", SCRIPTS / "artifacts.py")
        self.tmp = tempfile.TemporaryDirectory(prefix="artifacts-")
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name

    def _list_payload(self, keyword="产品实习生", city="杭州", jobs=None,
                      scraped_at="2026-10-27T12:00:00+08:00"):
        return {"keyword": keyword, "city": city, "format_version": 2,
                "scraped_at": scraped_at,
                "jobs": jobs if jobs is not None else [_job("a")]}

    def _deliverable(self, scope_key, created_at, jobs=None, label="实习AI"):
        return {
            "keyword": "实习AI", "city": "杭州",
            "jobs": jobs if jobs is not None else [_job("a", with_jd=True)],
            "job_count": 1,
            "artifact": {
                "kind": "deliverable", "format_version": 1,
                "created_at": created_at, "scope_key": scope_key,
                "scope": {"city": "杭州", "keyword": "实习AI",
                          "label": label, "recruit_types": ["intern"]},
            },
        }

    def test_classify_scan_and_sidecars(self):
        """列表/详情/交付物分类；failures 挂到详情本体，不当独立产物。"""
        write_json(self.dir, "boss_jobs_20261027_120000_1.json",
                   self._list_payload())
        write_json(self.dir, "boss_details_20261027_120000_1.json",
                   [{"job_id": "a", "jd": JD}])
        write_json(self.dir,
                   "boss_details_20261027_120000_1.json.failures.json",
                   [{"job_id": "b", "reason": "bad"}])
        write_json(self.dir, "boss_list_旧命名_但内容是列表.json",
                   self._list_payload(jobs=[_job("c")]))
        index = self.module.build_index(self.dir)
        kinds = {item["path"]: item["kind"] for item in index["artifacts"]}
        self.assertEqual(kinds["boss_jobs_20261027_120000_1.json"], "list")
        self.assertEqual(kinds["boss_details_20261027_120000_1.json"], "details")
        self.assertEqual(kinds["boss_list_旧命名_但内容是列表.json"], "list")
        self.assertNotIn(
            "boss_details_20261027_120000_1.json.failures.json", kinds)
        details = next(item for item in index["artifacts"]
                       if item["kind"] == "details")
        self.assertEqual(
            details["sidecars"],
            ["boss_details_20261027_120000_1.json.failures.json"])

    def test_scope_and_supersede(self):
        """同 scope_key 的新旧交付物：新的 active、旧的 superseded（不移动文件）。"""
        write_json(self.dir, "old.json",
                   self._deliverable("杭州|intern|实习AI",
                                     "2026-10-26T10:00:00+08:00"))
        write_json(self.dir, "new.json",
                   self._deliverable("杭州|intern|实习AI",
                                     "2026-10-27T10:00:00+08:00"))
        index = self.module.build_index(self.dir)
        by_path = {item["path"]: item for item in index["artifacts"]}
        self.assertEqual(by_path["old.json"]["status"], "superseded")
        self.assertEqual(by_path["new.json"]["status"], "active")
        self.assertTrue(os.path.exists(os.path.join(self.dir, "old.json")))
        latest = index["latest"]["deliverables"]
        self.assertEqual([item["path"] for item in latest], ["new.json"])
        self.assertEqual(latest[0]["scope_key"], "杭州|intern|实习AI")

    def test_archived_and_latest_list(self):
        """archive/ 下的产物 status=archived；latest 只挑 active。"""
        write_json(self.dir, "boss_jobs_20261026_120000_1.json",
                   self._list_payload(jobs=[_job("old")]))
        write_json(self.dir, "boss_jobs_20261027_120000_1.json",
                   self._list_payload(jobs=[_job("new")]))
        archive = os.path.join(self.dir, "archive")
        os.makedirs(archive, exist_ok=True)
        write_json(archive, "boss_jobs_20261020_120000_1.json",
                   self._list_payload(jobs=[_job("archived")]))
        index = self.module.build_index(self.dir)
        archived = [item for item in index["artifacts"]
                    if item["status"] == "archived"]
        self.assertEqual(len(archived), 1)
        self.assertEqual(index["latest"]["list"],
                         "boss_jobs_20261027_120000_1.json")

    def test_stale_flag(self):
        write_json(self.dir, "boss_jobs_20261001_120000_1.json",
                   self._list_payload(
                       scraped_at="2026-10-01T12:00:00+08:00"))
        write_json(self.dir, "boss_jobs_20261027_120000_1.json",
                   self._list_payload())
        index = self.module.build_index(self.dir, stale_days=7)
        self.assertEqual(index["summary"]["stale"], 1)
        by_path = {item["path"]: item for item in index["artifacts"]}
        self.assertTrue(by_path["boss_jobs_20261001_120000_1.json"]["stale"])
        self.assertFalse(by_path["boss_jobs_20261027_120000_1.json"]["stale"])
        fresh = self.module.build_index(self.dir, stale_days=-1)
        self.assertEqual(fresh["summary"]["stale"], 0)

    def test_index_roundtrip_and_corrupt_rebuild(self):
        """缓存可加载；损坏/版本不符时 load 返回 None（调用方重建）。"""
        write_json(self.dir, "boss_jobs_20261027_120000_1.json",
                   self._list_payload())
        index = self.module.refresh_index(self.dir)
        self.assertIsNotNone(self.module.load_index(self.dir))
        path = os.path.join(self.dir, self.module.INDEX_NAME)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{corrupt")
        self.assertIsNone(self.module.load_index(self.dir))
        rebuilt = self.module.refresh_index(self.dir)
        self.assertEqual(rebuilt["summary"]["total"],
                         index["summary"]["total"])
        # 清单自身不会把 artifacts.json 当产物扫进去
        self.assertNotIn(self.module.INDEX_NAME,
                         [item["path"] for item in rebuilt["artifacts"]])

    def test_json_output_is_pure_json(self):
        """--json 输出必须能被直接解析（agent 消费契约）。"""
        write_json(self.dir, "boss_jobs_20261027_120000_1.json",
                   self._list_payload())
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = self.module.run_artifacts(result_dir=self.dir, as_json=True)
        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["format_version"], self.module.FORMAT_VERSION)
        self.assertIn("latest", payload)
        self.assertEqual(payload["latest"]["list"],
                         "boss_jobs_20261027_120000_1.json")


class DeliverableStampingTests(unittest.TestCase):
    """build_deliverable 必须在产物里盖 artifact 章（清单据此识别系列）。"""

    def setUp(self):
        self.module = load_module(
            "build_deliverable", SCRIPTS / "build_deliverable.py")
        self.tmp = tempfile.TemporaryDirectory(prefix="deliverable-stamp-")
        self.addCleanup(self.tmp.cleanup)

    def _write(self, name, payload):
        return write_json(self.tmp.name, name, payload)

    def test_file_mode_stamps_artifact(self):
        list1 = self._write("list.json", {"jobs": [_job("a")]})
        details = self._write("det.json", [{"job_id": "a", "jd": JD}])
        target = os.path.join(self.tmp.name, "out.json")
        code = self.module.main([
            "--result-dir", self.tmp.name,
            "--lists", list1, "--details", details, "--target", target,
            "--keyword", "AI产品实习", "--city", "杭州",
            "--scope", "实习AI", "--scope-key", "杭州|intern|实习AI",
        ])
        self.assertEqual(code, 0)
        with open(target, encoding="utf-8") as handle:
            final = json.load(handle)
        artifact = final["artifact"]
        self.assertEqual(artifact["kind"], "deliverable")
        self.assertEqual(artifact["builder"], "build_deliverable")
        self.assertEqual(artifact["scope_key"], "杭州|intern|实习AI")
        self.assertEqual(artifact["scope"]["city"], "杭州")
        self.assertEqual(artifact["inputs"]["kind"], "files")
        self.assertEqual(artifact["inputs"]["lists"], ["list.json"])

    def test_db_mode_stamps_scope_and_source(self):
        artifacts = load_module("artifacts_for_db_test",
                                SCRIPTS / "artifacts.py")
        db_store = load_module("db_store_for_stamp", SCRIPTS / "db_store.py")
        db_path = os.path.join(self.tmp.name, "boss.db")
        conn = db_store.open_store(db_path)
        try:
            db_store.upsert_jobs(conn, [_job("a")], run_id="r1",
                                 keyword="产品实习生", city="杭州")
            db_store.upsert_details(conn, [{"job_id": "a", "jd": JD}],
                                    run_id="r1")
        finally:
            conn.close()
        target = os.path.join(self.tmp.name, "out.json")
        code = self.module.main([
            "--result-dir", self.tmp.name, "--from-db", db_path,
            "--city", "杭州", "--recruit-type", "intern",
            "--target", target, "--scope", "实习AI",
        ])
        self.assertEqual(code, 0)
        with open(target, encoding="utf-8") as handle:
            final = json.load(handle)
        self.assertEqual(final["artifact"]["scope_key"], "杭州|intern|实习AI")
        self.assertEqual(final["artifact"]["inputs"]["kind"], "db")
        self.assertEqual(final["source"]["kind"], "db")
        # 扫产物时它应该被认成交付物（不是普通列表快照）
        index = artifacts.build_index(self.tmp.name)
        kinds = {item["path"]: item["kind"] for item in index["artifacts"]}
        self.assertEqual(kinds["out.json"], "deliverable")


if __name__ == "__main__":
    unittest.main()
