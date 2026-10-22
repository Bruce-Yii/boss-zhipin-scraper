"""详情记录必须与导出**同一套 schema**（2026-10-22 修的双轨）。

此前详情记录（`--detail-output`）是另一套手写 schema：只 23 个字段，缺 `company_name`
等 16 个列表侧字段 →

- 详情输出**不满足导出契约**（契约要求 company_name，详情里只有 company）；
- CSV 是从详情记录写的 → 46 列里 **32 列整列全空**（用户拿到的表三分之二是空的）；
- 详情输出与导出两套字段集（双轨）。

现在详情记录由岗位模型统一产生。
"""

import importlib.util
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class DetailRecordContractTests(unittest.TestCase):

    def setUp(self):
        self.model = load_module("job_model", ROOT / "scripts" / "job_model.py")
        self.main = load_module("boss_cdp_raw", ROOT / "scripts" / "boss_cdp_raw.py")
        self.contract = load_module("export_contract", ROOT / "scripts" / "export_contract.py")
        self.job = {
            "job_id": "j1", "title": "AI产品经理", "company_name": "闪念科技",
            "boss_name": "闪念科技", "salary": "20-30K", "salary_source": "api",
            "location": "杭州·余杭区·仓前", "boss_active_status": "在线",
            "tags": "3-5年 | 本科", "job_link": "https://x/a.html",
            "proxy_job": "1", "proxy_type": "", "job_type": "4",
            "boss_title": "猎头顾问", "company_scale": "100-499人",
            "company_stage": "A轮", "company_industry": "互联网",
            "welfare": "五险一金", "skills": ["RAG"], "icon_flags": "急",
            "job_valid_status": "1", "anonymous": "0", "company_link": "https://x/c.html",
            "job_labels": "五险一金 · 弹性工作",
            "security_id": "SID", "lid": "LID", "encrypt_job_id": "EJ",
            "encrypt_boss_id": "EB", "encrypt_brand_id": "EBD",
        }
        self.extracted = {
            "jd": "岗位职责正文" * 20, "tags": ["B端产品"],
            "page_update_date": "2026-10-22", "boss_active_status": "刚刚活跃",
        }

    def _record(self):
        return self.main.build_detail_record(self.job, self.extracted)

    def test_detail_record_satisfies_export_contract(self):
        """详情记录必须满足导出契约的必填字段（此前缺 company_name → 不满足）。"""
        record = self._record()
        missing = [f for f in self.contract._CONTRACT_REQUIRED_FIELDS
                   if not str(record.get(f) or "").strip()]
        self.assertEqual(missing, [], f"详情记录缺契约必填字段：{missing}")

    def test_detail_record_carries_list_side_fields(self):
        """列表侧字段必须在详情记录里（此前全缺 → CSV 32 列空）。"""
        record = self._record()
        for field in ("company_name", "boss_title", "proxy_job", "job_type",
                      "company_scale", "company_stage", "company_industry",
                      "welfare", "skills", "icon_flags", "job_valid_status",
                      "company_link", "anonymous", "job_labels"):
            self.assertIn(field, record, f"详情记录缺列表侧字段 {field}")

    def test_detail_record_never_carries_internal_ids(self):
        """红线：平台内部 id/凭据不进详情输出。"""
        record = self._record()
        for field in self.model.EXPORT_HIDDEN_FIELDS:
            self.assertNotIn(field, record, f"{field} 不能出现在详情记录里")

    def test_detail_record_has_derived_semantics(self):
        record = self._record()
        self.assertEqual(record["post_type"], "agency")
        self.assertEqual(record["recruit_type"], "intern")
        self.assertEqual(record["active_state"], "active")
        self.assertEqual(record["title_key"], "ai产品经理")
        self.assertTrue(record["link_verified_at"])

    def test_detail_record_keeps_legacy_aliases(self):
        """历史列位不变（company / tags_list / link 仍在，值取模型字段）。"""
        record = self._record()
        self.assertEqual(record["company"], "闪念科技")
        self.assertEqual(record["tags_list"], "3-5年 | 本科")
        self.assertEqual(record["link"], "https://x/a.html")

    def test_detail_record_active_status_prefers_detail_side(self):
        """活跃度取更精细的一侧（详情页「刚刚活跃」优先于列表「在线」）。"""
        record = self._record()
        self.assertEqual(record["boss_active_status"], "刚刚活跃")


if __name__ == "__main__":
    unittest.main()
