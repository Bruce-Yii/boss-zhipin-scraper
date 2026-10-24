"""CSV 随附文件验收（issue #171）：行数/job_id 对账、必需列、编码、self_check 接线。"""

import csv
import importlib.util
import json
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


JOBS = [
    {"job_id": f"j{i}", "title": f"AI产品经理{i}", "salary": "20-30K",
     "company_name": f"公司{i}", "location": "杭州",
     "job_link": f"https://x/{i}.html", "jd": "岗位职责：" + "描述。" * 40}
    for i in range(3)
]

CSV_COLS = ("job_id", "title", "salary", "company_name", "location", "job_link")


def _row(job):
    return {k: job.get(k, "") for k in CSV_COLS}


class CsvCompanionTests(unittest.TestCase):

    def setUp(self):
        self.acc = load_module("acceptance", SCRIPTS / "acceptance.py")

    def _write_json(self, directory, payload, name="jobs.json"):
        path = pathlib.Path(directory) / name
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        return str(path)

    def _write_csv(self, directory, rows, header=None, name="jobs.csv",
                   encoding="utf-8-sig"):
        path = pathlib.Path(directory) / name
        with open(path, "w", encoding=encoding, newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(header or CSV_COLS))
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
        return str(path)

    def test_missing_csv_is_not_a_defect(self):
        """CSV 是可选产物：缺失不算缺陷。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_json(tmp, {"jobs": JOBS})
            self.assertEqual(self.acc.check_csv_companion(path), [])

    def test_matching_csv_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_json(tmp, {"jobs": JOBS})
            self._write_csv(tmp, [_row(j) for j in JOBS])
            self.assertEqual(self.acc.check_csv_companion(path), [])

    def test_row_count_and_id_mismatch_are_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_json(tmp, {"jobs": JOBS})
            rows = [_row(JOBS[0]), dict(_row(JOBS[0]), job_id="ghost")]
            self._write_csv(tmp, rows)
            findings = self.acc.check_csv_companion(path)
            ids = {f["id"] for f in findings}
            self.assertIn("csv_row_count", ids)
            self.assertIn("csv_extra_rows", ids)
            self.assertIn("csv_missing_rows", ids)
            self.assertTrue(all(f["level"] == "error" for f in findings))

    def test_missing_required_columns_is_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_json(tmp, {"jobs": JOBS})
            self._write_csv(tmp, [{"job_id": j["job_id"]} for j in JOBS],
                            header=["job_id"])
            findings = self.acc.check_csv_companion(path)
            self.assertEqual(findings[0]["id"], "csv_missing_columns")
            self.assertEqual(findings[0]["level"], "error")

    def test_unreadable_csv_is_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_json(tmp, {"jobs": JOBS})
            (pathlib.Path(tmp) / "jobs.csv").write_bytes(b"\xff\xfe\x00broken")
            findings = self.acc.check_csv_companion(path)
            self.assertEqual([f["id"] for f in findings], ["csv_unreadable"])

    def test_empty_job_id_rows_are_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_json(tmp, {"jobs": JOBS})
            rows = [_row(j) for j in JOBS]
            rows.append({"job_id": "", "title": "空id", "salary": "",
                         "company_name": "", "location": "", "job_link": ""})
            self._write_csv(tmp, rows)
            findings = self.acc.check_csv_companion(path)
            self.assertIn("csv_empty_job_id", {f["id"] for f in findings})

    def test_duplicate_job_ids_are_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_json(tmp, {"jobs": JOBS})
            rows = [_row(j) for j in JOBS] + [_row(JOBS[0])]
            self._write_csv(tmp, rows)
            findings = self.acc.check_csv_companion(path)
            self.assertIn("csv_duplicate_ids", {f["id"] for f in findings})
            self.assertIn("csv_row_count", {f["id"] for f in findings})

    def test_detail_shape_uses_detail_columns(self):
        """详情档（裸列表 JSON）用详情必需列核对。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_json(tmp, JOBS, name="details.json")
            self._write_csv(tmp, [{"job_id": j["job_id"], "title": j["title"],
                                   "jd": j["jd"], "job_link": j["job_link"]}
                                  for j in JOBS],
                            header=["job_id", "title", "jd", "job_link"],
                            name="details.csv")
            self.assertEqual(self.acc.check_csv_companion(path), [])


class SelfCheckCsvWiringTests(unittest.TestCase):

    def test_self_check_fails_product_with_broken_csv(self):
        """self_check：随附 CSV 对不上 → error 并入产物结论。"""
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        ap = load_module("audit_pipeline", SCRIPTS / "audit_pipeline.py")
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "jobs.json"
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"jobs": JOBS}, handle, ensure_ascii=False)
            self._write_broken_csv(path.with_suffix(".csv"))
            ok, report = ap.self_check([str(path)], repo_root=str(ROOT))
        prod = report["products"][0]
        self.assertFalse(prod["ok"])
        self.assertIn("csv_missing_columns", {f["id"] for f in prod["findings"]})
        self.assertFalse(ok)

    @staticmethod
    def _write_broken_csv(path):
        with open(path, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["job_id"])
            writer.writeheader()
            writer.writerow({"job_id": "ghost"})


if __name__ == "__main__":
    unittest.main()
