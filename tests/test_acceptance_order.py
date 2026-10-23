"""验收顺序回归：验收必须看到落盘前的最终派生状态（issue #139）。

此前 attach_acceptance_result 跑在重复组打标之前，合并带陈旧 dup_size 的
旧产物时会误报 P13“组内条数不符”、P3“重复未标记”。现在验收挪到写盘前
最后一步，看到的就是即将落盘的同一份状态。

另有 P13 时效合并级回归（issue #144）：旧戳重合并后，P13 要在产物验收里
报 stale（验收器层面的旧戳用例见 c21，这里验证合并透传不断链）。
"""

import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _job(i, **over):
    base = {
        "job_id": f"j{i}",
        "title": f"AI产品经理{i}",
        "company_name": f"公司{i}",
        "salary": "20-30K",
        "location": "杭州",
        "job_link": f"https://x/{i}.html",
        # 陈旧标记：之前合并的旧产物自带的脏 dup 状态
        "dup_group": "dup-stale",
        "dup_size": 99,
        "is_primary": True,
    }
    base.update(over)
    return base


class AcceptanceOrderTests(unittest.TestCase):

    def setUp(self):
        self.ec = load_module("export_contract", SCRIPTS / "export_contract.py")

    def test_stale_dup_mark_does_not_cause_false_p13(self):
        """带陈旧 dup_size 的旧产物合并后，验收不误报组内条数不符。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "jobs.json")
            payload = {
                "keyword": "AI产品经理", "city": "杭州",
                "total": 2, "job_count": 2,
                "jobs": [_job(0), _job(1)],
            }
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
            jd = "负责AI产品规划与设计。" * 30
            details = [
                {"job_id": "j0", "jd": jd},
                {"job_id": "j1", "jd": jd},
            ]
            kept, _ = self.ec._merge_jd_into_export(
                path, details, keep_junk=True)
            self.assertEqual(kept, 2)
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            # 落盘的 dup_size 必须是重算后的 2，不是陈旧的 99
            sizes = {j["job_id"]: j.get("dup_size") for j in data["jobs"]}
            self.assertEqual(sizes, {"j0": 2, "j1": 2})
            # 验收看到的是最终状态：不报组内条数不符
            findings = (data.get("acceptance") or {}).get("findings") or []
            bad = [f for f in findings
                   if f.get("id") == "P13_identity_freshness_credentials"
                   and "组内实际条数" in str(f.get("detail"))]
            self.assertEqual(bad, [])

    def test_stale_stamp_survives_merge_and_is_reported(self):
        """旧戳重合并：P13 要在产物验收里报超 24 小时（issue #142 的预期）。"""
        old_stamp = (datetime.now(UTC) - timedelta(hours=30)).isoformat()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "jobs.json")
            payload = {
                "keyword": "AI产品经理", "city": "杭州",
                "total": 1, "job_count": 1,
                "jobs": [_job("s0", job_id="s0",
                               job_link="https://x/s0.html",
                               link_verified_at=old_stamp)],
            }
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
            jd = "负责AI产品规划与设计。" * 30
            details = [{"job_id": "s0", "jd": jd,
                        "link_verified_at": old_stamp}]
            kept, _ = self.ec._merge_jd_into_export(
                path, details, keep_junk=True)
            self.assertEqual(kept, 1)
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            findings = (data.get("acceptance") or {}).get("findings") or []
            stale = [f for f in findings
                     if f.get("id") == "P13_identity_freshness_credentials"
                     and "超过" in str(f.get("detail"))]
            self.assertEqual(len(stale), 1, f"旧戳重合并必须报 P13：{findings}")


if __name__ == "__main__":
    unittest.main()
