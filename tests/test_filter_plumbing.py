"""过滤贯通性（独立审计 F2/F3/F11 的根因修法）。

三个现象其实是同一个根因：过滤发生在"编排层"而不是"产出层"——
数据从列表出来会经过多个写盘点（列表文件、merge、两个 CSV），而过滤只在 main() 的
某个分支里改了一份**内存副本**。

- F2：控制台说"丢弃 N 条"，落盘 JSON/CSV 仍是全量；
- F3：``--merge`` 把被丢掉的岗位从旧文件复活，**还会再花一次详情额度**；
- F11：``--input`` / ``--batch`` 两条入口完全忽略过滤参数（静默忽略）。

修法：过滤收敛到 ``apply_run_filters`` 一处，直接改 ``list_data``（所有写盘点读同一份），
调用点在**合并之后**，且 live / --input / --batch 三条入口走同一入口。
"""

import importlib.util
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _row(job_id, title, industry="互联网", job_type=""):
    return {"job_id": job_id, "title": title, "company_name": "公司" + job_id,
            "company_industry": industry, "job_type": job_type,
            "salary": "20-30K", "location": "杭州"}


class ApplyRunFiltersTests(unittest.TestCase):

    def setUp(self):
        self.main = load_module("boss_cdp_raw", SCRIPTS / "boss_cdp_raw.py")

    def test_filter_updates_the_same_list_data(self):
        """过滤必须**直接改 list_data**（否则落盘口径与控制台不一致 → F2）。"""
        data = {"jobs": [_row("a", "AI产品经理"),
                         _row("b", "产品经理", industry="餐饮")],
                "total": 2, "job_count": 2}
        kept, dropped, detail = self.main.apply_run_filters(
            data, args={"industry_block": ["餐饮"]})
        self.assertEqual(kept, 1)
        self.assertEqual(dropped, 1)
        self.assertEqual(len(data["jobs"]), 1)
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["job_count"], 1)
        self.assertEqual(detail["industry_block"], 1)

    def test_merged_jobs_are_filtered_too(self):
        """合并进来的旧岗位同样要过过滤（否则复活 + 再花额度 → F3）。"""
        merged = {"jobs": [_row("old", "实习生", industry="房地产中介/租赁", job_type="4"),
                           _row("new", "AI产品经理")],
                  "total": 2, "job_count": 2}
        kept, dropped, _ = self.main.apply_run_filters(
            merged, args={"recruit_type": ["intern"]})
        self.assertEqual(kept, 1)
        self.assertEqual(merged["jobs"][0]["job_id"], "old")
        self.assertEqual(dropped, 1)

    def test_no_options_means_no_change(self):
        data = {"jobs": [_row("a", "AI产品经理")], "total": 1, "job_count": 1}
        kept, dropped, _ = self.main.apply_run_filters(data, args={})
        self.assertEqual(kept, 1)
        self.assertEqual(dropped, 0)

    def test_filter_options_helper_reads_both_shapes(self):
        class Args:
            recruit_type = "intern"
            industry_allow = None
            industry_block = "房地产中介/租赁"
            only_relevant_title = True

        from_args = self.main._filter_options(Args())
        from_dict = self.main._filter_options(
            {"recruit_type": "intern", "industry_block": "房地产中介/租赁",
             "only_relevant_title": True})
        self.assertEqual(from_args["recruit_type"], "intern")
        self.assertEqual(from_dict["industry_block"], "房地产中介/租赁")
        self.assertTrue(from_args["only_relevant_title"])

    def test_batch_task_carries_relevance_filters(self):
        """--batch 的任务级过滤参数要能透传（此前完全忽略且无提示 → F11）。"""
        self.assertIn("recruit_type", self.main.RELEVANCE_FILTER_KEYS)
        self.assertIn("industry_block", self.main.RELEVANCE_FILTER_KEYS)


if __name__ == "__main__":
    unittest.main()
