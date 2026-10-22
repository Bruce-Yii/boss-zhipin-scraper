"""验收机制与零成本过滤的行为锁定（2026-10-22）。

这一组测试对应用户的三条追问：
1. **"怎么验收/体检"** → `scripts/acceptance.py` 按 `data/acceptance_spec.json` 判定；
   规格来自消费者的问题（不是来自实现），检查按**性质**而不是字段清单；
2. **"噪声能不能零成本丢掉"** → `--recruit-type` / `--industry-allow` 在**列表阶段**过滤，
   丢掉的岗不抓详情 → 不耗额度；
3. **"JD 与岗位名对不上"** → 与销售岗同级，从摘要里剔除。
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


def _record(**over):
    base = {
        "job_id": "j1", "title": "AI产品经理", "company_name": "闪念科技",
        "company_industry": "互联网", "company_scale": "100-499人",
        "company_stage": "A轮", "company_link": "https://x/c.html",
        "salary": "20-30K", "salary_source": "api", "location": "杭州·余杭区",
        "boss_active_status": "今日活跃", "active_state": "active",
        "job_valid_status": "1", "link_verified_at": "2026-10-22T10:00:00",
        "job_link": "https://x/a.html", "jd": "岗位职责：负责 AI 产品需求分析与设计。" * 10,
        "post_type": "direct", "recruit_type": "social", "title_key": "ai产品经理",
        "page_update_date": "2026-10-22", "skill_tags": ["B端产品"],
        "tags": "3-5年 | 本科", "experience": "3-5年", "education": "本科",
        "dup_group": "", "dup_size": 1, "is_primary": True,
        "proxy_job": "", "proxy_type": "", "boss_title": "HR", "job_type": "",
    }
    base.update(over)
    return base


class AcceptanceCheckerTests(unittest.TestCase):

    def setUp(self):
        self.acc = load_module("acceptance", SCRIPTS / "acceptance.py")
        self.model = load_module("job_model", SCRIPTS / "job_model.py")

    def test_clean_export_passes(self):
        ok, findings = self.acc.check_export({"jobs": [_record()]})
        self.assertTrue(ok, f"干净数据不该有 error：{findings}")

    def test_missing_required_field_fails(self):
        ok, findings = self.acc.check_export({"jobs": [_record(company_name="")]})
        self.assertFalse(ok)
        self.assertTrue(any(f["id"] == "P1_required_non_empty" for f in findings))

    def test_unflagged_duplicates_fail(self):
        """内容相同却没归进同一 dup_group → 失败（2026-10-22 实测 4 组真重复）。"""
        a = _record(job_id="a1")
        b = _record(job_id="b2")
        ok, findings = self.acc.check_export({"jobs": [a, b]})
        self.assertFalse(ok)
        self.assertTrue(any(f["id"] == "P3_duplicates_are_flagged" for f in findings))

    def test_flagged_duplicates_pass(self):
        a = _record(job_id="a1", dup_group="dup-1", dup_size=2, is_primary=True)
        b = _record(job_id="b2", dup_group="dup-1", dup_size=2, is_primary=False)
        ok, findings = self.acc.check_export({"jobs": [a, b]})
        self.assertFalse(any(f["id"] == "P3_duplicates_are_flagged" for f in findings))

    def test_link_shared_by_two_jobs_fails(self):
        a = _record(job_id="a1")
        b = _record(job_id="b2", job_link="https://x/a.html")
        ok, findings = self.acc.check_export({"jobs": [a, b]})
        self.assertFalse(ok)
        self.assertTrue(any(f["id"] == "P4_link_belongs_to_record" for f in findings))

    def test_jd_not_matching_title_is_flagged(self):
        job = _record(jd="看清楚再投简历，谢谢【招募小红书运营合伙人｜股权合作】我们正在做…")
        ok, findings = self.acc.check_export({"jobs": [job]})
        self.assertTrue(any(f["id"] == "P5_jd_matches_its_title" for f in findings))

    def test_short_jd_fails(self):
        ok, findings = self.acc.check_export({"jobs": [_record(jd="太短")]})
        self.assertFalse(ok)
        self.assertTrue(any(f["id"] == "P9_jd_not_too_short" for f in findings))

    def test_junk_tail_fails(self):
        job = _record(jd="岗位职责：负责 AI 产品需求分析与设计。" * 10 + "\n薪资详情\n刚刚活跃")
        ok, findings = self.acc.check_export({"jobs": [job]})
        self.assertFalse(ok)
        self.assertTrue(any(f["id"] == "P10_no_junk_tail" for f in findings))

    def test_forbidden_internal_fields_fail(self):
        job = _record(security_id="SID")
        ok, findings = self.acc.check_export({"jobs": [job]})
        self.assertFalse(ok)
        self.assertTrue(any(f["id"] == "P0_forbidden_fields" for f in findings))

    def test_spec_covers_consumer_questions(self):
        """规格必须逐条回答消费者的问题（这是验收的独立性来源）。"""
        spec = self.acc.load_spec()
        self.assertGreaterEqual(len(spec.get("questions") or []), 8)
        for question in spec.get("questions") or []:
            self.assertTrue(question.get("ask"), "每条问题要有原话")
            self.assertTrue(question.get("requires"), "每条问题要列出需要的字段")
        for prop in spec.get("properties") or []:
            self.assertTrue(prop.get("id") and prop.get("desc"))

    def test_fingerprint_is_single_definition(self):
        """指纹必须只有一处定义（验收器与打标器同源）。"""
        semantics = load_module("semantics", SCRIPTS / "semantics.py")
        job = _record()
        self.assertEqual(semantics.content_fingerprint(job),
                         self.model.content_fingerprint(job))


class RelevanceFilterTests(unittest.TestCase):

    def setUp(self):
        self.semantics = load_module("semantics", SCRIPTS / "semantics.py")
        self.model = load_module("job_model", SCRIPTS / "job_model.py")

    def test_recruit_type_filter_keeps_only_requested(self):
        jobs = [
            {"job_id": "1", "job_type": "4", "company_industry": "互联网"},
            {"job_id": "2", "job_type": "", "company_industry": "互联网"},
        ]
        kept, dropped = self.semantics.filter_by_relevance(
            jobs, recruit_types=["intern"], industry_allow=[])
        self.assertEqual([j["job_id"] for j in kept], ["1"])
        self.assertEqual(dropped["recruit_type"], 1)

    def test_industry_is_not_filtered_by_default(self):
        """**默认按行业不过滤**（实测：餐饮 4 条 100% 是产品经理岗，行业 ≠ 相关性）。"""
        jobs = [
            {"job_id": "1", "job_type": "", "company_industry": "互联网"},
            {"job_id": "2", "job_type": "", "company_industry": "餐饮"},
            {"job_id": "3", "job_type": "", "company_industry": "房地产中介/租赁"},
        ]
        kept, dropped = self.semantics.filter_by_relevance(jobs)
        self.assertEqual(len(kept), 3)
        self.assertEqual(dropped["industry_block"], 0)
        self.assertEqual(dropped["industry_allow"], 0)

    def test_industry_blocklist_drops_and_lists_what_was_dropped(self):
        """显式黑名单才丢，且**丢的是哪些要能核对**（不静默丢）。"""
        jobs = [
            {"job_id": "1", "title": "产品经理", "job_type": "",
             "company_industry": "互联网", "company_name": "A"},
            {"job_id": "2", "title": "产品经理", "job_type": "",
             "company_industry": "房地产中介/租赁", "company_name": "B"},
        ]
        kept, dropped = self.semantics.filter_by_relevance(
            jobs, industry_block=["房地产中介/租赁"])
        self.assertEqual([j["job_id"] for j in kept], ["1"])
        self.assertEqual(dropped["industry_block"], 1)
        self.assertEqual(len(dropped["items"]), 1)
        self.assertIn("房地产", dropped["items"][0]["reason"])
        self.assertEqual(dropped["items"][0]["title"], "产品经理")

    def test_industry_allowlist_drops_others_when_explicitly_given(self):
        jobs = [
            {"job_id": "1", "job_type": "", "company_industry": "互联网"},
            {"job_id": "2", "job_type": "", "company_industry": "餐饮"},
        ]
        kept, dropped = self.semantics.filter_by_relevance(
            jobs, industry_allow=["互联网"])
        self.assertEqual([j["job_id"] for j in kept], ["1"])
        self.assertEqual(dropped["industry_allow"], 1)

    def test_missing_industry_is_kept_and_counted(self):
        """行业缺失（平台没给）不静默丢，单独计数供人判断。"""
        jobs = [{"job_id": "3", "job_type": "", "company_industry": ""}]
        kept, dropped = self.semantics.filter_by_relevance(
            jobs, industry_allow=["互联网"])
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped["no_industry"], 1)

    def test_title_relevance_filter(self):
        """相关性判定看**标题**（产品经理/AI 方向），不看行业。"""
        jobs = [
            {"job_id": "1", "title": "AI产品经理", "job_type": "",
             "company_industry": "互联网"},
            {"job_id": "2", "title": "实习生", "job_type": "4",
             "company_industry": "互联网"},
            {"job_id": "3", "title": "AI招聘&猎头方向实习生", "job_type": "4",
             "company_industry": "互联网"},
        ]
        kept, dropped = self.semantics.filter_by_relevance(
            jobs, only_relevant_title=True)
        self.assertEqual([j["job_id"] for j in kept], ["1"])
        self.assertEqual(dropped["title_relevance"], 2)
        self.assertTrue(self.model.is_relevant_title({"title": "AI产品经理"}))
        self.assertFalse(self.model.is_relevant_title({"title": "实习生"}))


class JdTitleMatchTests(unittest.TestCase):

    def setUp(self):
        self.summary = load_module("job_summary", SCRIPTS / "job_summary.py")

    def test_matching_jd_returns_true(self):
        job = {"title": "AI产品经理", "jd": "负责 AI 产品需求分析与设计，推动版本交付。"}
        self.assertTrue(self.summary.jd_matches_title(job))

    def test_unrelated_jd_returns_false(self):
        job = {"title": "产品经理", "jd": "看清楚再投简历，招募小红书运营合伙人（股权合作）。"}
        self.assertFalse(self.summary.jd_matches_title(job))

    def test_empty_inputs_are_not_flagged(self):
        self.assertTrue(self.summary.jd_matches_title({"title": "", "jd": ""}))
        self.assertTrue(self.summary.jd_matches_title({"title": "产品经理", "jd": ""}))


if __name__ == "__main__":
    unittest.main()
