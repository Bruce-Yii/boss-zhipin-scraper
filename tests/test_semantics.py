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
import pathlib
import sys
import unittest
from unittest import mock

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


class FourFixesTests(unittest.TestCase):
    """四项根因修法的行为锁定（2026-10-22）。"""

    def setUp(self):
        self.s = load_module("semantics", ROOT / "scripts" / "semantics.py")

    # ---------- 招聘类型（平台的 jobType）----------
    def test_classify_recruit_type_uses_platform_job_type(self):
        """平台 jobType 的含义是**实证**出来的，不是猜的：

        - jobType=4 → 实习岗：294 条实测里 24/24 是日薪制、24/24 经验要求为空、
          17/24 标题含"实习"
        - jobType=5 → 校招岗：**11/11 经验要求 = "在校/应届"**
        - jobType 空 → 社招岗：259 条全是月薪制、经验要求有值
        """
        self.assertEqual(self.s.classify_recruit_type({"job_type": "4"}), "intern")
        self.assertEqual(self.s.classify_recruit_type({"job_type": "5"}), "campus")
        self.assertEqual(self.s.classify_recruit_type({"job_type": ""}), "social")
        self.assertEqual(self.s.classify_recruit_type({}), "social")
        self.assertEqual(self.s.classify_recruit_type(None), "social")

    # ---------- ① 失败指纹归类 ----------
    def test_classify_extract_failure_fingerprints(self):
        """抽取失败按**页面特征**归类，而不是笼统的 invalid_detail。"""
        self.assertEqual(
            self.s.classify_extract_failure(
                "job description too short after validation: 99 < 120"), "too_short")
        self.assertEqual(
            self.s.classify_extract_failure("职位描述"), "no_section")
        self.assertEqual(
            self.s.classify_extract_failure("", "BOSS正在加载中"), "shell_only")
        self.assertEqual(
            self.s.classify_extract_failure("", "登录查看完整内容"), "login_wall")
        self.assertEqual(
            self.s.classify_extract_failure("", "访问受限 您的账户存在异常行为"),
            "risk_page")
        self.assertEqual(self.s.classify_extract_failure("随便什么"), "unknown")

    # ---------- ② 重复组标记 ----------
    def test_mark_duplicate_groups_flags_same_content(self):
        """平台允许同一岗位多条发布（job_id 不同）→ 内容指纹标出重复组。"""
        jd = "岗位职责：负责 AI 产品需求分析与设计。" * 10
        jobs = [
            {"job_id": "a1", "jd": jd, "salary": "20-30K", "location": "杭州"},
            {"job_id": "b2", "jd": jd, "salary": "20-30K", "location": "杭州"},
            {"job_id": "c3", "jd": "别的岗位内容" * 30, "salary": "10-15K",
             "location": "上海"},
        ]
        self.s.mark_duplicate_groups(jobs)
        self.assertEqual(jobs[0]["dup_group"], jobs[1]["dup_group"])
        self.assertEqual(jobs[0]["dup_size"], 2)
        self.assertTrue(jobs[0]["is_primary"])
        self.assertFalse(jobs[1]["is_primary"])
        self.assertEqual(jobs[2]["dup_group"], "")
        self.assertTrue(jobs[2]["is_primary"])

    # ---------- ③ 标题归一化分组 ----------
    def test_title_groups_merge_case_variants(self):
        counts = self.s.title_group_counts([
            {"title": "AI产品经理"}, {"title": "ai产品经理"},
            {"title": "AI 产品经理"}, {"title": "AI产品经理"},
        ])
        self.assertEqual(counts, {"ai产品经理": 4})

    # ---------- ④ 活跃度三态 ----------
    def test_active_state_distinguishes_unknown_from_inactive(self):
        """**"平台没给"不等于"不活跃"**（实测 14 条两边都没有活跃标签）。"""
        self.assertEqual(self.s.active_state(""), "unknown")
        self.assertEqual(self.s.active_state("刚刚活跃"), "active")
        self.assertEqual(self.s.active_state("在线"), "active")
        self.assertEqual(self.s.active_state("半年前活跃"), "inactive")


class KnownBadPageSkipTests(unittest.TestCase):
    """① 已知坏页：命中台账 → 跳过、**不消耗额度**、不发请求。"""

    def setUp(self):
        self.module = load_module("boss_cdp_raw", ROOT / "scripts" / "boss_cdp_raw.py")

    def test_known_bad_page_is_skipped_without_request(self):
        module = self.module
        ws = mock.Mock()
        with mock.patch.object(module._known_failures, "is_known", return_value=True):
            result = module._scrape_one_detail_via_html(
                {"job_id": "j1", "title": "AI产品经理",
                 "job_link": "https://www.zhipin.com/job_detail/x.html"}, ws, "s")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "known_bad_page")
        ws.eval_js_async.assert_not_called()

    def test_invalid_detail_is_recorded_into_ledger(self):
        """抽取失败 → 沉淀 job_id + 失败指纹（供次日跳过）。"""
        module = self.module
        recorded = {}

        def fake_record(job_id, fingerprint, reason):
            recorded["job_id"] = job_id
            recorded["fingerprint"] = fingerprint
            recorded["reason"] = reason

        with mock.patch.object(module._known_failures, "record",
                               side_effect=fake_record):
            module._record_known_bad_page("j9", "职位描述")
        self.assertEqual(recorded["job_id"], "j9")
        self.assertEqual(recorded["fingerprint"], "no_section")


if __name__ == "__main__":
    unittest.main()
