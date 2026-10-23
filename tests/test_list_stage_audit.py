"""列表层体检 + 方向判定 + 薪资拆列 + 指纹（独立审计驱动的第一批修复）。

对应 issue #120：
- 列表层体检（问题要在便宜的一层暴露）
- 平台「求职期望」污染要读出来并报
- 相关度过滤默认不过滤行业、丢掉要逐条列出
- 方向判定（AI 产品经理 vs 纯行业产品经理 vs 招聘实习）
- 薪资拆列（Excel 可排序）
- 内容指纹不含薪资（同 JD 薪资带微调仍判同源）
- 验收器对无 JD 的列表产物不误报
"""

import importlib.util
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


def _row(title, industry="互联网", job_type="", company="某公司", skills=None):
    return {
        "job_id": title + company, "title": title, "company_name": company,
        "company_industry": industry, "job_type": job_type,
        "skill_tags": skills or [], "salary": "20-30K",
    }


class ListStageAuditTests(unittest.TestCase):

    def setUp(self):
        self.semantics = load_module("semantics", SCRIPTS / "semantics.py")
        self.model = load_module("job_model", SCRIPTS / "job_model.py")

    def test_direction_distribution_and_keyword_hit_rate(self):
        jobs = [
            _row("AI产品经理", company="A"),
            _row("AIGC产品经理", company="B"),
            _row("产品经理", industry="餐饮", company="C"),
            _row("实习生", industry="房地产中介/租赁", company="D"),
        ]
        audit = self.semantics.list_stage_audit(jobs, keywords=["AI产品经理", "AIGC产品经理"])
        self.assertEqual(audit["total"], 4)
        self.assertEqual(audit["direction_counts"].get("ai_pm"), 2)
        self.assertEqual(audit["direction_counts"].get("pm_only"), 1)
        self.assertEqual(audit["keyword_hits"], 2)
        self.assertAlmostEqual(audit["keyword_hit_rate"], 0.5)
        self.assertTrue(audit["warnings"], "命中率低于 60% 必须给提示")

    def test_expectation_pollution_is_reported(self):
        """平台求职期望=房地产中介 → 必须提示（用户 2026-10-22 反馈的污染源）。"""
        jobs = [_row("AI产品经理", company="A")]
        audit = self.semantics.list_stage_audit(
            jobs, keywords=["AI产品经理"], job_expectation="房地产中介")
        self.assertEqual(audit["job_expectation"], "房地产中介")
        self.assertTrue(any("求职期望" in w for w in audit["warnings"]),
                        f"求职期望污染必须提示：{audit['warnings']}")

    def test_no_expectation_warning_when_aligned(self):
        jobs = [_row("AI产品经理", company="A")]
        audit = self.semantics.list_stage_audit(
            jobs, keywords=["AI产品经理"], job_expectation="AI产品经理")
        self.assertFalse(any("求职期望" in w for w in audit["warnings"]))

    def test_empty_list_returns_zero_audit(self):
        audit = self.semantics.list_stage_audit([], keywords=["x"])
        self.assertEqual(audit["total"], 0)
        self.assertIsNone(audit["keyword_hit_rate"])


class DirectionTests(unittest.TestCase):

    def setUp(self):
        self.model = load_module("job_model", SCRIPTS / "job_model.py")

    def test_ai_product_manager_is_target(self):
        is_ai_pm, label, evidence = self.model.classify_direction(
            {"title": "AI产品经理", "jd": "负责 AI 产品规划"})
        self.assertTrue(is_ai_pm)
        self.assertEqual(label, "ai_pm")
        self.assertTrue(evidence)

    def test_plain_product_manager_is_not_ai(self):
        is_ai_pm, label, _ = self.model.classify_direction(
            {"title": "产品经理", "jd": "负责餐饮产品研发与创新"})
        self.assertFalse(is_ai_pm)
        self.assertEqual(label, "pm_only")

    def test_recruiting_intern_is_not_target(self):
        """「AI招聘&猎头方向实习生」→ AI 只是修饰词，不是产品岗。"""
        is_ai_pm, label, _ = self.model.classify_direction(
            {"title": "AI招聘&猎头方向实习生", "jd": "参与招聘项目与人才寻访"})
        self.assertFalse(is_ai_pm)
        self.assertNotEqual(label, "ai_pm")
        self.assertFalse(self.model.is_relevant_title(
            {"title": "AI招聘&猎头方向实习生"}))
        self.assertTrue(self.model.is_relevant_title({"title": "AI产品经理"}))

    def test_direction_uses_jd_and_skills(self):
        is_ai_pm, label, _ = self.model.classify_direction(
            {"title": "产品经理", "jd": "负责大模型应用产品设计"})
        self.assertTrue(is_ai_pm)
        self.assertEqual(label, "ai_pm")

    def test_zhi_li_is_not_direction_signal(self):
        """"治理"不是方向词（用户 2026-10-23：从未认可，已移除）。
        只有"治理"撑着的标题不得判 ai_pm；真 AI 产品岗不受影响。"""
        is_ai_pm, label, _ = self.model.classify_direction(
            {"title": "AI治理专员", "jd": "负责数据治理规范"})
        self.assertFalse(is_ai_pm)
        self.assertNotEqual(label, "ai_pm")
        is_ai_pm2, label2, _ = self.model.classify_direction(
            {"title": "AI产品经理", "jd": "负责 AI 产品规划"})
        self.assertTrue(is_ai_pm2)
        self.assertEqual(label2, "ai_pm")


class SalaryPartsTests(unittest.TestCase):

    def setUp(self):
        self.model = load_module("job_model", SCRIPTS / "job_model.py")

    def test_monthly_salary(self):
        self.assertEqual(self.model.parse_salary_parts("15-25K"),
                         ("15000", "25000", "month", ""))

    def test_monthly_salary_with_months(self):
        self.assertEqual(self.model.parse_salary_parts("150-200K·16薪"),
                         ("150000", "200000", "month", "16"))

    def test_daily_salary(self):
        self.assertEqual(self.model.parse_salary_parts("150-200元/天"),
                         ("150", "200", "day", ""))

    def test_negotiable(self):
        self.assertEqual(self.model.parse_salary_parts("面议"), ("", "", "", ""))


class FingerprintTests(unittest.TestCase):

    def setUp(self):
        self.model = load_module("job_model", SCRIPTS / "job_model.py")

    def test_same_jd_different_salary_is_same_fingerprint(self):
        """同一份 JD、薪资带被微调 → 仍判同源（独立审计实测漏标 10 条）。"""
        a = {"jd": "岗位职责：负责智能体平台的产品规划与设计。" * 8,
             "salary": "11-20K", "location": "杭州·余杭区·仓前"}
        b = {"jd": "岗位职责：负责智能体平台的产品规划与设计。" * 8,
             "salary": "12-20K", "location": "杭州·余杭区·仓前"}
        self.assertEqual(self.model.content_fingerprint(a),
                         self.model.content_fingerprint(b))

    def test_different_jd_is_different_fingerprint(self):
        a = {"jd": "A" * 400, "salary": "20-30K", "location": "杭州"}
        b = {"jd": "B" * 400, "salary": "20-30K", "location": "杭州"}
        self.assertNotEqual(self.model.content_fingerprint(a),
                            self.model.content_fingerprint(b))


class AcceptanceListProductTests(unittest.TestCase):

    def setUp(self):
        self.acc = load_module("acceptance", SCRIPTS / "acceptance.py")

    def test_list_product_without_jd_is_not_flagged_as_duplicate(self):
        """无 JD 的列表产物：不能因为"空指纹"把所有人归成一组（审计 F5）。"""
        payload = {"jobs": [
            {"job_id": "a1", "title": "AI产品经理", "company_name": "A",
             "salary": "20-30K", "location": "杭州", "job_link": "https://x/a.html",
             "jd": "", "post_type": "direct", "recruit_type": "social",
             "active_state": "active", "title_key": "ai产品经理",
             "link_verified_at": "2026-10-22T10:00:00"},
            {"job_id": "b2", "title": "产品经理", "company_name": "B",
             "salary": "30-40K", "location": "杭州", "job_link": "https://x/b.html",
             "jd": "", "post_type": "direct", "recruit_type": "social",
             "active_state": "active", "title_key": "产品经理",
             "link_verified_at": "2026-10-22T10:00:00"},
        ]}
        ok, findings = self.acc.check_export(payload)
        dup = [f for f in findings if f["id"] == "P3_duplicates_are_flagged"]
        self.assertFalse(dup, f"无 JD 的不同岗位不该被判重复：{dup}")


class OrphanTargetRegistryTests(unittest.TestCase):

    def setUp(self):
        self.main = load_module("boss_cdp_raw", SCRIPTS / "boss_cdp_raw.py")

    def test_recorded_target_appends_to_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = os.path.join(tmp, "open_targets.jsonl")
            original = self.main.OPEN_TARGETS_LOG
            self.main.OPEN_TARGETS_LOG = log_path
            try:
                self.main.record_opened_target("TID-1")
                self.main.record_opened_target("TID-2")
                with open(log_path, encoding="utf-8") as handle:
                    lines = [line for line in handle.read().splitlines() if line.strip()]
                self.assertEqual(len(lines), 2)
                self.assertIn("TID-1", lines[0])
            finally:
                self.main.OPEN_TARGETS_LOG = original


if __name__ == "__main__":
    unittest.main()
