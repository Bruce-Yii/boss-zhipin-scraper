"""风险标记（用户视角：这岗该不该投）。

此前这些信号（代发占位名、无区县、异地 base、陈旧帖、兼职/创业营）只散在
报告里人工看，**没进产物** → 每次都要人肉再查一遍。现在算成一个字段。
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


class RiskFlagTests(unittest.TestCase):

    def setUp(self):
        self.model = load_module("job_model", SCRIPTS / "job_model.py")

    def _job(self, **over):
        job = {
            "title": "AI产品经理实习生",
            "company_name": "智谱华章",
            "location": "杭州·余杭区·仓前",
            "jd": "负责 AI 产品设计与需求分析，推动功能落地。",
            "page_update_date": "2026-10-20",
            "salary_max": 300,
        }
        job.update(over)
        return job

    def test_clean_job_has_no_flags(self):
        flags = self.model.risk_flags(self._job())
        self.assertEqual(flags, [], f"正常岗不该有风险标记：{flags}")

    def test_placeholder_company_name_flagged(self):
        flags = self.model.risk_flags(self._job(company_name="某大型互联网公司"))
        self.assertIn("agency_placeholder", flags)

    def test_missing_district_flagged(self):
        flags = self.model.risk_flags(self._job(location="杭州"))
        self.assertIn("no_district", flags)

    def test_other_base_flagged(self):
        flags = self.model.risk_flags(self._job(company_name="北京某某科技有限公司"))
        self.assertIn("other_base", flags)

    def test_stale_post_flagged(self):
        flags = self.model.risk_flags(self._job(page_update_date="2020-01-01"))
        self.assertIn("stale", flags)

    def test_part_time_flagged(self):
        flags = self.model.risk_flags(self._job(title="AI产品实习生（可兼职）"))
        self.assertIn("part_time", flags)

    def test_salary_outlier_flagged(self):
        flags = self.model.risk_flags(self._job(salary_max=3000),
                                      peer_salary_median=300)
        self.assertIn("salary_outlier", flags)

    def test_missing_update_date_is_not_stale(self):
        """平台没给更新时间 ≠ 陈旧 → 不能误杀。"""
        flags = self.model.risk_flags(self._job(page_update_date=""))
        self.assertNotIn("stale", flags)

    def test_flags_enter_the_record(self):
        record = self.model.JobRecord(self._job(company_name="某大型互联网公司"))
        record.apply_derived()
        self.assertIn("agency_placeholder", record.data["risk_flags"])

    def test_direction_covers_intern_product_titles(self):
        """「ai产品实习生」是 AI 产品岗（此前被判成 ai_other，少给用户 7 个真岗）。"""
        for title in ("ai产品实习生", "AI产品实习生", "AI产品助理实习生"):
            _is_ai, label, _evidence = self.model.classify_direction({"title": title})
            self.assertEqual(label, "ai_pm", f"{title} 应判为 ai_pm")
        for title in ("AI开发实习生", "AI数据标注实习生", "AI短剧编剧实习生"):
            _is_ai, label, _evidence = self.model.classify_direction({"title": title})
            self.assertEqual(label, "ai_other", f"{title} 不该判为 ai_pm")


if __name__ == "__main__":
    unittest.main()
