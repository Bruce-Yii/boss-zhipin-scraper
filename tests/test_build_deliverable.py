"""交付物构建入口（scripts/build_deliverable.py）的行为锁定（issue #146）。

合成输入跑完整拼装（列表过滤 + 旧交付回填 + 生产合并 + 落盘 + CSV），
不断网、不碰真实 job-result。
"""

import importlib.util
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
        """列表过滤 intern + 旧交付 JD 回填 + 落盘 JSON/CSV。"""
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


if __name__ == "__main__":
    unittest.main()
