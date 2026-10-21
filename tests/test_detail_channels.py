"""详情通道统计、错误序列与 panel 生命周期回归测试。"""

import importlib.util
import json
import pathlib
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "boss_cdp_raw.py"


def load_module():
    sys.modules.setdefault("websocket", mock.Mock())
    sys.modules.setdefault("requests", mock.Mock())
    spec = importlib.util.spec_from_file_location("boss_detail_channels", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def jobs(count):
    return [{
        "job_id": f"j{i}",
        "title": f"岗位{i}",
        "company": "公司",
        "location": "上海",
        "job_link": f"https://www.zhipin.com/job_detail/j{i}.html",
    } for i in range(count)]


def success(channel, job_id):
    return {
        "ok": True,
        "detail": {"job_id": job_id, "jd": "岗位职责"},
        "job_id": job_id,
        "reason": "",
        "message": "",
        "channel": channel,
    }


def failure(reason, channel, job_id):
    return {
        "ok": False,
        "detail": None,
        "job_id": job_id,
        "reason": reason,
        "message": "fixture",
        "channel": channel,
    }


class DetailChannelTests(unittest.TestCase):
    def test_serial_path_aggregates_actual_channels(self):
        module = load_module()
        stats = {}
        with tempfile.TemporaryDirectory() as tmp:
            output = str(pathlib.Path(tmp) / "details.json")
            with mock.patch.object(module, "_open_dom_tab",
                                   return_value=[mock.Mock(), "t", "s", 0]), \
                    mock.patch.object(module, "_close_api_tab"), \
                    mock.patch.object(module, "_scrape_one_detail", side_effect=[
                        success("api", "j0"), success("dom", "j1"),
                        success("panel", "j2")]), \
                    mock.patch.object(module.time, "sleep"):
                result = module.scrape_details(
                    {"jobs": jobs(3)}, output_path=output,
                    detail_channel="dom", channel_stats=stats)
        self.assertEqual(len(result), 3)
        self.assertEqual(stats, {"api": 1, "dom": 1, "panel": 1})

    def test_serial_success_resets_consecutive_cdp_errors(self):
        module = load_module()
        stats = {}
        sequence = [
            failure("cdp_session", "dom", "j0"), success("dom", "j1"),
            failure("cdp_session", "dom", "j2"), success("dom", "j3"),
            failure("cdp_session", "dom", "j4"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            output = str(pathlib.Path(tmp) / "details.json")
            with mock.patch.object(module, "_open_dom_tab",
                                   return_value=[mock.Mock(), "t", "s", 0]), \
                    mock.patch.object(module, "_close_api_tab"), \
                    mock.patch.object(module, "_scrape_one_detail",
                                      side_effect=sequence), \
                    mock.patch.object(module, "mark_cdp_cooldown") as cooldown, \
                    mock.patch.object(module.time, "sleep"):
                result = module.scrape_details(
                    {"jobs": jobs(5)}, output_path=output,
                    detail_channel="dom", channel_stats=stats)
        self.assertEqual(len(result), 2)
        self.assertEqual(stats, {"dom": 2})
        cooldown.assert_not_called()

    def test_parallel_success_resets_consecutive_cdp_errors(self):
        module = load_module()
        stats = {}
        sequence = [
            failure("cdp_session", "dom", "j0"), success("dom", "j1"),
            failure("cdp_session", "dom", "j2"), success("dom", "j3"),
            failure("cdp_session", "dom", "j4"), success("dom", "j5"),
            failure("cdp_session", "dom", "j6"),
        ]
        lock = threading.Lock()
        cursor = [0]

        def next_result(*args, **kwargs):
            with lock:
                index = cursor[0]
                cursor[0] += 1
            return sequence[index]

        limiter = mock.Mock()
        with tempfile.TemporaryDirectory() as tmp:
            output = str(pathlib.Path(tmp) / "details.json")
            with mock.patch.object(module, "_scrape_one_detail",
                                   side_effect=next_result), \
                    mock.patch.object(module, "AdaptiveRateLimiter",
                                      return_value=limiter), \
                    mock.patch.object(module, "_open_dom_tab",
                                      return_value=[mock.Mock(), "t", "s", 0]), \
                    mock.patch.object(module, "_close_api_tab"), \
                    mock.patch.object(module, "load_existing_detail_ids",
                                      return_value=set()), \
                    mock.patch.object(module, "load_pending_ids", return_value={}), \
                    mock.patch.object(module, "check_cdp_recovery", return_value=0.0), \
                    mock.patch.object(module, "mark_cdp_cooldown") as cooldown, \
                    mock.patch.object(module.time, "sleep"):
                result, pending = module._scrape_details_parallel(
                    jobs(7), 9222, 2, limiter=limiter,
                    existing_ids=set(), pending_ids={}, output_path=output,
                    channel_stats=stats)
        self.assertEqual(len(result), 3)
        self.assertEqual(stats, {"dom": 3})
        self.assertEqual(cursor[0], 7, "成功后应继续提交所有任务")
        cooldown.assert_not_called()

    def test_panel_success_records_actual_channel(self):
        module = load_module()
        stats = {}
        session = [mock.Mock(), "t", "s", 0]
        with tempfile.TemporaryDirectory() as tmp:
            output = str(pathlib.Path(tmp) / "details.json")
            with mock.patch.object(module, "_open_api_tab", return_value=session), \
                    mock.patch.object(module, "_close_api_tab"), \
                    mock.patch.object(module, "_scrape_one_detail_via_panel",
                                      return_value=success("panel", "j0")), \
                    mock.patch.object(module.time, "sleep"):
                result = module._scrape_details_via_panel(
                    jobs(1), {"city_code": "101020100", "keyword": "AI"},
                    output, 9222, "json", existing_ids=set(), pending_ids={},
                    channel_stats=stats)
        self.assertEqual(len(result), 1)
        self.assertEqual(stats, {"panel": 1})

    def test_panel_no_session_still_writes_output_and_pending(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            output = str(pathlib.Path(tmp) / "details.json")
            with mock.patch.object(module, "_open_api_tab", return_value=None):
                result = module._scrape_details_via_panel(
                    jobs(1), {"city_code": "101020100", "keyword": "AI"},
                    output, 9222, "json", existing_ids=set(),
                    pending_ids={"j0": 1})
            self.assertEqual(result, [])
            self.assertTrue(pathlib.Path(output).exists())
            pending = json.loads(pathlib.Path(output + ".pending.json").read_text(
                encoding="utf-8"))
            self.assertEqual(pending, [{"job_id": "j0", "attempts": 1}])

    def test_parallel_worker_exception_keeps_original_job_pending(self):
        module = load_module()
        limiter = mock.Mock()

        def detail_for_job(job, *args, **kwargs):
            if job["job_id"] == "j0":
                raise TimeoutError("fixture cdp death")
            return success("dom", job["job_id"])

        with tempfile.TemporaryDirectory() as tmp:
            output = str(pathlib.Path(tmp) / "details.json")
            with mock.patch.object(module, "_scrape_one_detail", side_effect=detail_for_job), \
                    mock.patch.object(module, "AdaptiveRateLimiter", return_value=limiter), \
                    mock.patch.object(module, "_open_dom_tab",
                                      return_value=[mock.Mock(), "t", "s", 0]), \
                    mock.patch.object(module, "_close_api_tab"), \
                    mock.patch.object(module, "load_existing_detail_ids", return_value=set()), \
                    mock.patch.object(module, "load_pending_ids", return_value={}), \
                    mock.patch.object(module, "check_cdp_recovery", return_value=0.0), \
                    mock.patch.object(module.time, "sleep"), \
                    mock.patch.object(module, "mark_cdp_cooldown"):
                _, pending = module._scrape_details_parallel(
                    jobs(2), 9222, 1, limiter=limiter,
                    existing_ids=set(), pending_ids={}, output_path=output)
        self.assertEqual(pending, {"j0": 1})
        self.assertNotIn("j1", pending)

    def test_parallel_stopped_result_is_not_pending_failure(self):
        module = load_module()
        limiter = mock.Mock()
        stopped = {
            "ok": False, "detail": None, "job_id": "j0",
            "reason": "stopped", "message": "stop", "channel": "dom",
        }
        with tempfile.TemporaryDirectory() as tmp:
            output = str(pathlib.Path(tmp) / "details.json")
            with mock.patch.object(module, "_scrape_one_detail", return_value=stopped), \
                    mock.patch.object(module, "AdaptiveRateLimiter", return_value=limiter), \
                    mock.patch.object(module, "_open_dom_tab",
                                      return_value=[mock.Mock(), "t", "s", 0]), \
                    mock.patch.object(module, "_close_api_tab"), \
                    mock.patch.object(module, "load_existing_detail_ids", return_value=set()), \
                    mock.patch.object(module, "load_pending_ids", return_value={}), \
                    mock.patch.object(module, "check_cdp_recovery", return_value=0.0), \
                    mock.patch.object(module, "run_summary", return_value="") as summary, \
                    mock.patch.object(module.time, "sleep"):
                _, pending = module._scrape_details_parallel(
                    jobs(1), 9222, 1, limiter=limiter,
                    existing_ids=set(), pending_ids={}, output_path=output)
        self.assertEqual(pending, {})
        limiter.record_failure.assert_not_called()
        summary.assert_not_called()

    def test_panel_dom_session_is_replaced_after_cdp_failure(self):
        module = load_module()
        sessions = [[mock.Mock(), "t1", "s1", 0],
                    [mock.Mock(), "t2", "s2", 0]]
        panel_miss = {
            "ok": False, "detail": None, "job_id": "j0",
            "reason": "invalid_detail", "message": "miss", "channel": "panel",
        }
        dom_failure = failure("cdp_session", "dom", "j0")
        dom_success = success("dom", "j1")
        with tempfile.TemporaryDirectory() as tmp:
            output = str(pathlib.Path(tmp) / "details.json")
            with mock.patch.object(module, "_open_api_tab", return_value=sessions[0]), \
                    mock.patch.object(module, "_open_dom_tab", side_effect=sessions) as open_dom, \
                    mock.patch.object(module, "_close_api_tab") as close_tab, \
                    mock.patch.object(module, "_scrape_one_detail_via_panel", return_value=panel_miss), \
                    mock.patch.object(module, "_scrape_one_detail", side_effect=[dom_failure, dom_success]), \
                    mock.patch.object(module.time, "sleep"):
                result = module._scrape_details_via_panel(
                    jobs(2), {"city_code": "101020100", "keyword": "AI"},
                    output, 9222, "json", existing_ids=set(), pending_ids={})
        self.assertEqual(len(result), 1)
        self.assertEqual(open_dom.call_count, 2)
        close_tab.assert_any_call(sessions[0])

    def test_panel_risk_persists_pending_before_finalization(self):
        module = load_module()
        events = []
        risk = {
            "ok": False, "detail": None, "job_id": "j0",
            "reason": "risk_timeout", "message": "risk", "channel": "panel",
        }
        with tempfile.TemporaryDirectory() as tmp:
            output = str(pathlib.Path(tmp) / "details.json")
            with mock.patch.object(module, "_open_api_tab",
                                   return_value=[mock.Mock(), "t", "s", 0]), \
                    mock.patch.object(module, "_close_api_tab"), \
                    mock.patch.object(module, "_scrape_one_detail_via_panel", return_value=risk), \
                    mock.patch.object(module, "save_pending_ids",
                                      side_effect=lambda *args: events.append("pending")), \
                    mock.patch.object(module, "_note_detail_risk_blocked",
                                      side_effect=lambda *args, **kwargs: events.append("risk")), \
                    mock.patch.object(module.time, "sleep"):
                module._scrape_details_via_panel(
                    jobs(1), {"city_code": "101020100", "keyword": "AI"},
                    output, 9222, "json", existing_ids=set(), pending_ids={})
        self.assertEqual(events[:2], ["pending", "risk"])

    def test_preloaded_channel_stats_empty_falls_back_without_unknown(self):
        module = load_module()
        metadata = module._detail_channel_metadata(
            {"jobs": [{"job_id": "j0"}], "security_map": {}},
            "dom", {}, stats_supplied=True)
        self.assertEqual(metadata, {
            "detail_channel": "dom", "detail_channel_counts": {},
        })

    def test_panel_dead_session_stops_scheduling_and_persists_pending(self):
        module = load_module()
        session = [mock.Mock(), "t", "s", 0]
        panel_calls = []
        with tempfile.TemporaryDirectory() as tmp:
            output = str(pathlib.Path(tmp) / "details.json")
            with mock.patch.object(module, "_open_api_tab", return_value=session), \
                    mock.patch.object(module, "_close_api_tab") as close_session, \
                    mock.patch.object(module, "_scrape_one_detail_via_panel",
                                      side_effect=lambda *args, **kwargs: (
                                          panel_calls.append(args[0].get("job_id"))
                                          or failure("cdp_session", "panel", args[0].get("job_id")))):
                result = module._scrape_details_via_panel(
                    jobs(2), {"city_code": "101020100", "keyword": "AI"},
                    output, 9222, "json", existing_ids=set(), pending_ids={})
            self.assertEqual(result, [])
            self.assertEqual(panel_calls, ["j0"], "死会话后不得继续点击 panel")
            close_session.assert_any_call(session)
            pending = json.loads(pathlib.Path(output + ".pending.json").read_text(
                encoding="utf-8"))
            self.assertEqual(pending, [{"job_id": "j0", "attempts": 1}])

    def test_detail_channel_metadata_prefers_actual_counts(self):
        module = load_module()
        list_data = {
            "jobs": [{"job_id": "j0"}],
            "security_map": {"j0": "sid"},
        }
        actual = module._detail_channel_metadata(
            list_data, "auto", {"api": 1, "dom": 1, "panel": 1},
            stats_supplied=True)
        self.assertEqual(actual, {
            "detail_channel": "mixed",
            "detail_channel_counts": {"api": 1, "dom": 1, "panel": 1},
        })
        fallback = module._detail_channel_metadata(
            list_data, "auto", {}, stats_supplied=False)
        self.assertEqual(fallback["detail_channel"], "api")
        self.assertNotIn("detail_channel_counts", fallback)

    def test_run_batch_writes_actual_channel_metadata(self):
        module = load_module()
        list_data = {
            "keyword": "AI", "city": "上海",
            "jobs": jobs(1), "output_path": "boss_jobs_test.json",
            "security_map": {"j0": "sid"},
        }
        merged = {}

        def fake_details(*args, **kwargs):
            kwargs["channel_stats"].update({"panel": 1})
            return [{"job_id": "j0", "jd": "岗位职责"}]

        def fake_merge(*args, **kwargs):
            merged.update(kwargs)
            return (1, 0)

        with mock.patch.object(module, "load_batch_config",
                               return_value=([{"keyword": "AI", "city": "上海",
                                               "pages": 1}], [])), \
                mock.patch.object(module, "scrape_list", return_value=list_data), \
                mock.patch.object(module, "scrape_details", side_effect=fake_details), \
                mock.patch.object(module, "_list_has_detail_risk", return_value=False), \
                mock.patch.object(module, "_merge_jd_into_export", side_effect=fake_merge), \
                mock.patch.object(module.os, "listdir", side_effect=[
                    ["old.json"], ["old.json", "boss_jobs_test.json"]]):
            code = module.run_batch("batch.json", lock_already_held=True)
        self.assertEqual(code, 0)
        self.assertEqual(merged["extra_meta"], {
            "detail_channel": "panel",
            "detail_channel_counts": {"panel": 1},
        })

    def test_run_cli_passes_actual_channel_counts_to_export_metadata(self):
        module = load_module()
        merged = {}

        def fake_details(*args, **kwargs):
            kwargs["channel_stats"].update({"api": 1, "dom": 1, "panel": 1})
            return [{"job_id": "j0", "jd": "岗位职责"}]

        def fake_merge(*args, **kwargs):
            merged.update(kwargs)
            return (1, 0)

        with tempfile.TemporaryDirectory() as tmp:
            output = str(pathlib.Path(tmp) / "boss_jobs_test.json")
            list_data = {
                "keyword": "AI", "city": "上海", "city_code": "101020100",
                "jobs": [{"job_id": "j0", "title": "A", "location": "上海",
                          "job_link": "https://x/a", "company_name": "C"}],
                "output_path": output, "security_map": {"j0": "sid"},
            }
            argv = ["boss_cdp_raw.py", "--keyword", "AI", "--city", "上海",
                    "--pages", "1", "--output", output]
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(module, "require_runtime_dependencies", return_value=True), \
                    mock.patch.object(module, "cleanup_stale_tmp_files"), \
                    mock.patch.object(module, "cleanup_security_sidecars"), \
                    mock.patch.object(module, "resolve_city", return_value=("上海", "101020100")), \
                    mock.patch.object(module, "check_cdp_cooldown", return_value=None), \
                    mock.patch.object(module, "acquire_scrape_lock", return_value=True), \
                    mock.patch.object(module, "release_scrape_lock"), \
                    mock.patch.object(module, "check_login_state",
                                      return_value=module.LoginProbeResult(module.LoginProbeStatus.AVAILABLE)), \
                    mock.patch.object(module, "close_orphan_dock_tabs", return_value=0), \
                    mock.patch.object(module, "close_orphan_detail_tabs", return_value=0), \
                    mock.patch.object(module, "scrape_list", return_value=list_data), \
                    mock.patch.object(module, "scrape_details", side_effect=fake_details), \
                    mock.patch.object(module, "write_security_sidecar", return_value=None), \
                    mock.patch.object(module, "_merge_jd_into_export", side_effect=fake_merge):
                module.run_cli()
        # 通道元数据必须原样带进导出（2026-10-22 起 extra_meta 还会带 supply：
        # 供给 × 额度协调结果，故此处只断言通道相关键）
        self.assertEqual(merged["extra_meta"]["detail_channel"], "mixed")
        self.assertEqual(merged["extra_meta"]["detail_channel_counts"],
                         {"api": 1, "dom": 1, "panel": 1})


if __name__ == "__main__":
    unittest.main()
