"""交付物构建入口（scripts/build_deliverable.py）的行为锁定（issue #146 / #184）。

合成输入跑完整拼装（列表过滤 + 旧交付回填 + 生产合并 + 落盘 + CSV），
不断网、不碰真实 job-result；#184 另起一组用临时 SQLite 库验证「从原材料库
一键重建」：口径过滤（city/keyword/since/recruit-type）、只读不改库、缺库报错。
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


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _job(jid, title="AI产品经理", job_type="4"):
    return {
        "job_id": jid, "title": title, "company_name": "公司",
        "salary": "20-30K", "location": "杭州",
        "job_link": f"https://x/{jid}.html", "job_type": job_type,
    }


JD = "负责AI产品规划与设计。" * 30


class BuildDeliverableTests(unittest.TestCase):

    def setUp(self):
        self.module = load_module("build_deliverable",
                                  SCRIPTS / "build_deliverable.py")

    def _write(self, directory, name, payload):
        path = os.path.join(directory, name)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        return path

    def test_full_assemble_keeps_intern_and_backfills_old(self):
        """指定 intern：列表过滤 + 旧交付 JD 回填 + 落盘 JSON/CSV。"""
        with tempfile.TemporaryDirectory() as tmp:
            list1 = self._write(tmp, "list1.json", {"jobs": [
                _job("ta"), _job("tb", job_type="")]})  # tb 社招被过滤
            list2 = self._write(tmp, "list2.json", {"jobs": [_job("tc")]})
            details = self._write(tmp, "det.json", [
                {"job_id": "ta", "jd": JD}, {"job_id": "tc", "jd": JD}])
            old = self._write(tmp, "old.json", {"jobs": [
                dict(_job("td"), jd=JD)]})  # td 靠旧 JD 回填留下
            target = os.path.join(tmp, "out.json")
            code = self.module.main([
                "--result-dir", tmp,
                "--lists", list1, list2,
                "--details", details,
                "--old", old,
                "--target", target,
                "--keyword", "AI产品实习", "--city", "杭州",
                "--scope", "测试口径",
                "--target-roots", "AI产品,产品",
                "--recruit-type", "intern",
            ])
            self.assertEqual(code, 0)
            with open(target, encoding="utf-8") as handle:
                final = json.load(handle)
            ids = sorted(j["job_id"] for j in final["jobs"])
            self.assertEqual(ids, ["ta", "tc", "td"])
            self.assertEqual(final["job_count"], 3)
            self.assertEqual(final.get("target_roots"), ["AI产品", "产品"])
            self.assertIn("acceptance", final)
            csv_path = os.path.join(tmp, "out.csv")
            self.assertTrue(os.path.exists(csv_path))
            with open(csv_path, encoding="utf-8-sig") as handle:
                header = handle.readline()
            self.assertIn("job_id", header)

    def test_default_recruit_type_is_all(self):
        """不指定类型=全要：社招也不过滤（用户口径，issue #152）。"""
        with tempfile.TemporaryDirectory() as tmp:
            list1 = self._write(tmp, "list1.json", {"jobs": [
                _job("ta"), _job("tb", job_type="")]})
            details = self._write(tmp, "det.json", [
                {"job_id": "ta", "jd": JD},
                {"job_id": "tb", "jd": "负责AI产品规划与需求分析"}])
            target = os.path.join(tmp, "out.json")
            code = self.module.main([
                "--result-dir", tmp,
                "--lists", list1,
                "--details", details,
                "--target", target,
                "--keyword", "AI产品", "--city", "杭州",
            ])
            self.assertEqual(code, 0)
            with open(target, encoding="utf-8") as handle:
                final = json.load(handle)
            # tb 有 AI 证据+销售词 → 疑似保留；不指定类型时不过滤
            self.assertIn("tb", [j["job_id"] for j in final["jobs"]])

    def test_missing_input_returns_2(self):
        """输入文件不存在 → 报错退出码 2，不写盘。"""
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "out.json")
            code = self.module.main([
                "--result-dir", tmp,
                "--lists", os.path.join(tmp, "nope.json"),
                "--details", os.path.join(tmp, "det.json"),
                "--target", target,
            ])
            self.assertEqual(code, 2)
            self.assertFalse(os.path.exists(target))

    def test_recruit_type_comma_combo_file_mode(self):
        """--recruit-type 支持逗号组合（实习+校招一次过滤）。"""
        with tempfile.TemporaryDirectory() as tmp:
            list1 = self._write(tmp, "list1.json", {"jobs": [
                _job("ia", job_type="4"),
                _job("ca", title="AI产品经理（校招）", job_type="5"),
                _job("sa", job_type="")]})
            details = self._write(tmp, "det.json", [
                {"job_id": "ia", "jd": JD}, {"job_id": "ca", "jd": JD},
                {"job_id": "sa", "jd": JD}])
            target = os.path.join(tmp, "out.json")
            code = self.module.main([
                "--result-dir", tmp, "--lists", list1,
                "--details", details, "--target", target,
                "--recruit-type", "intern,campus",
            ])
            self.assertEqual(code, 0)
            with open(target, encoding="utf-8") as handle:
                ids = sorted(j["job_id"] for j in json.load(handle)["jobs"])
            self.assertEqual(ids, ["ca", "ia"])

    def test_bad_recruit_type_returns_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = self.module.main([
                "--result-dir", tmp, "--lists", "a.json", "--details", "b.json",
                "--target", os.path.join(tmp, "out.json"),
                "--recruit-type", "intern,fulltime",
            ])
            self.assertEqual(code, 2)


def load_db_store():
    """按 scripts 命名空间导入 db_store（与安装后的 scripts.db_store 一致）。"""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from scripts import db_store
    return db_store


def _detail(jid, jd=JD):
    return {"job_id": jid, "jd": jd}


class DbDeliverableTests(unittest.TestCase):
    """issue #184：--from-db 从 SQLite 原材料库一键重建交付物。"""

    def setUp(self):
        self.module = load_module("build_deliverable", SCRIPTS / "build_deliverable.py")
        self.db_store = load_db_store()
        self.tmp = tempfile.TemporaryDirectory(prefix="deliverable-db-")
        self.addCleanup(self.tmp.cleanup)
        self.db_path = os.path.join(self.tmp.name, "boss.db")

    def _seed(self, jobs, details, run_id="run-1", keyword="产品实习生,AI产品实习生",
              city="杭州", now=None):
        conn = self.db_store.open_store(self.db_path)
        try:
            self.db_store.record_run(conn, run_id, mode="single",
                                     keyword=keyword, city=city)
            self.db_store.upsert_jobs(conn, jobs, run_id=run_id,
                                      keyword=keyword, city=city, now=now)
            self.db_store.upsert_details(conn, details, run_id=run_id, now=now)
        finally:
            conn.close()

    def _counts(self):
        conn = self.db_store.open_store_readonly(self.db_path)
        try:
            info = self.db_store.stats(conn)
        finally:
            conn.close()
        return info["jobs"], info["details"]

    def _run(self, *extra):
        target = os.path.join(self.tmp.name, "out.json")
        args = ["--result-dir", self.tmp.name, "--from-db", self.db_path,
                "--target", target] + list(extra)
        with contextlib.redirect_stdout(io.StringIO()):
            code = self.module.main(args)
        return code, target

    def test_from_db_keeps_only_recruit_and_with_jd(self):
        """库模式：recruit 过滤 + 口径一（无 JD 剔除）+ 生产合并 + CSV + meta 溯源。"""
        self._seed(
            jobs=[_job("ia", job_type="4"),
                  _job("ca", title="AI产品经理（校招）", job_type="5"),
                  _job("sa", job_type=""),
                  _job("ib", job_type="4")],       # 无详情 → 口径一剔除
            details=[_detail("ia"), _detail("ca"), _detail("sa")])
        before = self._counts()
        code, target = self._run("--city", "杭州",
                                 "--recruit-type", "intern,campus")
        self.assertEqual(code, 0)
        with open(target, encoding="utf-8") as handle:
            final = json.load(handle)
        self.assertEqual(sorted(j["job_id"] for j in final["jobs"]), ["ca", "ia"])
        self.assertEqual(final["source"]["kind"], "db")
        self.assertEqual(final["source"]["recruit_types"], ["campus", "intern"])
        self.assertEqual(final["source"]["jobs_selected"], 4)
        self.assertTrue(os.path.exists(target.rsplit(".", 1)[0] + ".csv"))
        self.assertEqual(self._counts(), before)  # 只读：行数不变

    def test_from_db_city_filter(self):
        """--city 精确过滤：其他城市不混进交付物。"""
        self._seed(jobs=[_job("ia", job_type="4"),
                         _job("sh", job_type="4")],
                   details=[_detail("ia"), _detail("sh")])
        conn = self.db_store.open_store(self.db_path)
        try:
            self.db_store.upsert_jobs(
                conn, [dict(_job("sh", job_type="4"), location="上海")],
                run_id="run-2", keyword="产品实习生", city="上海")
        finally:
            conn.close()
        code, target = self._run("--city", "杭州")
        self.assertEqual(code, 0)
        with open(target, encoding="utf-8") as handle:
            ids = sorted(j["job_id"] for j in json.load(handle)["jobs"])
        self.assertEqual(ids, ["ia"])

    def test_from_db_since_filter(self):
        """--since 按最后出现时间过滤（旧行不再进交付物）。"""
        self._seed(jobs=[_job("old", job_type="4")],
                   details=[_detail("old")],
                   run_id="run-old", now="2026-10-20T00:00:00+00:00")
        self._seed(jobs=[_job("new", job_type="4")],
                   details=[_detail("new")],
                   run_id="run-new", now="2026-10-27T00:00:00+00:00")
        code, target = self._run("--city", "杭州", "--since", "2026-10-25")
        self.assertEqual(code, 0)
        with open(target, encoding="utf-8") as handle:
            ids = sorted(j["job_id"] for j in json.load(handle)["jobs"])
        self.assertEqual(ids, ["new"])

    def test_from_db_keyword_falls_back_to_run_label(self):
        """旧行 jobs.keyword 为 NULL 时，按关联 run 的关键词标签兜底匹配。"""
        self._seed(jobs=[_job("ia", job_type="4")], details=[_detail("ia")])
        conn = self.db_store.open_store(self.db_path)
        try:
            conn.execute("UPDATE jobs SET keyword = NULL")
            conn.commit()
        finally:
            conn.close()
        code, target = self._run("--city", "杭州", "--keyword", "产品实习生")
        self.assertEqual(code, 0)
        with open(target, encoding="utf-8") as handle:
            ids = [j["job_id"] for j in json.load(handle)["jobs"]]
        self.assertEqual(ids, ["ia"])

    def test_from_db_missing_db_returns_2(self):
        """库不存在 → 退出码 2，不创建库、不写交付物。"""
        missing = os.path.join(self.tmp.name, "nope.db")
        target = os.path.join(self.tmp.name, "out.json")
        with contextlib.redirect_stdout(io.StringIO()):
            code = self.module.main([
                "--result-dir", self.tmp.name, "--from-db", missing,
                "--target", target])
        self.assertEqual(code, 2)
        self.assertFalse(os.path.exists(missing))
        self.assertFalse(os.path.exists(target))

    def test_from_db_conflicts_with_file_inputs(self):
        """--from-db 与 --lists/--details 互斥 → 退出码 2。"""
        self._seed(jobs=[_job("ia", job_type="4")], details=[_detail("ia")])
        with contextlib.redirect_stdout(io.StringIO()):
            code = self.module.main([
                "--result-dir", self.tmp.name, "--from-db", self.db_path,
                "--lists", "x.json", "--details", "y.json",
                "--target", os.path.join(self.tmp.name, "out.json")])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
