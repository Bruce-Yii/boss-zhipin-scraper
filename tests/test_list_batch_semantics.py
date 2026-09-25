"""列表停页、风控广播、状态统计与 batch 参数语义回归测试。"""

import importlib.util
import io
import json
import pathlib
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "boss_cdp_raw.py"


def load_module():
    sys.modules.setdefault("websocket", mock.Mock())
    sys.modules.setdefault("requests", mock.Mock())
    spec = importlib.util.spec_from_file_location("boss_list_batch", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def page_jobs(page, count=30):
    return [{
        "title": f"AI岗位{page}-{i}",
        "job_link": f"https://example.com/job/{page}-{i}",
        "boss_name": "公司",
    } for i in range(count)]


class ListBatchSemanticsTests(unittest.TestCase):
    def _list_mocks(self, module, pages, cdp=None):
        if cdp is None:
            cdp = mock.Mock()

        def eval_js(script, sid=None):
            if "xhr.open" not in script:
                return None
            match = re.search(r"page=(\d+)", script)
            page = int(match.group(1)) if match else 1
            return json.dumps(pages.get(page, []))

        cdp.eval_js.side_effect = eval_js
        return cdp, (
            mock.patch.object(module, "resolve_city",
                              return_value=("上海", "101020100")),
            mock.patch.object(module, "CDPSession", return_value=cdp),
            mock.patch.object(module, "create_page_session", return_value=("t", "s")),
        )

    def test_empty_page_counter_resets_after_each_nonempty_page(self):
        module = load_module()
        pages = {
            1: page_jobs(1), 2: [], 3: page_jobs(3),
            4: [], 5: page_jobs(5),
        }
        _cdp, (city, cdp_session, page_session) = self._list_mocks(module, pages)
        with tempfile.TemporaryDirectory() as tmp:
            output = str(pathlib.Path(tmp) / "jobs.json")
            with city, cdp_session, page_session, \
                    mock.patch.object(module, "probe_risk_page", return_value={}), \
                    mock.patch.object(module, "flush_jobs"), \
                    mock.patch.object(module, "API_ATTEMPT_LIMIT", 1), \
                    mock.patch.object(module.time, "sleep"):
                result = module.scrape_list(
                    "AI", "上海", 5, {}, output,
                    max_concurrent=1, pages_parallel=1, lock_already_held=True)
        self.assertEqual(result["total"], 90)

    def test_confirmed_empty_page_risk_broadcasts_shared_lock(self):
        module = load_module()
        _cdp, (city, cdp_session, page_session) = self._list_mocks(module, {})
        with tempfile.TemporaryDirectory() as tmp:
            output = str(pathlib.Path(tmp) / "jobs.json")
            with city, cdp_session, page_session, \
                    mock.patch.object(module, "probe_risk_page",
                                      side_effect=[{}, {"hasSlider": True}]), \
                    mock.patch.object(module, "set_scrape_lock_risk") as set_risk, \
                    mock.patch.object(module, "send_alert"), \
                    mock.patch.object(module, "flush_jobs"), \
                    mock.patch.object(module, "API_ATTEMPT_LIMIT", 1), \
                    mock.patch.object(module.time, "sleep"):
                module.scrape_list("AI", "上海", 3, {}, output,
                                   max_concurrent=1, pages_parallel=1,
                                   lock_already_held=True)
        set_risk.assert_called_once_with()

    def test_retry_page_risk_broadcasts_shared_lock(self):
        module = load_module()
        _cdp, (city, cdp_session, page_session) = self._list_mocks(module, {})
        with tempfile.TemporaryDirectory() as tmp:
            output = str(pathlib.Path(tmp) / "jobs.json")
            with city, cdp_session, page_session, \
                    mock.patch.object(module, "probe_risk_page",
                                      side_effect=[{}, {"hasSlider": True}]), \
                    mock.patch.object(module, "set_scrape_lock_risk") as set_risk, \
                    mock.patch.object(module, "wait_for_risk_clear", return_value=False), \
                    mock.patch.object(module, "send_alert") as alert, \
                    mock.patch.object(module, "flush_jobs"), \
                    mock.patch.object(module, "API_ATTEMPT_LIMIT", 2), \
                    mock.patch.object(module.time, "sleep"), \
                    redirect_stdout(io.StringIO()) as output_text:
                module.scrape_list("AI", "上海", 1, {}, output,
                                   max_concurrent=1, pages_parallel=1,
                                   lock_already_held=True)
        set_risk.assert_called_once_with()
        alert.assert_called_once()
        self.assertIn("EXPORT_FAIL", output_text.getvalue())
        self.assertNotIn("EXPORT_OK", output_text.getvalue())

    def test_passive_restricted_response_does_not_fallback_to_xhr(self):
        module = load_module()
        cdp = mock.Mock()
        capture = mock.Mock()
        capture.wait_next_response.return_value = {"code": 0}
        restricted = module.LoginProbeResult(module.LoginProbeStatus.RESTRICTED)
        with tempfile.TemporaryDirectory() as tmp:
            output = str(pathlib.Path(tmp) / "jobs.json")
            with mock.patch.object(module, "resolve_city",
                                   return_value=("上海", "101020100")), \
                    mock.patch.object(module, "CDPSession", return_value=cdp), \
                    mock.patch.object(module, "create_page_session", return_value=("t", "s")), \
                    mock.patch.object(module, "NetworkJoblistCapture", return_value=capture), \
                    mock.patch.object(module, "classify_login_probe_response",
                                      return_value=restricted), \
                    mock.patch.object(module, "set_scrape_lock_risk") as set_risk, \
                    mock.patch.object(module, "send_alert") as alert, \
                    mock.patch.object(module, "_fetch_pages_parallel") as parallel_fetch, \
                    mock.patch.object(module, "flush_jobs"), \
                    mock.patch.object(module.time, "sleep"), \
                    redirect_stdout(io.StringIO()) as output_text:
                result = module.scrape_list(
                    "AI", "上海", 1, {}, output, list_mode="passive",
                    max_concurrent=1, pages_parallel=2, lock_already_held=True)
        set_risk.assert_called_once_with()
        alert.assert_called_once()
        parallel_fetch.assert_not_called()
        self.assertIn("EXPORT_FAIL", output_text.getvalue())
        self.assertNotIn("EXPORT_OK", output_text.getvalue())
        self.assertEqual(result["jobs"], [])

    def test_status_does_not_count_pending_as_detail_file(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            result_dir = pathlib.Path(tmp)
            (result_dir / "boss_details_real.json").write_text("[]", encoding="utf-8")
            (result_dir / "boss_details_real.json.pending.json").write_text(
                "[]", encoding="utf-8")
            fake_requests = mock.Mock()
            fake_requests.ConnectionError = ConnectionError
            fake_requests.Timeout = TimeoutError
            fake_requests.get.side_effect = ConnectionError("offline")
            with mock.patch.dict(sys.modules, {"requests": fake_requests}), \
                    mock.patch.object(module, "prepare_cdp_profile",
                                      return_value={"path": str(result_dir)}), \
                    mock.patch.object(module, "SCRAPE_LOCK_PATH",
                                      str(result_dir / "scrape.lock")), \
                    mock.patch.object(module, "check_cdp_cooldown", return_value=None), \
                    mock.patch.object(module, "check_cdp_recovery", return_value=0.0), \
                    redirect_stdout(io.StringIO()) as output:
                code = module.run_status(result_dir=str(result_dir))
        self.assertEqual(code, 0)
        self.assertIn("详情文件        : 1 个", output.getvalue())
        self.assertIn("最新 boss_details_real.json", output.getvalue())
        self.assertNotIn("最新 boss_details_real.json.pending.json", output.getvalue())
        self.assertIn("pending 文件    : 1 个", output.getvalue())

    def test_run_batch_forwards_safe_per_task_options(self):
        module = load_module()
        list_data = {
            "keyword": "AI", "city": "上海",
            "jobs": [{"job_id": "j0", "job_link": "https://x/a"}],
            "output_path": "boss_jobs_test.json", "security_map": {"j0": "sid"},
        }
        list_calls = []
        detail_calls = []
        filter_calls = []
        with mock.patch.object(module, "load_batch_config", return_value=(
                [{"keyword": "AI", "city": "上海", "pages": 1}], [])), \
                mock.patch.object(module, "scrape_list",
                                  side_effect=lambda *a, **k: list_calls.append(k)
                                  or list_data), \
                mock.patch.object(module, "scrape_details",
                                  side_effect=lambda *a, **k: detail_calls.append(k)
                                  or [{"job_id": "j0", "jd": "岗位职责"}]), \
                mock.patch.object(module, "_list_has_detail_risk", return_value=False), \
                mock.patch.object(module, "_merge_jd_into_export", return_value=(1, 0)), \
                mock.patch.object(module, "inactive_job_ids", return_value={"j0"}), \
                mock.patch.object(module, "filter_inactive_in_export",
                                  side_effect=lambda *a, **k: filter_calls.append((a, k))
                                  or (1, 0)), \
                mock.patch.object(module.os, "listdir", side_effect=[
                    ["old.json"], ["old.json", "boss_jobs_test.json"]]), \
                mock.patch.object(module.time, "sleep"):
            code = module._run_batch_impl(
                "batch.json", fmt="csv", allow_dom_fallback=True,
                max_jobs=7, max_seconds=12, filter_inactive=True)
        self.assertEqual(code, 0)
        self.assertEqual(list_calls[0]["fmt"], "csv")
        self.assertTrue(list_calls[0]["allow_dom_fallback"])
        self.assertEqual(list_calls[0]["max_jobs"], 7)
        self.assertEqual(detail_calls[0]["fmt"], "csv")
        self.assertEqual(detail_calls[0]["max_seconds"], 12)
        self.assertEqual(len(filter_calls), 1)

    def test_run_cli_forwards_safe_batch_options(self):
        module = load_module()
        argv = [
            "boss_cdp_raw.py", "--batch", "batch.json", "--format", "csv",
            "--max-jobs", "7", "--max-seconds", "12",
            "--allow-dom-fallback", "--filter-inactive", "--keep-without-jd",
        ]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(module, "cleanup_stale_tmp_files"), \
                mock.patch.object(module, "cleanup_security_sidecars"), \
                mock.patch.object(module, "require_runtime_dependencies", return_value=True), \
                mock.patch.object(module, "run_batch", return_value=0) as run_batch:
            with self.assertRaises(SystemExit) as ctx:
                module.run_cli()
        self.assertEqual(ctx.exception.code, 0)
        kwargs = run_batch.call_args.kwargs
        self.assertEqual(kwargs["fmt"], "csv")
        self.assertEqual(kwargs["max_jobs"], 7)
        self.assertEqual(kwargs["max_seconds"], 12)
        self.assertTrue(kwargs["allow_dom_fallback"])
        self.assertTrue(kwargs["filter_inactive"])
        self.assertTrue(kwargs["keep_without_jd"])

    def test_batch_rejects_incompatible_per_run_cli_options(self):
        module = load_module()
        cases = [
            ["--input", "input.json"],
            ["--merge", "old.json"],
            ["--output", "out.json"],
            ["--detail-output", "details.json"],
            ["--retry-job", "j0"],
            ["--db", "jobs.db"],
        ]
        for extra in cases:
            with self.subTest(extra=extra):
                argv = ["boss_cdp_raw.py", "--batch", "batch.json", *extra]
                with mock.patch.object(sys, "argv", argv), \
                        mock.patch.object(module, "cleanup_stale_tmp_files"), \
                        mock.patch.object(module, "cleanup_security_sidecars"), \
                        mock.patch.object(module, "run_batch") as run_batch, \
                        mock.patch("sys.stderr", new_callable=io.StringIO):
                    with self.assertRaises(SystemExit) as ctx:
                        module.run_cli()
                self.assertEqual(ctx.exception.code, 2)
                run_batch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
