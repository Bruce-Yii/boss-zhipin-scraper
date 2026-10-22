"""CSV 列由岗位模型派生（2026-10-22）。

为什么：CSV 是用户实际拿到的表。2026-10-22 之前，CSV 只有 11 列，语义层的
`post_type`/`active_state`/`dup_group` 等**只进了 JSON**，CSV 上等于没有——列清单手工维护，
加字段时必然漏。

现在：CSV 列 = 模型的公开字段（自动派生），模型加字段 → CSV 自动有；
同时守住红线：**平台内部 id（securityId/lid/encrypt_*）绝不进 CSV**。
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


class CsvColumnsFromModelTests(unittest.TestCase):

    def setUp(self):
        self.model = load_module("job_model", ROOT / "scripts" / "job_model.py")
        self.main = load_module("boss_cdp_raw", ROOT / "scripts" / "boss_cdp_raw.py")

    def test_csv_columns_contain_every_public_model_field(self):
        """模型的公开字段必须全部出现在 CSV 列里（漏一个就等于没交付）。"""
        columns = set(self.main.DETAIL_CSV_COLUMNS)
        missing = [name for name in self.model.EXPORT_PUBLIC_FIELDS
                   if name not in columns]
        self.assertEqual(missing, [], f"这些模型字段没进 CSV：{missing}")

    def test_csv_columns_never_contain_internal_ids(self):
        """**红线**：平台内部 id / 凭据绝不进 CSV。"""
        columns = set(self.main.DETAIL_CSV_COLUMNS)
        for name in self.model.EXPORT_HIDDEN_FIELDS:
            self.assertNotIn(name, columns, f"{name} 是内部字段，不能进 CSV")
        self.assertNotIn("security_id", columns)

    def test_csv_keeps_legacy_leading_columns(self):
        """历史列位保持不变（用户的表格不会因为升级而错位）。"""
        leading = self.main.DETAIL_CSV_COLUMNS[:11]
        self.assertEqual(leading, [
            "job_id", "title", "company", "salary", "salary_source", "location",
            "boss_active_status", "tags_list", "job_link", "skill_tags", "jd",
        ])

    def test_company_column_reads_the_model_field(self):
        """CSV 的 company 列取模型的 company_name（历史列名不变、取值正确）。"""
        self.assertEqual(self.main._DETAIL_CSV_VALUE_SOURCE.get("company"),
                         "company_name")

    def test_model_public_fields_exclude_hidden(self):
        hidden = set(self.model.EXPORT_HIDDEN_FIELDS)
        public = set(self.model.EXPORT_PUBLIC_FIELDS)
        self.assertFalse(hidden & public)
        self.assertTrue(hidden)


if __name__ == "__main__":
    unittest.main()
