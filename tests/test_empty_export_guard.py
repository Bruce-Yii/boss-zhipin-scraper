"""空导出不覆盖原文件（独立审计 F8）。

详情全失败（登录失效 / 风控 / 坏页台账）时，kept 会是 0。旧行为把列表文件里已有的
title/薪资/公司数据整个覆盖成空数组，**退出码还是 0** → 下游按"成功"处理就拿到空表。
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


class EmptyExportGuardTests(unittest.TestCase):

    def setUp(self):
        self.ec = load_module("export_contract", SCRIPTS / "export_contract.py")

    def _list_file(self, directory, count=3):
        path = os.path.join(directory, "jobs.json")
        payload = {
            "keyword": "AI产品经理", "city": "杭州", "total": count, "job_count": count,
            "jobs": [{"job_id": f"j{i}", "title": f"AI产品经理{i}",
                      "company_name": f"公司{i}", "salary": "20-30K",
                      "location": "杭州", "job_link": f"https://x/{i}.html"}
                     for i in range(count)],
        }
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        return path

    def test_empty_merge_keeps_original_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._list_file(tmp)
            kept, dropped = self.ec._merge_jd_into_export(path, [])
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            self.assertEqual(kept, 0)
            self.assertEqual(dropped, 3)
            self.assertEqual(len(data["jobs"]), 3, "原文件的数据不能被覆盖成空")
            self.assertTrue(data.get("empty_export_blocked"))
            self.assertTrue(any("未覆盖原文件" in w for w in data.get("warnings") or []))

    def test_normal_merge_still_drops_without_jd(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._list_file(tmp)
            details = [{"job_id": "j0", "jd": "岗位职责：" + "描述内容。" * 30}]
            kept, dropped = self.ec._merge_jd_into_export(path, details)
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            self.assertEqual(kept, 1)
            self.assertEqual(dropped, 2)
            self.assertEqual(len(data["jobs"]), 1)
            self.assertNotIn("empty_export_blocked", data)


if __name__ == "__main__":
    unittest.main()
