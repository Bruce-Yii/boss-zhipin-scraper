"""原始响应落盘 + 保真核对的回归护栏。

2026-08-22 独立保真核对（第一次真实跑）当场抓出两个真缺陷：
- ``location``：平台只给 cityName 时拼出 "杭州··"（两个空段 → 两个多余分隔符）；
- ``job_type``：平台给 "0"（社招），我们存 ""（没声明的规范化 → 保真核对报不一致）。

这两个用例把当时的缺陷钉住。
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


def _raw(**over):
    raw = {
        "encryptJobId": "abcdef0123456789",
        "encryptBrandId": "brand123",
        "encryptBossId": "boss123",
        "securityId": "SECRET-SHOULD-NOT-PERSIST",
        "jobName": "AI产品经理",
        "salaryDesc": "20-30K",
        "cityName": "杭州",
        "brandName": "某科技",
        "jobType": "0",
        "proxyJob": 0,
        "proxyType": 0,
    }
    raw.update(over)
    return raw


class RawCaptureTests(unittest.TestCase):

    def setUp(self):
        self.model = load_module("job_model", SCRIPTS / "job_model.py")
        self.contract = load_module("export_contract", SCRIPTS / "export_contract.py")

    def test_location_has_no_empty_segments(self):
        """平台只给 cityName 时，location 不应是 "杭州··"。"""
        record = self.model.JobRecord.from_list_payload(_raw()).to_dict()
        self.assertEqual(record["location"], "杭州")
        self.assertNotIn("··", record["location"])

    def test_location_keeps_all_present_parts(self):
        record = self.model.JobRecord.from_list_payload(
            _raw(cityName="杭州", areaDistrict="余杭区", businessDistrict="仓前")
        ).to_dict()
        self.assertEqual(record["location"], "杭州·余杭区·仓前")

    def test_raw_sanitizer_keeps_job_identity_drops_credentials(self):
        """原始响应落盘：保留岗位身份（保真核对要靠它对账），剥掉凭据与 HR 身份。"""
        cleaned = self.contract._sanitize_raw_payload({"zpData": {"jobList": [_raw()]}})
        entry = cleaned["zpData"]["jobList"][0]
        self.assertEqual(entry["encryptJobId"], "abcdef0123456789")
        self.assertNotIn("securityId", entry)
        self.assertNotIn("encryptBossId", entry)

    def test_export_sanitizer_still_drops_internal_ids(self):
        """导出产物的规则不变（内部标识不进产物）——两条规则不能混。"""
        cleaned = self.contract._sanitize_payload({"job": _raw()})
        self.assertNotIn("encryptJobId", cleaned["job"])
        self.assertNotIn("securityId", cleaned["job"])

    def test_job_type_is_verbatim(self):
        """平台 jobType=0（社招）模型原样存 "0" → 声明必须是 verbatim（逐字一致）。"""
        spec = {s.name: s for s in self.model.FIELD_SPECS}["job_type"]
        self.assertEqual(spec.provenance, "verbatim")

    def test_proxy_job_zero_is_declared_normalization(self):
        """平台 proxyJob=0 → 我们存空串：这是**声明过的**规范化（不是逐字抄）。"""
        spec = {s.name: s for s in self.model.FIELD_SPECS}["proxy_job"]
        self.assertEqual(spec.provenance, "normalized")
        self.assertEqual(spec.raw_shape, "zero_as_empty")

    def test_fidelity_reports_zero_mismatch_on_a_faithful_capture(self):
        """端到端：原文 → 记录 → 保真核对，0 处不一致。"""
        fid = load_module("fidelity", SCRIPTS / "fidelity.py")
        raw = _raw()
        record = self.model.JobRecord.from_list_payload(raw).to_dict()
        payload = {"zpData": {"jobList": [raw]}}
        ok, report = fid.check_fidelity(payload, {"jobs": [record]})
        self.assertTrue(ok, f"应当保真：{report['mismatches'][:3]}")
        self.assertEqual(report["stats"]["mismatch"], 0)

    def test_fidelity_catches_location_segment_defect(self):
        """回归：把 location 改回 "杭州··"，保真核对必须报不一致。"""
        fid = load_module("fidelity", SCRIPTS / "fidelity.py")
        raw = _raw()
        record = self.model.JobRecord.from_list_payload(raw).to_dict()
        record["location"] = "杭州··"
        ok, report = fid.check_fidelity({"zpData": {"jobList": [raw]}},
                                        {"jobs": [record]})
        self.assertFalse(ok)
        self.assertIn("location", {m["field"] for m in report["mismatches"]})

    def test_saved_raw_file_contains_no_credentials(self):
        """落盘文件里不能出现 securityId / HR 身份。"""
        main = load_module("boss_cdp_raw", SCRIPTS / "boss_cdp_raw.py")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "raw.json")
            payload = {"run_id": "x" * 8, "keyword": "k", "city": "杭州",
                       "list_pages": [{"page": 1, "response": {"zpData": {"jobList": [_raw()]}}}]}
            cleaned = main._sanitize_raw_payload(payload)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(cleaned, handle, ensure_ascii=False)
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
            self.assertNotIn("SECRET-SHOULD-NOT-PERSIST", text)
            self.assertNotIn("boss123", text)
            self.assertIn("abcdef0123456789", text)


if __name__ == "__main__":
    unittest.main()
