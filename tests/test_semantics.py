"""岗位语义层（scripts/semantics.py）与链接刷新能力的单测。

背景（2026-10-22 逐条读 294 条导出后定位的根因）：
- 平台用 proxy_job/proxy_type 标注了"猎头代发"，我们从不语义化使用 → 25 条匿名+
  薪资虚高的代发岗混在结果里（11/25 薪资上限 ≥50K，企业直招岗 0 条）。
- 标题大小写/空格不一致 → 按字符串分组会漏。
- 同一份 JD 被多家猎头/多个发布重复投放（job_id 不同但内容同源）。
- 详情额度（按次数）与列表供给（按页数×组合）之间没有协调 → 结构性缺口。
- 平台 job_detail URL 是短时效凭证（隔天指向别的岗位）→ 需要"取新"与"可刷新"。
"""

import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class SemanticsTests(unittest.TestCase):

    def setUp(self):
        self.s = load_module("semantics", ROOT / "scripts" / "semantics.py")

    # ---------- 发布主体类型 ----------
    def test_platform_proxy_flag_wins(self):
        """平台自己标的 proxy_job/proxy_type → agency（不靠猜公司名）。"""
        self.assertEqual(self.s.classify_post_type({"proxy_job": "1"}), "agency")
        self.assertEqual(self.s.classify_post_type({"proxy_type": "4"}), "agency")
        self.assertEqual(self.s.classify_post_type({"proxy_job": "", "proxy_type": ""}),
                         "direct")

    def test_anonymous_company_is_flagged(self):
        self.assertEqual(
            self.s.classify_post_type({"company_name": "某大型互联网直播电商上市公司"}),
            "anonymous")
        self.assertEqual(
            self.s.classify_post_type({"company_name": "某知名AI公司"}), "anonymous")
        self.assertEqual(
            self.s.classify_post_type({"company_name": "闪念科技"}), "direct")

    def test_agency_like_covers_both(self):
        self.assertTrue(self.s.is_agency_like({"proxy_job": "1"}))
        self.assertTrue(self.s.is_agency_like({"company_name": "某公司"}))
        self.assertFalse(self.s.is_agency_like({"company_name": "闪念科技"}))

    # ---------- 标题归一化 ----------
    def test_normalize_title_folds_case_and_space(self):
        self.assertEqual(self.s.normalize_title("AI 产品经理"),
                         self.s.normalize_title("ai产品经理"))
        self.assertEqual(self.s.normalize_title("AI产品经理"),
                         self.s.normalize_title("AI  产品  经理"))
        self.assertNotEqual(self.s.normalize_title("AI产品经理"),
                            self.s.normalize_title("AI产品销售经理"))

    # ---------- 内容指纹与重复投放 ----------
    def test_same_jd_from_two_agencies_is_one_group(self):
        """同一份 JD 被两家猎头投放 → 归为一组（重复）。"""
        jd = "岗位职责：负责 AI 产品需求分析与设计，推动版本交付。" * 8
        jobs = [
            {"job_id": "a1", "jd": jd, "salary": "20-30K", "location": "杭州",
             "company_name": "某猎头A"},
            {"job_id": "b2", "jd": jd, "salary": "20-30K", "location": "杭州",
             "company_name": "某猎头B"},
            {"job_id": "c3", "jd": "完全不同的岗位内容" * 20, "salary": "10-15K",
             "location": "上海", "company_name": "某公司"},
        ]
        fingerprint_by_id, sizes = self.s.group_duplicates(jobs)
        self.assertEqual(len(fingerprint_by_id), 3)
        self.assertEqual(sizes[fingerprint_by_id["a1"]], 2)
        self.assertEqual(sizes[fingerprint_by_id["c3"]], 1)

    def test_fingerprint_ignores_whitespace_only_differences(self):
        a = {"job_id": "a", "jd": "岗位职责  负责 AI 产品\n需求分析", "salary": "10-20K",
             "location": "杭州"}
        b = {"job_id": "b", "jd": "岗位职责 负责 AI 产品需求分析", "salary": "10-20K",
             "location": "杭州"}
        self.assertEqual(self.s.content_fingerprint(a), self.s.content_fingerprint(b))

    # ---------- 供给编排 ----------
    def test_supply_plan_scales_with_budget(self):
        needed, expected = self.s.supply_plan(700, 260)
        self.assertEqual(needed, 3)          # ceil(700/260)
        self.assertGreaterEqual(expected, 700)
        self.assertEqual(self.s.supply_plan(0)[0], 0)

    def test_supply_gap_reports_shortfall(self):
        gap, combos = self.s.supply_gap(700, 239)
        self.assertEqual(gap, 461)
        self.assertEqual(combos, 2)           # ceil(461/260)
        self.assertEqual(self.s.supply_gap(700, 900)[0], 0)   # 供给充足

    # ---------- 导出不变量 ----------
    def _clean_job(self, jid="j1"):
        return {
            "job_id": jid, "title": "AI产品经理", "company_name": "闪念科技",
            "salary": "20-30K", "location": "杭州", "job_link": "https://x/a.html",
            # JD 随 jid 变化：否则会被内容指纹判成"同源重复投放"（那是对的）
            "jd": f"岗位职责：负责 AI 产品需求分析与设计，方向 {jid}。" * 12,
            "post_type": "direct", "title_key": "ai产品经理",
            "link_verified_at": "2026-10-22T02:00:00",
        }

    def test_invariants_pass_on_clean_data(self):
        jobs = [self._clean_job(f"j{i}") for i in range(3)]
        result = self.s.check_invariants(jobs)
        failures = self.s.invariant_failures(result)
        self.assertEqual(failures, [], f"干净数据不应有失败项：{failures}")

    def test_invariants_catch_junk_tail_and_missing_type(self):
        job = self._clean_job()
        job["jd"] = job["jd"] + "\n薪资详情\n社保类型：五险一金\n刚刚活跃"
        job.pop("post_type")
        result = self.s.check_invariants([job])
        self.assertFalse(result["no_junk_tail"]["ok"])
        self.assertFalse(result["post_type"]["ok"])
        failures = " ".join(self.s.invariant_failures(result))
        self.assertIn("no_junk_tail", failures)
        self.assertIn("post_type", failures)

    def test_invariants_catch_missing_fields_and_link_stamp(self):
        job = self._clean_job()
        job["salary"] = ""
        job.pop("link_verified_at")
        result = self.s.check_invariants([job])
        self.assertFalse(result["field_salary"]["ok"])
        self.assertFalse(result["link_age"]["ok"])


class LinkRefreshTests(unittest.TestCase):
    """链接是短时效凭证：必须能"取新"且能"刷新回来"。"""

    def setUp(self):
        self.module = load_module("boss_cdp_raw", ROOT / "scripts" / "boss_cdp_raw.py")

    def test_new_link_replaces_stale_link_for_same_job_id(self):
        jobs = [{"job_id": "a", "job_link": "https://x/OLD.html"}]
        fresh = [{"job_id": "a", "job_link": "https://x/NEW.html"}]
        changed = self.module._refresh_links_in_jobs(jobs, fresh)
        self.assertEqual(changed, 1)
        self.assertEqual(jobs[0]["job_link"], "https://x/NEW.html")
        self.assertTrue(jobs[0]["link_verified_at"])

    def test_refresh_links_in_export_updates_and_reports_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "export.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"jobs": [
                    {"job_id": "a", "job_link": "https://x/OLD.html"},
                    {"job_id": "b", "job_link": "https://x/OLD2.html"},
                ]}, handle)
            fresh = [{"job_id": "a", "job_link": "https://x/NEW.html"}]
            updated, missing = self.module.refresh_links_in_export(path, fresh)
            self.assertEqual(updated, 1)
            self.assertEqual(missing, 1)      # b 在新列表里查不到
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            self.assertEqual(data["jobs"][0]["job_link"], "https://x/NEW.html")
            self.assertEqual(data["links_missing_count"], 1)
            self.assertTrue(data["links_refreshed_at"])


if __name__ == "__main__":
    unittest.main()
