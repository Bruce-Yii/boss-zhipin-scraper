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

    def test_incremental_merge_preserves_no_jd_history(self):
        """增量合入：无 JD 丢弃历史累计保留，coverage 与清单对得上账（issue #167）。

        旧行为第二轮合入后 jd_coverage 缩成单轮数字（1/1/0），与残留的
        dropped_no_jd 清单矛盾。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = self._list_file(tmp)
            details = [{"job_id": "j0", "jd": "岗位职责：" + "描述内容。" * 30}]
            self.ec._merge_jd_into_export(path, details)
            with open(path, encoding="utf-8") as handle:
                first = json.load(handle)
            self.assertEqual(first["dropped_no_jd"], ["j1", "j2"])
            self.assertEqual(first["jd_coverage"],
                             {"with_jd": 1, "total_before": 3, "dropped_no_jd": 2})
            self.ec._merge_jd_into_export(path, details)
            with open(path, encoding="utf-8") as handle:
                second = json.load(handle)
            self.assertEqual(second["dropped_no_jd"], ["j1", "j2"])
            self.assertEqual(second["jd_coverage"],
                             {"with_jd": 1, "total_before": 3, "dropped_no_jd": 2})

    def test_dropped_junk_accumulates_across_merges(self):
        """删库清单跨轮累计：第二轮无新增时不被空结果覆盖，coverage 仍对账。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = self._list_file(tmp, count=2)
            with open(path, encoding="utf-8") as handle:
                payload = json.load(handle)
            payload["jobs"][1]["title"] = "AI产品销售经理"
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
            details = [
                {"job_id": "j0", "jd": "岗位职责：" + "描述内容。" * 30},
                {"job_id": "j1", "jd": "岗位职责：" + "描述内容。" * 30},
            ]
            kept, _ = self.ec._merge_jd_into_export(path, details)
            self.assertEqual(kept, 1)
            with open(path, encoding="utf-8") as handle:
                first = json.load(handle)
            self.assertEqual([x["job_id"] for x in first["dropped_junk"]], ["j1"])
            self.assertEqual(first["jd_coverage"],
                             {"with_jd": 2, "total_before": 2, "dropped_no_jd": 0})
            self.ec._merge_jd_into_export(path, details)
            with open(path, encoding="utf-8") as handle:
                second = json.load(handle)
            self.assertEqual([x["job_id"] for x in second["dropped_junk"]], ["j1"])
            self.assertEqual(second["jd_coverage"],
                             {"with_jd": 2, "total_before": 2, "dropped_no_jd": 0})

    def test_job_returning_to_product_leaves_dropped_list(self):
        """岗位回炉（重新进产物）→ 旧丢弃记录不残留，清单只描述不在产物里的。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = self._list_file(tmp, count=2)
            details1 = [{"job_id": "j0", "jd": "岗位职责：" + "描述内容。" * 30}]
            self.ec._merge_jd_into_export(path, details1)
            with open(path, encoding="utf-8") as handle:
                self.assertEqual(json.load(handle)["dropped_no_jd"], ["j1"])
            with open(path, encoding="utf-8") as handle:
                payload = json.load(handle)
            payload["jobs"].append({"job_id": "j1", "title": "AI产品经理",
                                    "company_name": "公司1", "salary": "20-30K",
                                    "location": "杭州",
                                    "job_link": "https://x/1.html"})
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
            details2 = details1 + [{"job_id": "j1",
                                    "jd": "岗位职责：" + "描述内容。" * 30}]
            self.ec._merge_jd_into_export(path, details2)
            with open(path, encoding="utf-8") as handle:
                final = json.load(handle)
            self.assertNotIn("dropped_no_jd", final)
            self.assertEqual(final["jd_coverage"],
                             {"with_jd": 2, "total_before": 2, "dropped_no_jd": 0})


if __name__ == "__main__":
    unittest.main()
