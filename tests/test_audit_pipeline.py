"""验收流水线的行为锁定（2026-10-22）。

这个模块是**根因层**的东西：把「需求 → 验收性质 → 反例 → 独立执行 → 结论留痕」
变成可重复执行的流程，而不是每次靠临场写检查脚本。

对应：
- `data/audit_requirements.json`：需求的独立来源（使用者的问题）
- `data/acceptance_spec.json`：验收性质
- `tests/test_acceptance_counterexamples.py`：反例（性质没反例 = 没写）
- `data/audit_briefs/*.md`：标准化的独立审查 brief
- `scripts/audit_pipeline.py`：把它们串起来并报缺口
"""

import importlib.util
import json
import os
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys_modules = __import__("sys").modules
    sys_modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class AuditPipelineTests(unittest.TestCase):

    def setUp(self):
        self.pipeline = load_module("audit_pipeline", SCRIPTS / "audit_pipeline.py")

    def test_requirements_come_from_user_questions(self):
        requirements = self.pipeline.load_requirements()
        reqs = requirements.get("requirements") or []
        self.assertGreaterEqual(len(reqs), 8)
        for req in reqs:
            self.assertTrue(req.get("id"))
            self.assertTrue(req.get("ask"), "每条需求要写清楚使用者要问什么")
            self.assertTrue(req.get("question"), "每条需求要挂到规格里的一条 question")

    def test_every_requirement_maps_to_properties(self):
        """每条需求都要挂上验收性质；**性质必须在规格里存在**——不存在就是缺口，
        缺口由矩阵报出来（见下一个测试），不能被静默吞掉。"""
        rows, gaps = self.pipeline.coverage_matrix()
        for row in rows:
            self.assertTrue(row["properties"],
                            f"{row['id']} 没有挂任何验收性质（需求没人管）")
        # 规格里缺失的性质必须在缺口清单里出现（不能只写在矩阵行里）
        missing = [p for row in rows for p in row["missing_in_spec"]]
        for prop in set(missing):
            self.assertTrue(any(prop in gap for gap in gaps),
                            f"{prop} 在规格里不存在，但没有进缺口清单")

    def test_gaps_are_reported_not_hidden(self):
        rows, gaps = self.pipeline.coverage_matrix()
        for row in rows:
            for prop in row["without_counterexample"]:
                self.assertTrue(
                    any(prop in gap for gap in gaps),
                    f"{prop} 没有反例，必须作为缺口报出来")

    def test_coverage_matrix_reports_gaps(self):
        rows, gaps = self.pipeline.coverage_matrix()
        self.assertIsInstance(rows, list)
        self.assertIsInstance(gaps, list)
        # 矩阵必须能指出"没有反例"或"孤儿性质"这类缺口——
        # 它是"没反例的性质等于没写"的守卫。
        for gap in gaps:
            self.assertIsInstance(gap, str)

    def test_audit_briefs_exist_and_have_replay_section(self):
        requirements = self.pipeline.load_requirements()
        kinds = requirements.get("audit_kinds") or []
        self.assertGreaterEqual(len(kinds), 4)
        for kind in kinds:
            path = ROOT / kind["brief"]
            self.assertTrue(path.exists(), f"缺少审查 brief：{kind['brief']}")
            text = path.read_text(encoding="utf-8")
            self.assertIn("复盘", text,
                          f"{kind['id']} 的 brief 必须带复盘段（审查方式本身要能改进）")
            self.assertTrue(kind.get("question"), "每个审查类型要写清它回答什么问题")

    def test_self_check_reports_missing_counterexample_suite(self):
        ok, report = self.pipeline.self_check([])
        self.assertIn("requirements", report)
        self.assertIn("coverage_gaps", report)
        # 没有反例套件时不能悄悄当成"通过"
        self.assertIsNotNone(report["counterexample_suite_ok"])
        if report["counterexample_suite_ok"] is False:
            self.assertFalse(ok)

    def test_self_check_on_broken_product_fails(self):
        payload = {"jobs": [{"job_id": "x", "title": "", "company_name": "",
                             "salary": "", "location": "", "job_link": "",
                             "jd": ""}]}
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "broken.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
            ok, report = self.pipeline.self_check([path])
        self.assertFalse(ok, "必填全空的产物必须判不通过")


if __name__ == "__main__":
    unittest.main()
