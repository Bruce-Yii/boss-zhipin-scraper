"""消费端 vendor validator 的边界回归测试。"""

import importlib.util
import json
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
VALIDATOR_PATH = ROOT / "tests" / "fixtures" / "consumer_validator" / "src" / "python" / "contract_check.py"
NEGATIVE_FIXTURE = ROOT / "tests" / "fixtures" / "invalid_export_wt2.json"
_spec = importlib.util.spec_from_file_location("consumer_contract_check", VALIDATOR_PATH)
validator = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(validator)


def valid_job(job_id="a"):
    return {
        "job_id": job_id,
        "title": "AI 产品经理",
        "location": "上海",
        "job_link": "https://www.zhipin.com/job_detail/a.html",
        "company_name": "某科技",
    }


class ContractValidatorTests(unittest.TestCase):
    def test_rejects_non_dict_top_level(self):
        report = validator.validate_export([])
        self.assertFalse(report["ok"])
        self.assertTrue(any("顶层" in error or "dict" in error for error in report["errors"]))

    def test_rejects_bool_and_float_format_versions(self):
        for value in (True, 1.0):
            with self.subTest(value=value):
                report = validator.validate_export({
                    "format_version": value,
                    "job_count": 0,
                    "jobs": [],
                })
                self.assertFalse(report["ok"])

    def test_rejects_non_dict_job_and_blank_required_fields(self):
        report = validator.validate_export({
            "format_version": 2,
            "job_count": 2,
            "jobs": ["bad", {**valid_job(), "title": "   "}],
        })
        self.assertFalse(report["ok"])
        self.assertTrue(any("对象" in error or "dict" in error for error in report["errors"]))
        self.assertTrue(any("title" in error for error in report["errors"]))

    def test_rejects_duplicate_job_ids(self):
        report = validator.validate_export({
            "format_version": 2,
            "job_count": 2,
            "jobs": [valid_job("same"), valid_job("same")],
        })
        self.assertFalse(report["ok"])
        self.assertTrue(any("重复" in error for error in report["errors"]))

    def test_rejects_wt2_credential_key(self):
        report = validator.validate_export({
            "format_version": 2,
            "job_count": 1,
            "jobs": [{**valid_job(), "wt2": "credential"}],
        })
        self.assertFalse(report["ok"])
        self.assertTrue(any("wt2" in error for error in report["errors"]))

    def test_negative_fixture_is_rejected(self):
        report = validator.validate_export(json.loads(
            NEGATIVE_FIXTURE.read_text(encoding="utf-8")))
        self.assertFalse(report["ok"])
        self.assertTrue(any("wt2" in error for error in report["errors"]))

    def test_allows_ordinary_token_text_but_rejects_structured_credentials(self):
        """validator 不应把 JD 普通文本中的 token 误报；结构化凭据字段必须拒绝。"""
        ordinary = validator.validate_export({
            "format_version": 2, "job_count": 1,
            "jobs": [{**valid_job(), "jd": "负责 token 化与模型评估"}],
        })
        self.assertTrue(ordinary["ok"], ordinary["errors"])
        for field, value in (
                ("encryptJobId", "E"), ("security_map", {"a": "S"}),
                ("secret", "S"), ("accessToken", "S"), ("lid", "L")):
            with self.subTest(field=field):
                report = validator.validate_export({
                    "format_version": 2, "job_count": 1,
                    "jobs": [{**valid_job(), field: value}],
                })
                self.assertFalse(report["ok"])
                self.assertTrue(any(field.lower() in error.lower()
                                    for error in report["errors"]), report["errors"])

    def test_rejects_sensitive_url_query_and_fragment(self):
        report = validator.validate_export({
            "format_version": 2, "job_count": 1,
            "jobs": [{**valid_job(), "job_link": (
                "https://x.test/a?securityId=query-secret#access_token=fragment-secret")}],
        })
        self.assertFalse(report["ok"])
        self.assertTrue(any("security" in error.lower()
                            for error in report["errors"]), report["errors"])


if __name__ == "__main__":
    unittest.main()
