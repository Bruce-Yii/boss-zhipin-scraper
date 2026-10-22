"""保真核对器（用户验收标准："纸片上什么样，爬出来就应该是什么样"）。

关键点：
- verbatim 字段：我们的值必须与平台原文**逐字相同**；
- normalized 字段：按**声明过的形状**（拼接/过滤）取原文再比 → 不能用"我们规范化了"
  当借口藏差异；
- derived 字段：不与原文比，核对**推导输入在原文里存在** + **推导规则有登记**；
- 原文里没有的字段 → 记"无法核对"并写明原因，**不许算通过**。
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


def _raw_job(**over):
    raw = {
        "encryptJobId": "abcdef0123456789",
        "encryptBrandId": "brand123",
        "encryptBossId": "boss123",
        "jobName": "AI产品经理",
        "salaryDesc": "20-30K",
        "cityName": "杭州",
        "areaDistrict": "余杭区",
        "businessDistrict": "仓前",
        "brandName": "某科技",
        "bossTitle": "招聘负责人",
        "jobExperience": "3-5年",
        "jobDegree": "本科",
        "brandScaleName": "100-499人",
        "brandStageName": "A轮",
        "brandIndustry": "互联网",
        "proxyJob": 0,
        "proxyType": 0,
        "jobType": "",
        "jobValidStatus": 1,
        "anonymous": 0,
        "iconWord": "急",
        "iconFlagList": ["a", "b"],
        "jobLabels": ["5天/周", "3个月"],
        "welfareList": ["五险一金", "弹性工作"],
        "skills": ["B端产品"],
    }
    raw.update(over)
    return raw


def _records_for(raw, **over):
    """按模型从原文生成我们应有的记录（用来测"完全保真"的基线）。"""
    model = load_module("job_model", SCRIPTS / "job_model.py")
    record = model.JobRecord.from_list_payload(raw).to_dict()
    record.update(over)
    return record


class FidelityTests(unittest.TestCase):

    def setUp(self):
        self.fid = load_module("fidelity", SCRIPTS / "fidelity.py")
        self.model = load_module("job_model", SCRIPTS / "job_model.py")

    def _raw_payload(self, jobs):
        return {"zpData": {"jobList": jobs}}

    def test_fully_faithful_record_passes(self):
        raw = _raw_job()
        payload = {"jobs": [_records_for(raw)]}
        ok, report = self.fid.check_fidelity(self._raw_payload([raw]), payload)
        self.assertTrue(ok, f"完全保真应当通过：{report['mismatches'][:3]}")
        self.assertEqual(report["stats"]["mismatch"], 0)
        self.assertGreater(report["fidelity_rate"], 0.99)

    def test_modified_verbatim_field_is_caught(self):
        """平台原文是 20-30K，我们写成 50-60K → 必须被抓出来。"""
        raw = _raw_job()
        record = _records_for(raw)
        record["salary"] = "50-60K"
        ok, report = self.fid.check_fidelity(
            self._raw_payload([raw]), {"jobs": [record]})
        self.assertFalse(ok)
        fields = {m["field"] for m in report["mismatches"]}
        self.assertIn("salary", fields)

    def test_modified_company_name_is_caught(self):
        raw = _raw_job()
        record = _records_for(raw)
        record["company_name"] = "别家公司"
        ok, report = self.fid.check_fidelity(
            self._raw_payload([raw]), {"jobs": [record]})
        self.assertFalse(ok)
        self.assertIn("company_name", {m["field"] for m in report["mismatches"]})

    def test_normalized_field_is_compared_by_declared_shape(self):
        """tags 是 jobExperience+jobDegree 拼起来的 → 按声明的形状比，不是逐字段瞎比。"""
        raw = _raw_job()
        record = _records_for(raw)
        record["tags"] = "3-5年 | 不限"          # 我们多塞了一个"不限"（原文里被过滤掉了）
        ok, report = self.fid.check_fidelity(
            self._raw_payload([raw]), {"jobs": [record]})
        self.assertFalse(ok, "拼接结果与声明的形状不一致，必须被抓出来")
        self.assertIn("tags", {m["field"] for m in report["mismatches"]})

    def test_derived_field_checks_its_inputs_present(self):
        """派生字段不与原文比，但要核对推导输入在原文里存在。"""
        raw = _raw_job(jobType="4")
        record = _records_for(raw)
        ok, report = self.fid.check_fidelity(
            self._raw_payload([raw]), {"jobs": [record]})
        # 推导输入齐全 → 不算不一致
        self.assertNotIn("recruit_type", {m["field"] for m in report["mismatches"]})

    def test_missing_raw_is_not_counted_as_pass(self):
        """原文里找不到这条 → 记"无法核对"，不许算通过。"""
        raw = _raw_job()
        record = _records_for(raw)
        record["job_id"] = "不存在的id"
        ok, report = self.fid.check_fidelity(
            self._raw_payload([raw]), {"jobs": [record]})
        self.assertFalse(ok)
        self.assertGreaterEqual(report["no_check_count"], 1)

    def test_every_field_has_a_provenance_declaration(self):
        """所有字段都必须说清值怎么来的（说不清来路＝缺陷）。"""
        missing = [s.name for s in self.model.FIELD_SPECS if not s.provenance]
        self.assertEqual(missing, [], f"这些字段没有溯源声明：{missing}")

    def test_derived_rules_are_registered(self):
        """「规则可查」不能是空话：derived 字段的规则 id 必须在规则表里。"""
        for spec in self.model.FIELD_SPECS:
            if spec.provenance == "derived" and spec.derive_rule:
                self.assertIn(spec.derive_rule, self.model.DERIVE_RULES,
                              f"{spec.name} 的推导规则 {spec.derive_rule} 未登记")

    def test_cli_exit_codes(self):
        raw = _raw_job()
        record = _records_for(raw)
        record["salary"] = "999K"
        with tempfile.TemporaryDirectory() as tmp:
            raw_path = os.path.join(tmp, "raw.json")
            prod_path = os.path.join(tmp, "prod.json")
            with open(raw_path, "w", encoding="utf-8") as handle:
                json.dump(self._raw_payload([raw]), handle, ensure_ascii=False)
            with open(prod_path, "w", encoding="utf-8") as handle:
                json.dump({"jobs": [record]}, handle, ensure_ascii=False)
            code_bad = self.fid.main([raw_path, prod_path])
            self.assertEqual(code_bad, 1)
            good = _records_for(raw)
            with open(prod_path, "w", encoding="utf-8") as handle:
                json.dump({"jobs": [good]}, handle, ensure_ascii=False)
            self.assertEqual(self.fid.main([raw_path, prod_path]), 0)


if __name__ == "__main__":
    unittest.main()
