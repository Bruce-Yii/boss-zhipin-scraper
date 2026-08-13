import importlib.util
import contextlib
import csv
import io
import json
import logging
import os
import pathlib
import platform
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock


# Windows 控制台默认 GBK，测试断言/回溯含 emoji 会 UnicodeEncodeError；
# 统一重配为 UTF-8，保证测试不依赖外部 PYTHONIOENCODING 环境变量。
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


SCRIPT_PATH = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "boss_cdp_raw.py"


def load_module():
    sys.modules.setdefault("websocket", mock.Mock())
    sys.modules.setdefault("requests", mock.Mock())
    spec = importlib.util.spec_from_file_location("boss_cdp_raw", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ChromeSetupTests(unittest.TestCase):
    def test_default_cdp_profile_is_persistent_and_not_default_or_tmp(self):
        module = load_module()

        self.assertNotEqual(module.DEFAULT_CDP_DATA_DIR, module.DEFAULT_PROFILE_DIR)
        self.assertNotIn("/tmp/", module.DEFAULT_CDP_DATA_DIR)
        self.assertTrue(module.DEFAULT_CDP_DATA_DIR.endswith(".boss-zhipin-scraper/chrome-profile"))

    def test_default_result_dir_is_persistent_user_state(self):
        module = load_module()

        self.assertNotIn("/tmp/", module.DEFAULT_RESULT_DIR)
        self.assertTrue(module.DEFAULT_RESULT_DIR.endswith(".boss-zhipin-scraper/job-result"))
        self.assertTrue(module.default_output_path("jobs").startswith(module.DEFAULT_RESULT_DIR))
        self.assertTrue(module.default_output_path("details").startswith(module.DEFAULT_RESULT_DIR))
        self.assertIn("boss_jobs_", module.default_output_path("jobs"))
        self.assertIn("boss_details_", module.default_output_path("details"))

    def test_default_output_path_unique_across_concurrent_calls(self):
        """并发（多进程同秒写盘）不撞名：秒级时间戳 + pid 后缀（灰度实测暴露）。"""
        module = load_module()
        fake_now = module.datetime(2026, 8, 12, 18, 30, 45)
        fake_dt = mock.Mock()
        fake_dt.now.return_value = fake_now
        names = set()
        with mock.patch.object(module, "datetime", fake_dt), \
                mock.patch.object(module.os, "getpid",
                                  side_effect=["1111", "2222"]):
            names.add(module.default_output_path("jobs"))
            names.add(module.default_output_path("jobs"))
        self.assertEqual(len(names), 2,
                         "同秒不同 pid 的文件名应不同（不覆盖）")
        for n in names:
            self.assertIn("20260812_183045", n, "文件名含秒级时间戳")
        self.assertTrue(all("_1111" in n or "_2222" in n for n in names),
                        "文件名含 pid 后缀")

    def test_create_page_session_defaults_to_background_with_visibility_override(self):
        module = load_module()
        cdp = mock.Mock()
        cdp.send.side_effect = [
            {"result": {"targetId": "target-1"}},
            {"result": {"sessionId": "session-1"}},
            {"result": {}},
        ]

        result = module.create_page_session(cdp)

        self.assertEqual(result, ("target-1", "session-1"))
        self.assertEqual(
            cdp.send.call_args_list,
            [
                mock.call(
                    "Target.createTarget",
                    {"url": "about:blank", "background": True},
                ),
                mock.call(
                    "Target.attachToTarget",
                    {"targetId": "target-1", "flatten": True},
                ),
                mock.call(
                    "Page.addScriptToEvaluateOnNewDocument",
                    {"source": module.BACKGROUND_VISIBILITY_SCRIPT},
                    "session-1",
                ),
            ],
        )

    def test_create_page_session_can_open_interactive_foreground_target(self):
        module = load_module()
        cdp = mock.Mock()
        cdp.send.side_effect = [
            {"result": {"targetId": "login-target"}},
            {"result": {"sessionId": "login-session"}},
        ]

        result = module.create_page_session(
            cdp,
            background=False,
        )

        self.assertEqual(result, ("login-target", "login-session"))
        self.assertEqual(
            cdp.send.call_args_list,
            [
                mock.call(
                    "Target.createTarget",
                    {
                        "url": "about:blank",
                        "background": False,
                    },
                ),
                mock.call(
                    "Target.attachToTarget",
                    {"targetId": "login-target", "flatten": True},
                ),
            ],
        )

    # ----- 页面级风控/验证码检测 -----

    def test_classify_risk_page_clean_page_is_not_risk(self):
        module = load_module()
        probe = {"url": "https://www.zhipin.com/job_detail/x.html",
                 "title": "职位详情", "hasSlider": False, "hasLoginWall": False}
        self.assertEqual(module.classify_risk_page(probe), (False, ""))

    def test_classify_risk_page_detects_verify_url(self):
        module = load_module()
        probe = {"url": "https://www.zhipin.com/security-check/security.html",
                 "title": "BOSS直聘", "hasSlider": False, "hasLoginWall": False}
        is_risk, reason = module.classify_risk_page(probe)
        self.assertTrue(is_risk)
        self.assertIn("验证", reason)

    def test_classify_risk_page_detects_slider_element(self):
        module = load_module()
        probe = {"url": "https://www.zhipin.com/", "title": "",
                 "hasSlider": True, "hasLoginWall": False}
        is_risk, reason = module.classify_risk_page(probe)
        self.assertTrue(is_risk)
        self.assertIn("滑块", reason)

    def test_classify_risk_page_detects_login_wall(self):
        module = load_module()
        probe = {"url": "https://www.zhipin.com/", "title": "",
                 "hasSlider": False, "hasLoginWall": True}
        is_risk, reason = module.classify_risk_page(probe)
        self.assertTrue(is_risk)
        self.assertIn("登录墙", reason)

    def test_classify_risk_page_tolerates_bad_input(self):
        module = load_module()
        self.assertEqual(module.classify_risk_page(None), (False, ""))
        self.assertEqual(module.classify_risk_page({}), (False, ""))
        self.assertEqual(module.classify_risk_page("not-a-dict"), (False, ""))

    def test_probe_risk_page_returns_empty_on_eval_failure(self):
        module = load_module()
        cdp = mock.Mock()
        cdp.eval_js.side_effect = TimeoutError("cdp timeout")
        self.assertEqual(module.probe_risk_page(cdp, "sid"), {})

    def test_probe_risk_page_parses_json_value(self):
        module = load_module()
        cdp = mock.Mock()
        cdp.eval_js.return_value = json.dumps(
            {"url": "x", "title": "安全验证", "hasSlider": True, "hasLoginWall": False}
        )
        probe = module.probe_risk_page(cdp, "sid")
        self.assertEqual(probe["title"], "安全验证")
        self.assertTrue(probe["hasSlider"])

    def test_wait_for_risk_clear_returns_true_when_risk_resolved(self):
        module = load_module()
        cdp = mock.Mock()
        # 第一次命中风控，第二次恢复
        probes = [
            {"url": "security-check", "title": "", "hasSlider": True, "hasLoginWall": False},
            {"url": "https://www.zhipin.com/job_detail/x.html", "title": "职位详情",
             "hasSlider": False, "hasLoginWall": False},
        ]
        with mock.patch.object(module, "probe_risk_page", side_effect=probes), \
                mock.patch.object(module.time, "sleep"):
            self.assertTrue(module.wait_for_risk_clear(cdp, "sid", timeout=30, interval=1))

    def test_wait_for_risk_clear_returns_false_on_timeout(self):
        module = load_module()
        cdp = mock.Mock()
        probe = {"url": "security-check", "title": "安全验证",
                 "hasSlider": True, "hasLoginWall": False}
        with mock.patch.object(module, "probe_risk_page", return_value=probe), \
                mock.patch.object(module.time, "sleep"), \
                mock.patch.object(module.time, "time", side_effect=[0, 1, 2, 999, 1000]):
            self.assertFalse(module.wait_for_risk_clear(cdp, "sid", timeout=30, interval=1))

    # ----- 凭证自愈 -----

    def test_eval_detail_with_retry_uses_first_success(self):
        module = load_module()
        ws = mock.Mock()
        ws.eval_js.return_value = json.dumps(
            {"jd": "职位描述\nBuild AI agents", "page_text": "职位描述\nBuild AI agents",
             "tags": ["Python"], "url": "https://x"})
        with mock.patch.object(module.time, "sleep"):
            d = module.eval_detail_with_retry(ws, "sid", "https://x")
        self.assertEqual(d["jd"], "职位描述\nBuild AI agents")
        self.assertEqual(ws.eval_js.call_count, 1)
        ws.send.assert_not_called()

    def test_eval_detail_with_retry_refreshes_on_empty_and_recovers(self):
        module = load_module()
        ws = mock.Mock()
        empty = json.dumps({"jd": "", "page_text": "", "tags": [], "url": "https://x"})
        good = json.dumps({"jd": "职位描述\nOK", "page_text": "职位描述\nOK",
                           "tags": [], "url": "https://x"})
        ws.eval_js.side_effect = [empty, good]
        with mock.patch.object(module.time, "sleep"), \
                mock.patch.object(module.random, "uniform", return_value=1.0):
            d = module.eval_detail_with_retry(ws, "sid", "https://x")
        self.assertEqual(d["jd"], "职位描述\nOK")
        self.assertEqual(ws.eval_js.call_count, 2)
        ws.send.assert_called_once_with(
            "Page.navigate", {"url": "https://x"}, "sid")

    def test_eval_detail_with_retry_gives_up_after_retries(self):
        module = load_module()
        ws = mock.Mock()
        empty = json.dumps({"jd": "", "page_text": "", "tags": [], "url": "https://x"})
        ws.eval_js.return_value = empty
        with mock.patch.object(module.time, "sleep"), \
                mock.patch.object(module.random, "uniform", return_value=1.0):
            d = module.eval_detail_with_retry(ws, "sid", "https://x", retries=2)
        self.assertEqual(d["jd"], "")
        self.assertEqual(ws.eval_js.call_count, 3)

    # ----- 历史 job_id 预加载 -----

    def test_load_existing_detail_ids_missing_file_returns_empty(self):
        module = load_module()
        with tempfile_profile() as paths:
            self.assertEqual(
                module.load_existing_detail_ids(str(paths["cdp_profile"] / "none.json")), set())

    def test_load_existing_detail_ids_corrupt_file_returns_empty(self):
        module = load_module()
        with tempfile_profile() as paths:
            target = str(paths["cdp_profile"] / "details.json")
            os.makedirs(paths["cdp_profile"], exist_ok=True)
            with open(target, "w", encoding="utf-8") as f:
                f.write("{corrupt")
            self.assertEqual(module.load_existing_detail_ids(target), set())

    def test_load_existing_detail_ids_returns_job_ids(self):
        module = load_module()
        with tempfile_profile() as paths:
            target = str(paths["cdp_profile"] / "details.json")
            os.makedirs(paths["cdp_profile"], exist_ok=True)
            payload = [{"job_id": "a", "title": "x"}, {"job_id": "b"},
                       "not-a-dict", {"title": "no-id"}]
            with open(target, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            self.assertEqual(module.load_existing_detail_ids(target), {"a", "b"})

    # ----- 登录校验轮换探测 -----

    def test_check_login_state_rotates_on_empty_results(self):
        module = load_module()
        cdp = mock.Mock()
        EMPTY = module.LoginProbeResult(module.LoginProbeStatus.EMPTY)
        AVAILABLE = module.LoginProbeResult(module.LoginProbeStatus.AVAILABLE)
        with mock.patch.object(module, "CDPSession", return_value=cdp), \
                mock.patch.object(module, "create_page_session",
                                  return_value=("t", "s")), \
                mock.patch.object(module, "probe_login_state",
                                  side_effect=[EMPTY, EMPTY, AVAILABLE]) as probe_mock, \
                mock.patch.object(module.time, "sleep"):
            result = module.check_login_state(cdp_port=9333)
            # 三组探测目标全部轮换
            self.assertEqual(probe_mock.call_count, 3)
        self.assertEqual(result.status, module.LoginProbeStatus.AVAILABLE)

    def test_check_login_state_stops_on_unauthenticated(self):
        module = load_module()
        cdp = mock.Mock()
        UNAUTH = module.LoginProbeResult(module.LoginProbeStatus.UNAUTHENTICATED)
        with mock.patch.object(module, "CDPSession", return_value=cdp), \
                mock.patch.object(module, "create_page_session",
                                  return_value=("t", "s")), \
                mock.patch.object(module, "probe_login_state", return_value=UNAUTH) as probe_mock, \
                mock.patch.object(module.time, "sleep"):
            result = module.check_login_state(cdp_port=9333)
            # 确定状态直接返回，不轮换
            self.assertEqual(probe_mock.call_count, 1)
        self.assertEqual(result.status, module.LoginProbeStatus.UNAUTHENTICATED)

    def test_check_login_state_returns_last_result_when_all_rotations_fail(self):
        module = load_module()
        cdp = mock.Mock()
        RESP_ERR = module.LoginProbeResult(module.LoginProbeStatus.RESPONSE_ERROR,
                                           message="boom", retryable=True)
        with mock.patch.object(module, "CDPSession", return_value=cdp), \
                mock.patch.object(module, "create_page_session",
                                  return_value=("t", "s")), \
                mock.patch.object(module, "probe_login_state", return_value=RESP_ERR) as probe_mock, \
                mock.patch.object(module.time, "sleep"):
            result = module.check_login_state(cdp_port=9333)
            self.assertEqual(probe_mock.call_count, len(module.LOGIN_PROBE_TARGETS))
        self.assertEqual(result.status, module.LoginProbeStatus.RESPONSE_ERROR)

    # ----- 登录探测会话内缓存 -----

    def test_check_login_state_caches_result_within_ttl(self):
        module = load_module()
        cdp = mock.Mock()
        AVAILABLE = module.LoginProbeResult(module.LoginProbeStatus.AVAILABLE)
        with mock.patch.object(module, "CDPSession", return_value=cdp) as cdp_mock, \
                mock.patch.object(module, "create_page_session",
                                  return_value=("t", "s")), \
                mock.patch.object(module, "probe_login_state",
                                  return_value=AVAILABLE) as probe_mock, \
                mock.patch.object(module.time, "sleep"):
            first = module.check_login_state(cdp_port=9333)
            second = module.check_login_state(cdp_port=9333)
            self.assertIs(second, first, "TTL 内应直接复用缓存结果")
            self.assertEqual(cdp_mock.call_count, 1, "TTL 内不应重复连接 CDP")
            self.assertEqual(probe_mock.call_count, 1)

    def test_check_login_state_cache_expires_after_ttl(self):
        module = load_module()
        cdp = mock.Mock()
        UNAUTH = module.LoginProbeResult(module.LoginProbeStatus.UNAUTHENTICATED)
        with mock.patch.object(module, "CDPSession", return_value=cdp) as cdp_mock, \
                mock.patch.object(module, "create_page_session",
                                  return_value=("t", "s")), \
                mock.patch.object(module, "probe_login_state",
                                  return_value=UNAUTH) as probe_mock, \
                mock.patch.object(module.time, "sleep"):
            module.check_login_state(cdp_port=9333)
            module._LOGIN_PROBE_CACHE["ts"] -= module.LOGIN_PROBE_CACHE_TTL + 1
            module.check_login_state(cdp_port=9333)
            self.assertEqual(cdp_mock.call_count, 2, "缓存过期后应重新探测")
            self.assertEqual(probe_mock.call_count, 2)

    def test_check_login_state_use_cache_false_bypasses_cache(self):
        module = load_module()
        cdp = mock.Mock()
        AVAILABLE = module.LoginProbeResult(module.LoginProbeStatus.AVAILABLE)
        with mock.patch.object(module, "CDPSession", return_value=cdp) as cdp_mock, \
                mock.patch.object(module, "create_page_session",
                                  return_value=("t", "s")), \
                mock.patch.object(module, "probe_login_state",
                                  return_value=AVAILABLE) as probe_mock, \
                mock.patch.object(module.time, "sleep"):
            module.check_login_state(cdp_port=9333)
            module.check_login_state(cdp_port=9333, use_cache=False)
            self.assertEqual(cdp_mock.call_count, 2, "use_cache=False 应强制重新探测")
            self.assertEqual(probe_mock.call_count, 2)

    # ----- 断点续跑（pending 清单）-----

    def test_pending_path_for_appends_suffix(self):
        module = load_module()
        self.assertEqual(
            module.pending_path_for("C:/x/details.json"),
            "C:/x/details.json.pending.json",
        )

    def test_load_pending_ids_handles_missing_and_corrupt(self):
        module = load_module()
        with tempfile_profile() as paths:
            missing = str(paths["cdp_profile"] / "no.json")
            self.assertEqual(module.load_pending_ids(missing), {})
            corrupt = str(paths["cdp_profile"] / "bad.json")
            os.makedirs(paths["cdp_profile"], exist_ok=True)
            with open(corrupt, "w", encoding="utf-8") as f:
                f.write("not json")
            self.assertEqual(module.load_pending_ids(corrupt), {})

    def test_save_and_load_pending_ids_roundtrip(self):
        module = load_module()
        with tempfile_profile() as paths:
            out = str(paths["cdp_profile"] / "details.json")
            module.save_pending_ids(out, {"a": 0, "b": 2})
            self.assertEqual(module.load_pending_ids(out), {"a": 0, "b": 2})
            module.save_pending_ids(out, {})
            self.assertEqual(module.load_pending_ids(out), {})
            self.assertFalse(os.path.exists(module.pending_path_for(out)),
                             "空集合时应删除 pending 文件")

    def test_pending_retry_limit_gives_up_after_max_attempts(self):
        module = load_module()
        with tempfile_profile() as paths:
            out = str(paths["cdp_profile"] / "details.json")
            # 已达上限的 job 加载时应被放弃（不再自动重试浪费请求）
            module.save_pending_ids(out, {"job-giveup": 3, "job-retry": 1})
            pending = module.load_pending_ids(out)
            self.assertNotIn("job-giveup", pending, "达到重试上限应放弃")
            self.assertIn("job-retry", pending)
            self.assertEqual(pending["job-retry"], 1, "未达上限的保留原计数")

    def test_pending_load_handles_legacy_string_format(self):
        module = load_module()
        with tempfile_profile() as paths:
            out = str(paths["cdp_profile"] / "details.json")
            os.makedirs(paths["cdp_profile"], exist_ok=True)
            # 旧格式：纯字符串 job_id 列表（无计数）→ 归零兼容
            with open(module.pending_path_for(out), "w", encoding="utf-8") as f:
                json.dump(["legacy-1", "legacy-2"], f)
            self.assertEqual(module.load_pending_ids(out),
                             {"legacy-1": 0, "legacy-2": 0})

    def test_load_pending_ids_force_ids_override_retry_limit(self):
        module = load_module()
        with tempfile_profile() as paths:
            out = str(paths["cdp_profile"] / "details.json")
            module.save_pending_ids(out, {"job-giveup": 3, "job-retry": 1})
            pending = module.load_pending_ids(out, force_ids=["job-giveup"])
            self.assertIn("job-giveup", pending, "白名单内已达上限的 job 应强制重试")
            self.assertIn("job-retry", pending)

    def test_load_pending_ids_force_ids_add_unknown_jobs(self):
        module = load_module()
        with tempfile_profile() as paths:
            out = str(paths["cdp_profile"] / "details.json")
            module.save_pending_ids(out, {"job-a": 1})
            pending = module.load_pending_ids(out, force_ids=["job-a", "job-new"])
            self.assertIn("job-new", pending, "白名单里文件未记录的 job 也应加入重试")
            self.assertEqual(pending["job-new"], 0)

    # ----- --verify 结果文件校验 -----

    def _write_verify_files(self, paths, jobs, details,
                            list_name="boss_jobs_x.json",
                            detail_name="boss_details_x.json"):
        list_path = str(paths["cdp_profile"] / list_name)
        detail_path = str(paths["cdp_profile"] / detail_name)
        os.makedirs(paths["cdp_profile"], exist_ok=True)
        with open(list_path, "w", encoding="utf-8") as f:
            json.dump({"jobs": jobs}, f)
        with open(detail_path, "w", encoding="utf-8") as f:
            json.dump(details, f)
        return list_path, detail_path

    def test_verify_results_ok_for_complete_files(self):
        module = load_module()
        with tempfile_profile() as paths:
            list_path, detail_path = self._write_verify_files(
                paths,
                [{"job_id": "a", "title": "A"}, {"job_id": "b", "title": "B"}],
                [{"job_id": "a", "title": "A",
                  "jd": "x" * module.MIN_DETAIL_TEXT_LENGTH},
                 {"job_id": "b", "title": "B",
                  "jd": "x" * module.MIN_DETAIL_TEXT_LENGTH}],
            )
            report = module.verify_results(list_path, detail_path)
            self.assertTrue(report["ok"])
            self.assertEqual(report["issues"], [])
            self.assertEqual(report["coverage"], 1.0)
            self.assertEqual(report["list"]["count"], 2)
            self.assertEqual(report["details"]["count"], 2)

    def test_verify_results_flags_corrupt_list_file(self):
        module = load_module()
        with tempfile_profile() as paths:
            list_path = str(paths["cdp_profile"] / "boss_jobs_x.json")
            os.makedirs(paths["cdp_profile"], exist_ok=True)
            with open(list_path, "w", encoding="utf-8") as f:
                f.write("{corrupt")
            report = module.verify_results(list_path)
            self.assertFalse(report["ok"])
            self.assertTrue(any("无法解析" in i for i in report["issues"]))

    def test_verify_results_flags_missing_job_id_and_duplicates(self):
        module = load_module()
        with tempfile_profile() as paths:
            list_path, detail_path = self._write_verify_files(
                paths,
                [{"job_id": "a", "title": "A"},
                 {"title": "no-id"},
                 {"job_id": "a", "title": "dup"}],
                [],
            )
            report = module.verify_results(list_path, detail_path)
            self.assertFalse(report["ok"])
            joined = " | ".join(report["issues"])
            self.assertIn("job_id", joined)
            self.assertIn("重复", joined)
            self.assertEqual(report["coverage"], 0.0)

    def test_verify_results_auto_discovers_latest_details(self):
        module = load_module()
        with tempfile_profile() as paths:
            os.makedirs(paths["cdp_profile"], exist_ok=True)
            list_path = paths["cdp_profile"] / "boss_jobs_x.json"
            old_path = paths["cdp_profile"] / "boss_details_old.json"
            new_path = paths["cdp_profile"] / "boss_details_new.json"
            with open(list_path, "w", encoding="utf-8") as f:
                json.dump({"jobs": [{"job_id": "a", "title": "A"}]}, f)
            with open(old_path, "w", encoding="utf-8") as f:
                json.dump([{"job_id": "a", "title": "A", "jd": "x"}], f)
            with open(new_path, "w", encoding="utf-8") as f:
                json.dump([{"job_id": "a", "title": "A",
                            "jd": "x" * module.MIN_DETAIL_TEXT_LENGTH}], f)
            os.utime(old_path, (1000, 1000))
            os.utime(new_path, (2000, 2000))
            report = module.verify_results(str(list_path))
            self.assertTrue(report["ok"], f"应自动选中最新详情文件: {report['issues']}")
            self.assertEqual(report["coverage"], 1.0)

    def test_verify_results_flags_short_jd_details(self):
        module = load_module()
        with tempfile_profile() as paths:
            list_path, detail_path = self._write_verify_files(
                paths,
                [{"job_id": "a", "title": "A"}],
                [{"job_id": "a", "title": "A", "jd": "short"}],
            )
            report = module.verify_results(list_path, detail_path)
            self.assertFalse(report["ok"])
            self.assertTrue(any("JD" in i for i in report["issues"]))

    def test_verify_results_missing_detail_file(self):
        module = load_module()
        with tempfile_profile() as paths:
            list_path, _ = self._write_verify_files(
                paths, [{"job_id": "a", "title": "A"}], [])
            missing = str(paths["cdp_profile"] / "boss_details_none.json")
            report = module.verify_results(list_path, missing)
            self.assertFalse(report["ok"])
            self.assertTrue(any("未找到" in i for i in report["issues"]))

    def test_verify_latest_details_ignores_pending_files(self):
        module = load_module()
        with tempfile_profile() as paths:
            os.makedirs(paths["cdp_profile"], exist_ok=True)
            list_path = paths["cdp_profile"] / "boss_jobs_x.json"
            with open(list_path, "w", encoding="utf-8") as f:
                json.dump({"jobs": [{"job_id": "a", "title": "A"}]}, f)
            pending = paths["cdp_profile"] / "boss_details_x.json.pending.json"
            with open(pending, "w", encoding="utf-8") as f:
                json.dump([{"job_id": "a", "attempts": 1}], f)
            self.assertIsNone(module._latest_details_path(str(list_path)),
                              "pending 文件不应被当作详情文件")

    # ----- 列表抓取（max_jobs 条数上限）-----

    def test_scrape_list_stops_at_max_jobs(self):
        module = load_module()
        cdp = mock.Mock()
        stats = {"api_pages": []}

        def fake_eval_js(script, sid=None):
            if "xhr.open" not in script:
                return None
            m = re.search(r"page=(\d+)", script)
            pg = int(m.group(1)) if m else 1
            stats["api_pages"].append(pg)
            if pg > 2:
                return json.dumps([])
            jobs = [
                {"title": f"AI岗位{pg}-{i}",
                 "job_link": f"https://example.com/job/{pg}-{i}",
                 "salary": "20-40K", "boss_name": f"公司{pg}-{i}"}
                for i in range(30)
            ]
            return json.dumps(jobs)

        cdp.eval_js.side_effect = fake_eval_js
        with mock.patch.object(module, "resolve_city",
                               return_value=("上海", "101020100")), \
                mock.patch.object(module, "CDPSession", return_value=cdp), \
                mock.patch.object(module, "create_page_session",
                                  return_value=("t", "s")), \
                mock.patch.object(module, "probe_risk_page", return_value={}), \
                mock.patch.object(module, "flush_jobs"), \
                mock.patch.object(module.time, "sleep"):
            result = module.scrape_list("AI", "上海", 5, {}, None, max_jobs=50)
        self.assertEqual(len(result["jobs"]), 60, "达到目标后整页为止，允许一页边界")
        self.assertEqual(stats["api_pages"], [1, 2],
                         "第 2 页后累计 60 ≥ 50，不再请求第 3 页")

    def test_scrape_list_max_jobs_none_keeps_original_behavior(self):
        module = load_module()
        cdp = mock.Mock()
        stats = {"api_pages": []}

        def fake_eval_js(script, sid=None):
            if "xhr.open" not in script:
                return None
            m = re.search(r"page=(\d+)", script)
            pg = int(m.group(1)) if m else 1
            stats["api_pages"].append(pg)
            if pg > 1:
                return json.dumps([])
            jobs = [
                {"title": f"AI岗位{pg}-{i}",
                 "job_link": f"https://example.com/job/{pg}-{i}"}
                for i in range(30)
            ]
            return json.dumps(jobs)

        cdp.eval_js.side_effect = fake_eval_js
        with mock.patch.object(module, "resolve_city",
                               return_value=("上海", "101020100")), \
                mock.patch.object(module, "CDPSession", return_value=cdp), \
                mock.patch.object(module, "create_page_session",
                                  return_value=("t", "s")), \
                mock.patch.object(module, "probe_risk_page", return_value={}), \
                mock.patch.object(module, "flush_jobs"), \
                mock.patch.object(module.time, "sleep"):
            result = module.scrape_list("AI", "上海", 3, {}, None, max_jobs=None)
        self.assertEqual(len(result["jobs"]), 30, "不设上限时按页数正常抓取")
        self.assertEqual(stats["api_pages"][0], 1)
        self.assertEqual(stats["api_pages"][-1], 3, "空页重试后仍按页循环推进")

    # ----- 阶段进度汇报（每 10 条 / 总量>200 时每 5% 一报）-----

    def test_resume_hint_empty_when_no_pending(self):
        module = load_module()
        self.assertEqual(module.resume_hint({}, "C:/x/details.json"), "")

    def test_resume_hint_mentions_count_path_and_rerun(self):
        module = load_module()
        hint = module.resume_hint({"a": 2, "b": 1}, "C:/x/details.json")
        self.assertIn("2 个详情待重试", hint)
        self.assertIn("details.json.pending.json", hint)
        self.assertIn("重跑刚才的命令", hint)

    def test_run_summary_empty_when_no_jobs(self):
        module = load_module()
        self.assertEqual(module.run_summary(0, 0, 0, {}), "")

    def test_run_summary_reports_rate_and_reason_breakdown(self):
        module = load_module()
        line = module.run_summary(180, 30, 27, {"invalid_detail": 2,
                                                "cdp_session": 1})
        self.assertIn("完成 30 条", line)
        self.assertIn("成功 27", line)
        self.assertIn("失败 3", line)
        self.assertIn("invalid_detail:2", line)
        self.assertIn("cdp_session:1", line)
        self.assertIn("3 分 0 秒", line)
        self.assertIn("6s/条", line)

    def test_run_summary_formats_short_elapsed(self):
        module = load_module()
        line = module.run_summary(45, 10, 10, {})
        self.assertIn("45 秒", line)
        self.assertIn("5s/条", line)

    def test_progress_step_scales_with_total(self):
        module = load_module()
        self.assertEqual(module.progress_step(30), 10, "小任务固定每 10 条")
        self.assertEqual(module.progress_step(100), 10)
        self.assertEqual(module.progress_step(200), 10, "200 条以内保持 10")
        self.assertEqual(module.progress_step(209), 11, "超 200 后按 5% 取整")
        self.assertEqual(module.progress_step(300), 15)
        self.assertEqual(module.progress_step(500), 25)

    def test_progress_line_only_at_report_points(self):
        module = load_module()
        self.assertIsNone(module.progress_line(9, 100, 8), "未到汇报点返回 None")
        line = module.progress_line(10, 100, 9)
        self.assertIn("10/100", line)
        self.assertIn("10%", line)
        self.assertIn("成功 9", line)
        self.assertIn("失败 1", line)
        self.assertIsNone(module.progress_line(14, 300, 10), "300 条按 15 条粒度")
        self.assertIn("15/300", module.progress_line(15, 300, 11))
        final = module.progress_line(12, 12, 12)
        self.assertIn("12/12", final, "完成时即使不整除也应汇报")
        self.assertIn("100%", final)

    def test_parallel_progress_report_printed_per_step(self):
        module = load_module()
        jobs = self._sample_jobs(12)["jobs"]
        state = (threading.Lock(), [0], [0])
        with mock.patch.object(module, "_scrape_one_detail",
                               new=self._fake_parallel_worker(state, set())), \
                mock.patch.object(module, "AdaptiveRateLimiter") as limiter_cls, \
                mock.patch.object(module, "load_existing_detail_ids",
                                  return_value=set()), \
                mock.patch.object(module, "load_pending_ids",
                                  return_value=set()), \
                mock.patch.object(module.time, "sleep"), \
                mock.patch("sys.stdout", new_callable=__import__("io").StringIO) as out:
            module._scrape_details_parallel(
                jobs, cdp_port=9222, concurrency=2,
                limiter=limiter_cls.return_value)
        printed = out.getvalue()
        self.assertIn("[进度 10/12", printed, "第 10 条应出阶段汇总")
        self.assertIn("[进度 12/12", printed, "完成时应出最终汇总")

    def test_serial_progress_report_printed_per_step(self):
        module = load_module()
        list_data = {"jobs": self._sample_jobs(12)["jobs"]}

        def fake_one(job, cdp_port, stop_event=None, limiter=None, verbose=False):
            return {"ok": True, "detail": {"job_id": job["job_id"],
                                           "title": job["title"],
                                           "jd": "x" * 200},
                    "job_id": job["job_id"], "reason": "", "message": ""}

        with tempfile_profile() as paths:
            out = str(paths["cdp_profile"] / "details.json")
            with mock.patch.object(module, "_scrape_one_detail",
                                   new=fake_one), \
                    mock.patch.object(module, "load_existing_detail_ids",
                                      return_value=set()), \
                    mock.patch.object(module, "load_pending_ids",
                                      return_value={}), \
                    mock.patch.object(module.time, "sleep"), \
                    mock.patch("sys.stdout", new_callable=__import__("io").StringIO) as outbuf:
                module.scrape_details(list_data, output_path=out,
                                      cdp_port=9222, concurrency=1)
            printed = outbuf.getvalue()
            self.assertIn("[进度 10/12", printed, "串行第 10 条应出阶段汇总")
            self.assertIn("[进度 12/12", printed, "串行完成时应出最终汇总")

    # ----- 结果文件管理（--list-results / --archive）-----

    def _make_result_files(self, paths, jobs=2, details=1, pending=1, csv=1):
        root = paths["cdp_profile"]
        root.mkdir(parents=True, exist_ok=True)
        files = []
        for i in range(jobs):
            p = root / f"boss_jobs_{i}.json"
            p.write_text(json.dumps({"jobs": [{"job_id": str(i)}]}), encoding="utf-8")
            files.append(p)
        for i in range(details):
            p = root / f"boss_details_{i}.json"
            p.write_text(json.dumps([{"job_id": "a"}]), encoding="utf-8")
            files.append(p)
        if pending:
            p = root / "boss_details_0.json.pending.json"
            p.write_text(json.dumps([{"job_id": "a", "attempts": 1}]), encoding="utf-8")
            files.append(p)
        if csv:
            p = root / "boss_jobs_0.csv"
            p.write_text("a,b\n", encoding="utf-8")
            files.append(p)
        return files

    def test_list_results_classifies_files(self):
        module = load_module()
        with tempfile_profile() as paths:
            self._make_result_files(paths)
            entries = module.list_results(str(paths["cdp_profile"]))
            kinds = [e["kind"] for e in entries]
            self.assertIn("jobs", kinds)
            self.assertIn("details", kinds)
            self.assertIn("pending", kinds)
            self.assertNotIn("csv", kinds, "CSV 不单列")
            self.assertGreaterEqual(len(entries), 4)

    def test_archive_results_keeps_latest_and_moves_rest(self):
        module = load_module()
        with tempfile_profile() as paths:
            self._make_result_files(paths, jobs=3, details=2, pending=1, csv=0)
            module.archive_results(str(paths["cdp_profile"]), keep_latest=1)
            root = paths["cdp_profile"]
            archive = root / "archive"
            self.assertTrue(archive.exists(), "应创建 archive 子目录")
            jobs_left = [f for f in root.glob("boss_jobs_*.json")]
            details_left = [
                f for f in root.glob("boss_details_*.json")
                if not f.name.endswith(".pending.json")]
            self.assertEqual(len(jobs_left), 1, "jobs 保留最新 1 个")
            self.assertEqual(len(details_left), 1, "details 保留最新 1 个")
            self.assertEqual(len(list(archive.glob("boss_jobs_*.json"))), 2,
                             "其余 jobs 移入 archive")
            self.assertTrue((root / "boss_details_0.json.pending.json").exists(),
                            "pending 是活动文件，不归档")

    def test_archive_results_noop_when_nothing_to_archive(self):
        module = load_module()
        with tempfile_profile() as paths:
            self._make_result_files(paths, jobs=1, details=0, pending=0, csv=0)
            module.archive_results(str(paths["cdp_profile"]), keep_latest=1)
            self.assertFalse((paths["cdp_profile"] / "archive").exists(),
                             "无需归档时不建目录")

    # ----- --batch 批量任务编排 -----

    def _write_batch(self, paths, content):
        p = paths["cdp_profile"] / "batch.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")
        return str(p)

    def test_load_batch_config_parses_tasks_with_defaults(self):
        module = load_module()
        with tempfile_profile() as paths:
            path = self._write_batch(paths, [
                {"keyword": "AI产品经理", "city": "上海", "pages": 4, "scale": 305},
                {"keyword": "AI Agent"},
            ])
            tasks, errors = module.load_batch_config(path)
            self.assertEqual(errors, [])
            self.assertEqual(len(tasks), 2)
            self.assertEqual(tasks[0]["keyword"], "AI产品经理")
            self.assertEqual(tasks[0]["city"], "上海")
            self.assertEqual(tasks[0]["pages"], 4)
            self.assertEqual(tasks[0]["scale"], 305)
            self.assertEqual(tasks[1]["pages"], 3, "缺省页数用默认")
            self.assertEqual(tasks[1]["city"], module.DEFAULT_CITY_INPUT, "缺省城市用默认")

    def test_load_batch_config_rejects_bad_entries(self):
        module = load_module()
        with tempfile_profile() as paths:
            path = self._write_batch(paths, [
                "not-a-dict",
                {"city": "上海"},
                {"keyword": "ok", "pages": "abc"},
                {"keyword": "ok2"},
            ])
            tasks, errors = module.load_batch_config(path)
            self.assertEqual(len(tasks), 1, "仅合法任务进入列表")
            self.assertEqual(tasks[0]["keyword"], "ok2")
            self.assertEqual(len(errors), 3, "非法任务各记一条错误")

    def test_load_batch_config_missing_or_not_array(self):
        module = load_module()
        with tempfile_profile() as paths:
            missing = str(paths["cdp_profile"] / "no.json")
            tasks, errors = module.load_batch_config(missing)
            self.assertEqual(tasks, [])
            self.assertEqual(len(errors), 1)
            path = self._write_batch(paths, {"keyword": "x"})
            tasks, errors = module.load_batch_config(path)
            self.assertEqual(errors, ["配置必须是 JSON 数组（每个元素一个任务）"])

    def test_run_batch_executes_tasks_with_gaps(self):
        module = load_module()
        with tempfile_profile() as paths:
            path = self._write_batch(paths, [
                {"keyword": "AI", "city": "上海", "pages": 4, "sleep": 5},
                {"keyword": "Java", "city": "杭州", "pages": 2},
            ])
            calls = []
            with mock.patch.object(module, "scrape_list",
                                   side_effect=lambda *a, **k: calls.append((a, k))), \
                    mock.patch.object(module.time, "sleep") as sleep, \
                    mock.patch.object(module, "resolve_city") as rc, \
                    mock.patch.object(module.os, "listdir",
                                      side_effect=[
                                          ["old1.json", "old2.json"],
                                          ["old1.json", "old2.json",
                                           "boss_jobs_new1.json",
                                           "boss_jobs_new2.json"],
                                      ]):
                rc.side_effect = lambda city: (city, "101020100")
                code = module.run_batch(path)
            self.assertEqual(code, 0)
            self.assertEqual(len(calls), 2, "两个任务各抓一次列表")
            self.assertEqual(calls[0][0][0], "AI")
            self.assertEqual(calls[0][0][1], "上海")
            self.assertEqual(calls[0][0][2], 4)
            self.assertEqual(calls[0][1]["max_jobs"], None)
            self.assertEqual(sleep.call_count, 1, "两个任务之间只等一次")

    def test_run_batch_passes_max_concurrent(self):
        """--batch 透传 --max-concurrent（多 batch 进程并行时锁允许多持有者）。"""
        module = load_module()
        with tempfile_profile() as paths:
            path = self._write_batch(paths, [
                {"keyword": "AI", "city": "上海", "pages": 1},
            ])
            calls = []
            with mock.patch.object(module, "scrape_list",
                                   side_effect=lambda *a, **k: calls.append((a, k))), \
                    mock.patch.object(module.time, "sleep"), \
                    mock.patch.object(module, "resolve_city") as rc, \
                    mock.patch.object(module.os, "listdir",
                                      side_effect=[
                                          ["old1.json"],
                                          ["old1.json", "boss_jobs_new1.json"],
                                      ]):
                rc.side_effect = lambda city: (city, "101020100")
                code = module.run_batch(path, max_concurrent=2)
            self.assertEqual(code, 0)
            self.assertEqual(calls[0][1]["max_concurrent"], 2,
                             "batch 任务应透传 max_concurrent")

    def test_run_batch_integrity_check_passes_when_files_match(self):
        """batch 完成校验：任务数 = 新增文件数 → 通过。"""
        module = load_module()
        with tempfile_profile() as paths:
            path = self._write_batch(paths, [
                {"keyword": "AI", "city": "上海", "pages": 1},
                {"keyword": "Java", "city": "杭州", "pages": 1},
            ])
            with mock.patch.object(module, "scrape_list"), \
                    mock.patch.object(module.time, "sleep"), \
                    mock.patch.object(module, "resolve_city") as rc, \
                    mock.patch.object(module.os, "listdir",
                                      side_effect=[
                                          ["old1.json", "old2.json"],   # before
                                          ["old1.json", "old2.json",
                                           "boss_jobs_new1.json",
                                           "boss_jobs_new2.json"],      # after
                                      ]), \
                    mock.patch("sys.stdout",
                               new_callable=__import__("io").StringIO) as out:
                rc.side_effect = lambda city: (city, "101020100")
                code = module.run_batch(path)
            printed = out.getvalue()
            self.assertEqual(code, 0)
            self.assertIn("完整性校验通过", printed)

    def test_run_batch_integrity_check_fails_on_shortfall(self):
        """batch 完成校验：文件数 < 任务数（撞名/写盘异常）→ EXPORT_FAIL。"""
        module = load_module()
        with tempfile_profile() as paths:
            path = self._write_batch(paths, [
                {"keyword": "AI", "city": "上海", "pages": 1},
                {"keyword": "Java", "city": "杭州", "pages": 1},
            ])
            with mock.patch.object(module, "scrape_list"), \
                    mock.patch.object(module.time, "sleep"), \
                    mock.patch.object(module, "resolve_city") as rc, \
                    mock.patch.object(module.os, "listdir",
                                      side_effect=[
                                          ["old1.json"],                # before
                                          ["old1.json",
                                           "boss_jobs_new1.json"],      # after: 少一个
                                      ]), \
                    mock.patch("sys.stdout",
                               new_callable=__import__("io").StringIO) as out:
                rc.side_effect = lambda city: (city, "101020100")
                code = module.run_batch(path)
            printed = out.getvalue()
            self.assertEqual(code, 1)
            self.assertIn("EXPORT_FAIL reason=batch_shortfall", printed)
            self.assertIn("tasks_expected=2", printed)

    # ----- 宽 except 收紧（技术债 #2）-----

    def test_is_cdp_ready_lets_unexpected_errors_escape(self):
        module = load_module()
        fake_requests = mock.Mock()
        with mock.patch.object(module, "requests", fake_requests):
            fake_requests.get.side_effect = ValueError("unexpected")
            with self.assertRaises(ValueError):
                module.is_cdp_ready(9222)
            fake_requests.get.side_effect = ConnectionError
            self.assertFalse(module.is_cdp_ready(9222), "连接类错误应返回 False")
            fake_requests.get.side_effect = TimeoutError
            self.assertFalse(module.is_cdp_ready(9222))
            fake_requests.get.side_effect = None
            fake_requests.get.return_value = type("Resp", (), {"status_code": 200})()
            self.assertTrue(module.is_cdp_ready(9222))
            fake_requests.get.return_value = type("Resp", (), {"status_code": 503})()
            self.assertFalse(module.is_cdp_ready(9222))

    def test_iter_chrome_process_commands_lets_unexpected_errors_escape(self):
        module = load_module()
        with mock.patch.object(module.platform, "system",
                               return_value="Windows"), \
                mock.patch.object(module.subprocess, "run",
                                  side_effect=ValueError("boom")):
            with self.assertRaises(ValueError):
                module.iter_chrome_process_commands()
        with mock.patch.object(module.platform, "system",
                               return_value="Windows"), \
                mock.patch.object(module.subprocess, "run",
                                  side_effect=OSError("boom")):
            self.assertEqual(module.iter_chrome_process_commands(), [],
                             "OSError 应兜底返回空列表")
        with mock.patch.object(module.platform, "system",
                               return_value="Linux"), \
                mock.patch.object(module.subprocess, "run",
                                  side_effect=OSError("boom")):
            self.assertEqual(module.iter_chrome_process_commands(), [],
                             "POSIX 分支 OSError 同样兜底")

    # ----- S1 契约层（ai-pm-job-intel 规格 v1.0）-----

    REQUIRED_JOB_FIELDS = ["job_id", "title", "location", "job_link", "company_name"]

    def test_flush_jobs_emits_job_count(self):
        """规格 meta 必填 job_count（现有 total 保留兼容）。"""
        module = load_module()
        with tempfile_profile() as paths:
            target = str(paths["cdp_profile"] / "jobs.json")
            module.flush_jobs(target, {"keyword": "AI"}, [{
                "job_id": "a", "title": "T", "location": "深圳",
                "job_link": "https://www.zhipin.com/job_detail/x.html",
                "company_name": "某科技",
            }])
            with open(target, encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(data["job_count"], 1)
            self.assertEqual(data["total"], 1, "total 保留向后兼容")

    # ----- 单进程互斥（规格 §3.6 代码强制）-----

    def test_scrape_lock_acquire_and_release(self):
        module = load_module()
        with tempfile_profile() as paths:
            lock = str(paths["cdp_profile"] / "scrape.lock")
            with mock.patch.object(module, "SCRAPE_LOCK_PATH", lock):
                self.assertTrue(module.acquire_scrape_lock(), "无锁应可获取")
                self.assertTrue(os.path.exists(lock), "应创建锁文件")
                module.release_scrape_lock()
                self.assertFalse(os.path.exists(lock), "释放后锁文件删除")

    def test_scrape_lock_rejects_when_other_process_holds(self):
        module = load_module()
        with tempfile_profile() as paths:
            lock = str(paths["cdp_profile"] / "scrape.lock")
            with mock.patch.object(module, "SCRAPE_LOCK_PATH", lock), \
                    mock.patch.object(module, "_pid_is_running",
                                      return_value=True) as running:
                self.assertTrue(module.acquire_scrape_lock())
                self.assertFalse(module.acquire_scrape_lock(),
                                 "其他存活进程持锁时应拒绝")
                running.assert_called()

    def test_scrape_lock_takes_over_stale_lock(self):
        module = load_module()
        with tempfile_profile() as paths:
            lock = str(paths["cdp_profile"] / "scrape.lock")
            os.makedirs(paths["cdp_profile"], exist_ok=True)
            with open(lock, "w", encoding="utf-8") as f:
                f.write("99999")
            with mock.patch.object(module, "SCRAPE_LOCK_PATH", lock), \
                    mock.patch.object(module, "_pid_is_running",
                                      return_value=False):
                self.assertTrue(module.acquire_scrape_lock(),
                                "持锁进程已死应接管")
                with open(lock, encoding="utf-8") as f:
                    self.assertNotEqual(f.read().strip(), "99999")

    def test_scrape_lock_release_keeps_other_lock(self):
        """只释放自己的锁：锁内容不是本进程 pid 时不删除。"""
        module = load_module()
        with tempfile_profile() as paths:
            lock = str(paths["cdp_profile"] / "scrape.lock")
            os.makedirs(paths["cdp_profile"], exist_ok=True)
            with mock.patch.object(module, "SCRAPE_LOCK_PATH", lock):
                with open(lock, "w", encoding="utf-8") as f:
                    f.write("88888")
                module.release_scrape_lock()
                self.assertTrue(os.path.exists(lock), "他人锁不应被删")

    def test_scrape_lock_default_max_concurrent_is_one(self):
        """规格 §3.6 修订：默认并发上限 1（现状行为不变）。"""
        module = load_module()
        with tempfile_profile() as paths:
            lock = str(paths["cdp_profile"] / "scrape.lock")
            with mock.patch.object(module, "SCRAPE_LOCK_PATH", lock), \
                    mock.patch.object(module, "_pid_is_running",
                                      return_value=True) as running:
                self.assertTrue(module.acquire_scrape_lock())
                self.assertFalse(module.acquire_scrape_lock(),
                                 "默认上限 1：第二个存活持有者应拒绝")
                self.assertTrue(running.called)

    def test_scrape_lock_max_concurrent_allows_parallel(self):
        """--max-concurrent N：指令显式放开并发上限。"""
        module = load_module()
        with tempfile_profile() as paths:
            lock = str(paths["cdp_profile"] / "scrape.lock")
            with mock.patch.object(module, "SCRAPE_LOCK_PATH", lock), \
                    mock.patch.object(module, "_pid_is_running",
                                      return_value=True):
                self.assertTrue(module.acquire_scrape_lock(max_concurrent=3))
                self.assertTrue(module.acquire_scrape_lock(max_concurrent=3),
                                "上限 3：第二个存活持有者应允许")
                self.assertTrue(module.acquire_scrape_lock(max_concurrent=3),
                                "上限 3：第三个存活持有者应允许")
                self.assertFalse(module.acquire_scrape_lock(max_concurrent=3),
                                 "上限 3：第四个存活持有者应拒绝")

    def test_scrape_lock_pid_list_in_file(self):
        """锁文件内容为 max_concurrent 行 + 持有 pid 列表。"""
        module = load_module()
        with tempfile_profile() as paths:
            lock = str(paths["cdp_profile"] / "scrape.lock")
            with mock.patch.object(module, "SCRAPE_LOCK_PATH", lock), \
                    mock.patch.object(module, "_pid_is_running",
                                      return_value=True):
                module.acquire_scrape_lock(max_concurrent=2)
                module.acquire_scrape_lock(max_concurrent=2)
                with open(lock, encoding="utf-8") as f:
                    lines = f.read().splitlines()
                self.assertEqual(lines[0], "2", "首行应为并发上限")
                self.assertEqual(len(lines[1:]), 2, "应有 2 个持有 pid")

    def test_scrape_lock_release_only_removes_own_pid(self):
        """并发持有：释放只移除自己 pid，其他持有者保留。"""
        module = load_module()
        with tempfile_profile() as paths:
            lock = str(paths["cdp_profile"] / "scrape.lock")
            with mock.patch.object(module, "SCRAPE_LOCK_PATH", lock), \
                    mock.patch.object(module, "_pid_is_running",
                                      return_value=True), \
                    mock.patch.object(module.os, "getpid",
                                      side_effect=[100, 200, 100]):
                self.assertTrue(module.acquire_scrape_lock(max_concurrent=3))
                self.assertTrue(module.acquire_scrape_lock(max_concurrent=3))
                module.release_scrape_lock()  # 进程 100 释放
                with open(lock, encoding="utf-8") as f:
                    lines = f.read().splitlines()
                self.assertEqual(lines[0], "3")
                self.assertEqual(lines[1:], ["200"], "只剩进程 200")

    def test_scrape_lock_risk_broadcast(self):
        """熔断广播：set 后 is 返回 True，其他任务据此全停。"""
        module = load_module()
        with tempfile_profile() as paths:
            lock = str(paths["cdp_profile"] / "scrape.lock")
            with mock.patch.object(module, "SCRAPE_LOCK_PATH", lock):
                module.acquire_scrape_lock(max_concurrent=2)
                self.assertFalse(module.is_scrape_lock_risk())
                module.set_scrape_lock_risk()
                self.assertTrue(module.is_scrape_lock_risk())
                # 熔断中释放仍保留 risk 标志（挂起等人工确认）
                module.release_scrape_lock()
                self.assertTrue(module.is_scrape_lock_risk())

    def test_scrape_lock_risk_blocks_new_acquire(self):
        """熔断中（即使无存活持有者）新任务也应拒绝，必须人工确认后重开。"""
        module = load_module()
        with tempfile_profile() as paths:
            lock = str(paths["cdp_profile"] / "scrape.lock")
            with mock.patch.object(module, "SCRAPE_LOCK_PATH", lock), \
                    mock.patch.object(module, "_pid_is_running",
                                      return_value=True):
                module.acquire_scrape_lock(max_concurrent=2)
                module.set_scrape_lock_risk()
                module.release_scrape_lock()
                self.assertFalse(module.acquire_scrape_lock(max_concurrent=2),
                                 "熔断中不应允许新任务")

    def test_scrape_lock_risk_cleared_by_reset(self):
        """人工确认后 --reset-lock 清除熔断，可重新抓取。"""
        module = load_module()
        with tempfile_profile() as paths:
            lock = str(paths["cdp_profile"] / "scrape.lock")
            with mock.patch.object(module, "SCRAPE_LOCK_PATH", lock), \
                    mock.patch.object(module, "_pid_is_running",
                                      return_value=False):
                module.acquire_scrape_lock(max_concurrent=2)
                module.set_scrape_lock_risk()
                module.release_scrape_lock()
                self.assertTrue(module.is_scrape_lock_risk())
                self.assertTrue(module.clear_scrape_lock_risk(),
                                "reset-lock 应成功")
                self.assertFalse(module.is_scrape_lock_risk(),
                                 "清除后 risk 应为 False")
                self.assertTrue(module.acquire_scrape_lock(max_concurrent=2),
                                "清除后可重新获取锁")

    def test_scrape_lock_clear_keeps_other_holders(self):
        """reset-lock 仅清熔断；有其他存活持有者时锁文件保留。"""
        module = load_module()
        with tempfile_profile() as paths:
            lock = str(paths["cdp_profile"] / "scrape.lock")
            with mock.patch.object(module, "SCRAPE_LOCK_PATH", lock), \
                    mock.patch.object(module, "_pid_is_running",
                                      return_value=True), \
                    mock.patch.object(module.os, "getpid",
                                      side_effect=[100, 200]):
                module.acquire_scrape_lock(max_concurrent=2)
                module.acquire_scrape_lock(max_concurrent=2)
                module.set_scrape_lock_risk()
                self.assertTrue(module.clear_scrape_lock_risk())
                with open(lock, encoding="utf-8") as f:
                    lines = f.read().splitlines()
                self.assertEqual(lines[0], "2")
                self.assertEqual(lines[1:], ["100", "200"], "持有 pid 保留")

    def test_scrape_lock_guard_serializes_rmw(self):
        """guard 文件互斥 RMW：并发 acquire 不丢 pid（模拟进程 100/200 交错）。"""
        module = load_module()
        with tempfile_profile() as paths:
            lock = str(paths["cdp_profile"] / "scrape.lock")
            with mock.patch.object(module, "SCRAPE_LOCK_PATH", lock), \
                    mock.patch.object(module, "_pid_is_running",
                                      return_value=True), \
                    mock.patch.object(module.os, "getpid",
                                      side_effect=[100, 200, 300]):
                # 两进程先后 acquire（guard 保证串行 RMW）
                self.assertTrue(module.acquire_scrape_lock(max_concurrent=3))
                self.assertTrue(module.acquire_scrape_lock(max_concurrent=3))
                self.assertTrue(module.acquire_scrape_lock(max_concurrent=3))
                with open(lock, encoding="utf-8") as f:
                    lines = f.read().splitlines()
                self.assertEqual(lines[0], "3")
                self.assertEqual(sorted(lines[1:]), ["100", "200", "300"],
                                 "三个 pid 都应保留（无覆盖丢失）")

    def test_scrape_lock_guard_cleaned_up(self):
        """guard 文件在 acquire 完成后应删除（无残留）。"""
        module = load_module()
        with tempfile_profile() as paths:
            lock = str(paths["cdp_profile"] / "scrape.lock")
            guard = lock + ".guard"
            with mock.patch.object(module, "SCRAPE_LOCK_PATH", lock):
                self.assertTrue(module.acquire_scrape_lock())
                self.assertFalse(os.path.exists(guard),
                                 "guard 用完应删除")
                self.assertTrue(os.path.exists(lock), "锁文件保留")

    # ----- CDP 熔断冷却（#5）-----

    def test_cdp_cooldown_mark_and_check(self):
        """熔断冷却：mark 后 check 返回剩余秒数。"""
        module = load_module()
        with tempfile_profile() as paths:
            lock = str(paths["cdp_profile"] / "scrape.lock")
            with mock.patch.object(module, "SCRAPE_LOCK_PATH", lock), \
                    mock.patch.object(module.time, "time",
                                      side_effect=[1000.0, 1000.0, 1200.0]):
                self.assertIsNone(module.check_cdp_cooldown(), "未冷却返回 None")
                module.mark_cdp_cooldown(seconds=300)
                remaining = module.check_cdp_cooldown()
                self.assertIsNotNone(remaining, "冷却中应返回剩余秒数")
                self.assertGreater(remaining, 0)
                self.assertLessEqual(remaining, 300)

    def test_cdp_cooldown_expires_and_clears(self):
        """冷却到期：check 返回 None；文件保留到恢复期结束后由 check_cdp_recovery 清除。"""
        module = load_module()
        with tempfile_profile() as paths:
            lock = str(paths["cdp_profile"] / "scrape.lock")
            with mock.patch.object(module, "SCRAPE_LOCK_PATH", lock), \
                    mock.patch.object(module.time, "time",
                                      side_effect=[1000.0, 1400.0]):
                module.mark_cdp_cooldown(seconds=300)  # 冷却截止 1300，恢复期截止 1420
                self.assertIsNone(module.check_cdp_cooldown(), "到期应返回 None")
                self.assertTrue(os.path.exists(module._cdp_cooldown_path()),
                                "恢复期内文件保留（渐变恢复信息）")
            # 恢复期也结束后（time=1500）：check_cdp_recovery 清理残留
            with mock.patch.object(module, "SCRAPE_LOCK_PATH", lock), \
                    mock.patch.object(module.time, "time", return_value=1500.0):
                self.assertEqual(module.check_cdp_recovery(), 0.0)
                self.assertFalse(os.path.exists(module._cdp_cooldown_path()),
                                 "恢复期结束后清除标记文件")

    # ----- 风控码（#3）-----

    def test_login_restricted_codes_include_common_risk_codes(self):
        """风控码表含 BOSS 常用码 31/35/36/37/38（boss-jd-scraper 实测）。"""
        module = load_module()
        for code in (31, 35, 36, 37, 38):
            self.assertIn(code, module.LOGIN_RESTRICTED_CODES,
                          f"风控码 {code} 应被识别")

    # ----- 连续空页风控静默降级（#4b）-----

    def test_scrape_list_stops_after_two_empty_pages(self):
        """连续 2 页无数据（风控静默降级信号）→ EXPORT_FAIL 停止。"""
        module = load_module()
        cdp = mock.Mock()

        def fake_eval_js(script, sid=None):
            return json.dumps([])  # 全部空页

        cdp.eval_js.side_effect = fake_eval_js
        with mock.patch.object(module, "resolve_city",
                               return_value=("上海", "101020100")), \
                mock.patch.object(module, "CDPSession", return_value=cdp), \
                mock.patch.object(module, "create_page_session",
                                  return_value=("t", "s")), \
                mock.patch.object(module, "probe_risk_page", return_value={}), \
                mock.patch.object(module, "flush_jobs"), \
                mock.patch.object(module.time, "sleep"), \
                mock.patch("sys.stdout",
                           new_callable=__import__("io").StringIO) as out:
            result = module.scrape_list("AI", "上海", 3, {}, None)
        printed = out.getvalue()
        self.assertIn("EXPORT_FAIL reason=risk_blocked", printed)
        self.assertEqual(result["jobs"], [])

    def test_scrape_list_aborts_when_lock_held(self):
        module = load_module()
        with tempfile_profile() as paths:
            lock = str(paths["cdp_profile"] / "scrape.lock")
            with mock.patch.object(module, "SCRAPE_LOCK_PATH", lock), \
                    mock.patch.object(module, "acquire_scrape_lock",
                                      return_value=False), \
                    mock.patch.object(module, "resolve_city",
                                      return_value=("上海", "101020100")), \
                    mock.patch.object(module, "flush_jobs"), \
                    mock.patch("sys.stdout",
                               new_callable=__import__("io").StringIO) as out:
                result = module.scrape_list("AI", "上海", 1, {}, None)
            self.assertIn("EXPORT_FAIL reason=lock_held", out.getvalue())
            self.assertEqual(result["jobs"], [])


    def test_flush_jobs_emits_contract_meta(self):
        module = load_module()
        with tempfile_profile() as paths:
            target = str(paths["cdp_profile"] / "jobs.json")
            meta = {"keyword": "AI", "page_count": 2,
                    "warnings": ["第3页疑似空数据"]}
            module.flush_jobs(target, dict(meta), [{
                "job_id": "a", "title": "T", "location": "深圳",
                "job_link": "https://www.zhipin.com/job_detail/x.html",
                "company_name": "某科技",
            }])
            with open(target, encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(data["format_version"], 1)
            self.assertEqual(data["page_count"], 2)
            self.assertEqual(data["warnings"], ["第3页疑似空数据"])

    def test_fetch_api_template_emits_contract_fields(self):
        module = load_module()
        js = module.FETCH_API_JS_TEMPLATE
        self.assertIn("company_name: j.brandName", js)
        self.assertIn("experience: j.jobExperience", js)
        self.assertIn("education: j.jobDegree", js)
        self.assertIn("skills: j.skills", js)
        self.assertNotIn("(j.skills || []).join", js, "skills 应为数组而非拼接字符串")

    def test_retry_limit_matches_contract(self):
        module = load_module()
        self.assertEqual(module.API_ATTEMPT_LIMIT, 2, "NFR-3 最多 1 次重试")

    def test_export_jobs_cover_required_fields(self):
        module = load_module()
        with tempfile_profile() as paths:
            target = str(paths["cdp_profile"] / "jobs.json")
            module.flush_jobs(target, {"keyword": "AI"}, [{
                "job_id": "a", "title": "T", "location": "深圳 南山",
                "job_link": "https://www.zhipin.com/job_detail/x.html",
                "company_name": "某科技",
            }])
            with open(target, encoding="utf-8") as f:
                data = json.load(f)
            for job in data["jobs"]:
                for field in self.REQUIRED_JOB_FIELDS:
                    self.assertTrue(str(job.get(field) or "").strip(),
                                    f"必填字段缺失: {field}")

    def test_export_has_no_sensitive_fields(self):
        """NFR-6：导出文件不落任何登录凭据（外部数据经 --merge/--input 混入时过滤）。"""
        module = load_module()
        with tempfile_profile() as paths:
            target = str(paths["cdp_profile"] / "jobs.json")
            module.flush_jobs(target, {"keyword": "AI"}, [{
                "job_id": "a", "title": "T", "location": "深圳",
                "job_link": "https://www.zhipin.com/job_detail/x.html",
                "company_name": "某科技",
                "cookie": "secret", "token": "t", "wt2": "x",
                "zp_stoken": "y", "password": "p",
            }])
            with open(target, encoding="utf-8") as f:
                raw = f.read()
            for secret in ("cookie", "token", "wt2", "zp_stoken", "password"):
                self.assertNotIn(secret, raw.lower())

    def test_export_excludes_internal_boss_ids(self):
        """规格侧建议：security_id/lid/encrypt_* 等 BOSS 内部标识不落导出文件
        （下游误读风险，且非契约字段）。"""
        module = load_module()
        with tempfile_profile() as paths:
            target = str(paths["cdp_profile"] / "jobs.json")
            module.flush_jobs(target, {"keyword": "AI"}, [{
                "job_id": "a", "title": "T", "location": "深圳",
                "job_link": "https://www.zhipin.com/job_detail/x.html",
                "company_name": "某科技",
                "security_id": "sec", "lid": "lid-1",
                "encrypt_job_id": "ej", "encrypt_boss_id": "eb",
                "encrypt_brand_id": "er",
            }])
            with open(target, encoding="utf-8") as f:
                data = json.load(f)
            job = data["jobs"][0]
            for internal in ("security_id", "lid", "encrypt_job_id",
                             "encrypt_boss_id", "encrypt_brand_id"):
                self.assertNotIn(internal, job, f"内部标识不应导出: {internal}")
            self.assertEqual(job["job_link"],
                             "https://www.zhipin.com/job_detail/x.html",
                             "job_link 是公开信息保留")

    # ----- 双端契约一致校验（消费端校验器 vendor 副本 v1.0.0）-----

    CONSUMER_VALIDATOR = (
        pathlib.Path(__file__).resolve().parents[0]
        / "fixtures" / "consumer_validator" / "scripts" / "validate_export.py"
    )
    CONTRACT_FIXTURE = (
        pathlib.Path(__file__).resolve().parents[0] / "fixtures" / "sample_export_v1.json"
    )

    def test_vendor_validator_fixture_files_exist(self):
        self.assertTrue(self.CONSUMER_VALIDATOR.is_file(),
                        "消费端校验器 vendor 副本缺失")
        self.assertTrue(self.CONTRACT_FIXTURE.is_file(),
                        "契约样例 fixture 缺失")

    def test_vendor_validator_passes_contract_fixture(self):
        """双端一致：消费端校验器（vendor 副本 v1.0.0）须通过我方契约样例，
        且输出行含 v1.0.0（版本漂移即失败，触发与 ai-pm-job-intel 对齐）。"""
        result = subprocess.run(
            [sys.executable, str(self.CONSUMER_VALIDATOR), str(self.CONTRACT_FIXTURE)],
            capture_output=True, text=True, encoding="utf-8", timeout=60,
        )
        out = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, f"校验器退出码非 0:\n{out}")
        self.assertIn("validator=v1.0.0", out,
                      f"校验器版本不匹配（需与 ai-pm-job-intel 对齐）:\n{out}")
        self.assertIn("ok=True", out, f"样例未通过消费端契约校验:\n{out}")


    def test_scrape_list_emits_export_ok_line(self):
        module = load_module()
        cdp = mock.Mock()
        stats = {"api_pages": []}

        def fake_eval_js(script, sid=None):
            if "xhr.open" not in script:
                return None
            m = re.search(r"page=(\d+)", script)
            pg = int(m.group(1)) if m else 1
            stats["api_pages"].append(pg)
            if pg > 1:
                return json.dumps([])
            jobs = [
                {"title": f"AI岗位-{i}",
                 "job_link": f"https://www.zhipin.com/job/{i}",
                 "salary": "20-40K", "boss_name": f"公司{i}"}
                for i in range(3)
            ]
            return json.dumps(jobs)

        cdp.eval_js.side_effect = fake_eval_js
        with mock.patch.object(module, "resolve_city",
                               return_value=("深圳", "101280600")), \
                mock.patch.object(module, "CDPSession", return_value=cdp), \
                mock.patch.object(module, "create_page_session",
                                  return_value=("t", "s")), \
                mock.patch.object(module, "probe_risk_page", return_value={}), \
                mock.patch.object(module, "flush_jobs"), \
                mock.patch.object(module.time, "sleep"), \
                mock.patch("sys.stdout", new_callable=__import__("io").StringIO) as out:
            result = module.scrape_list("AI", "深圳", 2, {}, None)
        printed = out.getvalue()
        self.assertIn("EXPORT_OK jobs=3", printed)
        self.assertIn("city=深圳", printed)
        self.assertIn("keyword=AI", printed)
        self.assertTrue(result["jobs"], "导出数据仍正常返回")

    # ----- DoD 验收（规格 §4）-----

    def test_dod4_idempotent_repeat_flush_no_growth(self):
        """DoD 4 幂等：同日重复写入同一数据 → job_id 集合一致、文件不膨胀。"""
        module = load_module()
        with tempfile_profile() as paths:
            target = str(paths["cdp_profile"] / "jobs.json")
            jobs = [{"job_id": f"job-{i}", "title": f"T{i}", "location": "深圳",
                     "job_link": f"https://www.zhipin.com/job_detail/job-{i}.html",
                     "company_name": "某科技"} for i in range(10)]
            module.flush_jobs(target, {"keyword": "AI"}, jobs)
            with open(target, encoding="utf-8") as f:
                first = json.load(f)
            module.flush_jobs(target, {"keyword": "AI"}, jobs)
            with open(target, encoding="utf-8") as f:
                second = json.load(f)
            self.assertEqual([j["job_id"] for j in first["jobs"]],
                             [j["job_id"] for j in second["jobs"]],
                             "重复写入 job_id 集合一致")
            self.assertEqual(len(second["jobs"]), 10, "不膨胀")
            self.assertEqual(second["job_count"], 10)

    def test_dod5_login_failure_exits_nonzero(self):
        """DoD 5 失败信号：登录失效 → 非零退出码 + 明确报错。"""
        module = load_module()
        UNAUTH = module.LoginProbeResult(module.LoginProbeStatus.UNAUTHENTICATED)
        with mock.patch.object(sys, "argv", [
                "boss_cdp_raw.py", "--keyword", "AI", "--city", "上海",
        ]), \
                mock.patch.object(module, "require_runtime_dependencies",
                                  return_value=True), \
                mock.patch.object(module, "resolve_city",
                                  return_value=("上海", "101020100")), \
                mock.patch.object(module, "check_login_state",
                                  return_value=UNAUTH), \
                mock.patch.object(module, "scrape_list") as scrape, \
                redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as exit_context:
                module.main()
        self.assertEqual(exit_context.exception.code, 1, "登录失败应非零退出")
        self.assertIn("未检测到 BOSS直聘登录状态", output.getvalue())
        scrape.assert_not_called(), "登录失败时零请求发出"

    def test_login_failure_sends_alert(self):
        """登录失效推送：UNAUTH/RESTRICTED/RESPONSE_ERROR → send_alert 携带 EXPORT_FAIL reason=login_failed。"""
        module = load_module()
        for status in (module.LoginProbeStatus.UNAUTHENTICATED,
                       module.LoginProbeStatus.RESTRICTED,
                       module.LoginProbeStatus.RESPONSE_ERROR):
            with self.subTest(status=status):
                result = module.LoginProbeResult(status)
                with mock.patch.object(sys, "argv", [
                        "boss_cdp_raw.py", "--keyword", "AI", "--city", "上海",
                ]), \
                        mock.patch.object(module, "require_runtime_dependencies",
                                          return_value=True), \
                        mock.patch.object(module, "resolve_city",
                                          return_value=("上海", "101020100")), \
                        mock.patch.object(module, "check_login_state",
                                          return_value=result), \
                        mock.patch.object(module, "send_alert") as alert, \
                        mock.patch.object(module, "scrape_list") as scrape, \
                        redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit) as exit_context:
                        module.main()
                self.assertEqual(exit_context.exception.code, 1)
                alert.assert_called_once()
                title, text = alert.call_args[0]
                self.assertEqual(title, "登录失效")
                self.assertIn("reason=login_failed", text)
                self.assertIn("city=上海", text)
                self.assertIn(status.name, text)
                scrape.assert_not_called()

    def test_dod6_risk_compliance_no_bypass_no_high_frequency(self):
        """DoD 6 风控合规：源码自查——无 headless 伪装、无验证码绕过、
        页间等待有下限（低频随机节奏）。"""
        source = (ROOT_PATH / "scripts" / "boss_cdp_raw.py").read_text(encoding="utf-8")
        self.assertNotIn("headless", source.lower(), "禁止 headless 伪装")
        self.assertIn("wait_for_risk_clear", source, "验证码必须人工介入")
        self.assertIn("random.uniform(12, 22)", source, "翻页间隔 12-22s 随机")
        self.assertNotIn("--disable-web-security", source)

    # ----- 详情会话失败防护 -----

    def _sample_jobs(self, n=3):
        return {"jobs": [
            {"job_id": f"job-{i}", "title": f"t{i}", "job_link": f"https://x/{i}"}
            for i in range(n)
        ]}

    def test_scrape_details_records_pending_and_continues_on_session_failure(self):
        module = load_module()
        with tempfile_profile() as paths:
            out = str(paths["cdp_profile"] / "details.json")
            with mock.patch.object(module, "CDPSession",
                                   side_effect=TimeoutError("cdp down")), \
                    mock.patch.object(module.time, "sleep"):
                results = module.scrape_details(self._sample_jobs(2), output_path=out)
            self.assertEqual(results, [], "单条会话失败不应崩溃，应跳过并记录")
            self.assertEqual(module.load_pending_ids(out), {"job-0": 1, "job-1": 1})

    def test_scrape_details_breaks_after_consecutive_session_failures(self):
        module = load_module()
        with tempfile_profile() as paths:
            out = str(paths["cdp_profile"] / "details.json")
            # SCRAPE_LOCK_PATH mock 到临时路径：熔断冷却文件也落在临时目录（不污染真实 ~/.boss-zhipin-scraper）
            real_cooldown = (pathlib.Path.home() / ".boss-zhipin-scraper"
                             / "cdp.cooldown")
            existed_before = real_cooldown.exists()
            with mock.patch.object(module, "SCRAPE_LOCK_PATH",
                                   str(paths["cdp_profile"] / "scrape.lock")), \
                    mock.patch.object(module, "CDPSession",
                                      side_effect=TimeoutError("cdp down")), \
                    mock.patch.object(module.time, "sleep"):
                # 5 个 job、阈值 3 → 连续 3 次失败后熔断停止
                results = module.scrape_details(self._sample_jobs(5), output_path=out)
            self.assertEqual(results, [])
            self.assertEqual(len(module.load_pending_ids(out)), 3,
                             "熔断后不应继续尝试剩余 job")
            # 冷却文件应落在 mock 的临时锁目录，不新增真实目录文件
            tmp_cooldown = pathlib.Path(paths["cdp_profile"]) / "cdp.cooldown"
            self.assertTrue(tmp_cooldown.exists(),
                            "冷却文件应写 mock 的临时锁目录")
            self.assertEqual(real_cooldown.exists(), existed_before,
                             "真实目录冷却文件不应被测试新增")

    # ----- 并发详情抓取：全局限速令牌桶 -----

    def test_token_bucket_allows_burst_up_to_capacity(self):
        module = load_module()
        bucket = module.TokenBucket(rate=2.0, capacity=2)
        with mock.patch.object(module.time, "sleep") as sleep_mock, \
                mock.patch.object(module.time, "time", side_effect=[0.0, 0.1, 0.2]):
            bucket.acquire()
            bucket.acquire()
        sleep_mock.assert_not_called(), "容量内不应阻塞"

    def test_token_bucket_blocks_when_depleted(self):
        module = load_module()
        bucket = module.TokenBucket(rate=1.0, capacity=1)
        with mock.patch.object(module.time, "sleep") as sleep_mock, \
                mock.patch.object(module.time, "time", side_effect=[0.0, 0.1, 1.1]):
            bucket.acquire()   # 消耗唯一令牌
            bucket.acquire()   # 需要等 1 秒补充
        sleep_mock.assert_called_once()
        self.assertGreaterEqual(sleep_mock.call_args[0][0], 0.9,
                                "等待时长应覆盖令牌补充间隔")

    def test_token_bucket_accumulates_tokens_over_time(self):
        module = load_module()
        bucket = module.TokenBucket(rate=2.0, capacity=10)
        with mock.patch.object(module.time, "sleep"), \
                mock.patch.object(module.time, "time", side_effect=[0.0, 5.0]):
            # 5 秒空闲后应有 10 个令牌（受容量上限）
            bucket.acquire()
        # 无阻塞即说明令牌已按速率累计
        self.assertTrue(True)

    def test_adaptive_limiter_halves_rate_on_high_failure_window(self):
        module = load_module()
        limiter = module.AdaptiveRateLimiter(base_rate=4.0, window=60.0,
                                             failure_threshold=0.3,
                                             pause_seconds=60.0)
        # 窗口内 10 次请求 4 次失败（40% > 30%）
        for _ in range(6):
            limiter.record_success()
        for _ in range(4):
            limiter.record_failure()
        limiter._roll_window()
        self.assertEqual(limiter.current_rate(), 2.0, "失败率超阈值后速率应降半")

    def test_adaptive_limiter_pauses_after_consecutive_bad_windows(self):
        module = load_module()
        limiter = module.AdaptiveRateLimiter(base_rate=4.0, window=60.0,
                                             failure_threshold=0.3,
                                             pause_seconds=30.0)
        times = iter([0.0, 0.1, 0.2, 1.0, 59.0, 59.1, 59.2, 60.0, 60.1, 60.2])
        with mock.patch.object(module.time, "sleep") as sleep_mock, \
                mock.patch.object(module.time, "time", side_effect=lambda: next(times)):
            # 两个连续坏窗口
            for _ in range(2):
                limiter.record_failure()
                limiter.record_failure()
                limiter.record_failure()
                limiter._roll_window()
            limiter.acquire()  # 应触发暂停（至少 30s）
        self.assertGreaterEqual(sleep_mock.call_args[0][0], 29.0,
                                "连续坏窗口后应暂停约 pause_seconds")

    def test_adaptive_limiter_recovers_after_healthy_window(self):
        module = load_module()
        limiter = module.AdaptiveRateLimiter(base_rate=4.0, window=60.0,
                                             failure_threshold=0.3,
                                             pause_seconds=60.0)
        limiter._halved = True
        for _ in range(10):
            limiter.record_success()
        limiter._roll_window()
        self.assertEqual(limiter.current_rate(), 4.0, "健康窗口后应恢复基线速率")

    # ----- 并发详情抓取：单详情 worker 单元 -----

    @contextlib.contextmanager
    def _mock_detail_page(self, module, jd_text):
        """构造 mock 的详情页环境：CDPSession + 干净页面 + 可提取内容。"""
        ws = mock.Mock()
        detail_url = "https://www.zhipin.com/job_detail/abc.html"
        good = json.dumps({
            "jd": f"职位描述\n{jd_text}",
            "page_text": f"职位描述\n{jd_text}",
            "tags": ["Python"],
            "url": detail_url,
        })
        ws.eval_js.return_value = good
        with mock.patch.object(module, "CDPSession", return_value=ws), \
                mock.patch.object(module, "create_page_session",
                                  return_value=("tid-1", "sid-1")), \
                mock.patch.object(module, "probe_risk_page", return_value={}), \
                mock.patch.object(module, "classify_risk_page",
                                  return_value=(False, "")), \
                mock.patch.object(module.time, "sleep"), \
                mock.patch.object(module.random, "uniform", return_value=1.0), \
                mock.patch.object(module.random, "randint", return_value=3), \
                mock.patch.object(module.random, "random", return_value=0.1):
            yield ws, detail_url

    def _detail_job(self, job_id="job-1"):
        return {
            "job_id": job_id,
            "title": "AI产品经理",
            "boss_name": "某公司",
            "salary": "30-60K",
            "salary_source": "api",
            "location": "杭州·滨江区",
            "tags": "3-5年 | 本科",
            "job_link": "https://www.zhipin.com/job_detail/abc.html",
            "boss_active_status": "在线",
        }

    def test_scrape_one_detail_success_path(self):
        module = load_module()
        with self._mock_detail_page(module, "Build AI agents " * 20) as (_ws, _url):
            result = module._scrape_one_detail(self._detail_job(), cdp_port=9222)
        self.assertTrue(result["ok"])
        self.assertEqual(result["job_id"], "job-1")
        self.assertIn("AI agents", result["detail"]["jd"])
        self.assertEqual(result["detail"]["title"], "AI产品经理")
        self.assertEqual(result["detail"]["boss_active_status"], "在线")
        self.assertEqual(result["reason"], "")

    def test_scrape_one_detail_reports_cdp_session_failure(self):
        module = load_module()
        with mock.patch.object(module, "CDPSession",
                               side_effect=TimeoutError("cdp down")), \
                mock.patch.object(module.time, "sleep"):
            result = module._scrape_one_detail(self._detail_job(), cdp_port=9222)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "cdp_session")
        self.assertEqual(result["detail"], None)

    def test_scrape_one_detail_reports_risk_timeout(self):
        module = load_module()
        ws = mock.Mock()
        with mock.patch.object(module, "CDPSession", return_value=ws), \
                mock.patch.object(module, "create_page_session",
                                  return_value=("tid-1", "sid-1")), \
                mock.patch.object(module, "probe_risk_page", return_value={}), \
                mock.patch.object(module, "classify_risk_page",
                                  return_value=(True, "滑块验证")), \
                mock.patch.object(module, "wait_for_risk_clear",
                                  return_value=False), \
                mock.patch.object(module.time, "sleep"), \
                mock.patch.object(module.random, "uniform", return_value=1.0), \
                mock.patch.object(module.random, "randint", return_value=3), \
                mock.patch.object(module.random, "random", return_value=0.1):
            result = module._scrape_one_detail(self._detail_job(), cdp_port=9222)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "risk_timeout")

    def test_scrape_one_detail_reports_invalid_detail(self):
        module = load_module()
        ws = mock.Mock()
        short = json.dumps({"jd": "职位描述\n太短", "page_text": "", "tags": [],
                            "url": "https://x"})
        ws.eval_js.return_value = short
        with mock.patch.object(module, "CDPSession", return_value=ws), \
                mock.patch.object(module, "create_page_session",
                                  return_value=("tid-1", "sid-1")), \
                mock.patch.object(module, "probe_risk_page", return_value={}), \
                mock.patch.object(module, "classify_risk_page",
                                  return_value=(False, "")), \
                mock.patch.object(module.time, "sleep"), \
                mock.patch.object(module.random, "uniform", return_value=1.0), \
                mock.patch.object(module.random, "randint", return_value=3), \
                mock.patch.object(module.random, "random", return_value=0.1):
            result = module._scrape_one_detail(self._detail_job(), cdp_port=9222)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "invalid_detail")

    def test_scrape_one_detail_reports_login_required(self):
        module = load_module()
        ws = mock.Mock()
        wall = json.dumps({"jd": "", "page_text": "登录查看完整内容", "tags": [],
                           "url": "https://x"})
        ws.eval_js.return_value = wall
        with mock.patch.object(module, "CDPSession", return_value=ws), \
                mock.patch.object(module, "create_page_session",
                                  return_value=("tid-1", "sid-1")), \
                mock.patch.object(module, "probe_risk_page", return_value={}), \
                mock.patch.object(module, "classify_risk_page",
                                  return_value=(False, "")), \
                mock.patch.object(module.time, "sleep"), \
                mock.patch.object(module.random, "uniform", return_value=1.0), \
                mock.patch.object(module.random, "randint", return_value=3), \
                mock.patch.object(module.random, "random", return_value=0.1):
            result = module._scrape_one_detail(self._detail_job(), cdp_port=9222)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "login_required")

    def test_scrape_one_detail_respects_stop_event(self):
        module = load_module()
        stop_event = threading.Event()
        stop_event.set()
        with mock.patch.object(module, "CDPSession",
                               side_effect=AssertionError("不应建立会话")), \
                mock.patch.object(module.time, "sleep"):
            result = module._scrape_one_detail(self._detail_job(),
                                               cdp_port=9222,
                                               stop_event=stop_event)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "stopped")

    def test_scrape_one_detail_verbose_prints_progress(self):
        module = load_module()
        with self._mock_detail_page(module, "Build AI agents " * 20) as (_ws, _url), \
                mock.patch("builtins.print") as print_mock:
            module._scrape_one_detail(self._detail_job(), cdp_port=9222, verbose=True)
        texts = " ".join(
            str(c[0][0]) for c in print_mock.call_args_list if c[0]
        )
        self.assertIn("加载页面", texts)
        self.assertIn("模拟滚动", texts)

    def test_scrape_one_detail_non_verbose_stays_quiet(self):
        module = load_module()
        with self._mock_detail_page(module, "Build AI agents " * 20) as (_ws, _url), \
                mock.patch("builtins.print") as print_mock:
            module._scrape_one_detail(self._detail_job(), cdp_port=9222, verbose=False)
        texts = " ".join(
            str(c[0][0]) for c in print_mock.call_args_list if c[0]
        )
        self.assertNotIn("模拟滚动", texts)

    # ----- 并发详情抓取：并行执行层 -----

    def _fake_parallel_worker(self, active_state, ok_ids, fail_ids=(),
                              fail_reason="invalid_detail"):
        """构造模拟 worker：记录并发峰值，按 job_id 返回成功/失败。"""
        lock, active, max_active = active_state

        def worker(job, cdp_port, stop_event=None, limiter=None):
            if stop_event is not None and stop_event.is_set():
                return {"ok": False, "detail": None, "job_id": job["job_id"],
                        "reason": "stopped", "message": ""}
            with lock:
                active[0] += 1
                max_active[0] = max(max_active[0], active[0])
            # 注意：不能用 time.sleep（测试会 mock module.time.sleep，time 是单例模块，
            # 会连测试代码里的 sleep 一起 mock 掉）；用 Event.wait 实现真实阻塞
            threading.Event().wait(0.15)
            with lock:
                active[0] -= 1
            if job["job_id"] in fail_ids:
                return {"ok": False, "detail": None, "job_id": job["job_id"],
                        "reason": fail_reason, "message": "boom"}
            return {"ok": True, "detail": {"job_id": job["job_id"],
                                           "title": job["title"], "jd": "x" * 200},
                    "job_id": job["job_id"], "reason": "", "message": ""}
        return worker

    def test_parallel_respects_concurrency_limit_and_collects_results(self):
        module = load_module()
        jobs = self._sample_jobs(5)["jobs"]
        state = (threading.Lock(), [0], [0])
        with mock.patch.object(module, "_scrape_one_detail",
                               new=self._fake_parallel_worker(state, set())), \
                mock.patch.object(module, "AdaptiveRateLimiter") as limiter_cls, \
                mock.patch.object(module, "load_existing_detail_ids", return_value=set()), \
                mock.patch.object(module, "load_pending_ids", return_value=set()), \
                mock.patch.object(module.time, "sleep"):
            results, pending_out = module._scrape_details_parallel(
                jobs, cdp_port=9222, concurrency=2,
                limiter=limiter_cls.return_value)
        self.assertEqual(len(results), 5, "全部成功应收集 5 条")
        self.assertLessEqual(state[2][0], 2, "并发峰值不应超过 concurrency")
        self.assertEqual(state[2][0], 2, "5 个任务在 2 并发下应出现并发峰值 2")
        self.assertEqual(pending_out, {})

    def test_parallel_records_failed_jobs_to_pending(self):
        module = load_module()
        jobs = self._sample_jobs(4)["jobs"]
        state = (threading.Lock(), [0], [0])
        with mock.patch.object(module, "_scrape_one_detail",
                               new=self._fake_parallel_worker(
                                   state, set(),
                                   fail_ids={"job-1", "job-2"},
                                   fail_reason="cdp_session")), \
                mock.patch.object(module, "AdaptiveRateLimiter") as limiter_cls, \
                mock.patch.object(module, "load_existing_detail_ids", return_value=set()), \
                mock.patch.object(module, "load_pending_ids", return_value=set()), \
                mock.patch.object(module.time, "sleep"):
            results, pending_out = module._scrape_details_parallel(
                jobs, cdp_port=9222, concurrency=3,
                limiter=limiter_cls.return_value)
        self.assertEqual(len(results), 2, "失败的 job 不应写入结果")
        self.assertEqual(pending_out, {"job-1": 1, "job-2": 1})

    def test_parallel_invalid_detail_not_recorded_to_pending(self):
        """#8 失败分类：解析类失败（invalid_detail）不进 pending（重试浪费且掩盖漂移）。"""
        module = load_module()
        jobs = self._sample_jobs(3)["jobs"]
        state = (threading.Lock(), [0], [0])
        with mock.patch.object(module, "_scrape_one_detail",
                               new=self._fake_parallel_worker(
                                   state, set(),
                                   fail_ids={"job-1"},
                                   fail_reason="invalid_detail")), \
                mock.patch.object(module, "AdaptiveRateLimiter") as limiter_cls, \
                mock.patch.object(module, "load_existing_detail_ids", return_value=set()), \
                mock.patch.object(module, "load_pending_ids", return_value=set()), \
                mock.patch.object(module.time, "sleep"):
            results, pending_out = module._scrape_details_parallel(
                jobs, cdp_port=9222, concurrency=3,
                limiter=limiter_cls.return_value)
        self.assertEqual(len(results), 2, "成功的 job 正常收集")
        self.assertEqual(pending_out, {}, "invalid_detail 不应进 pending")

    def test_parallel_stops_submitting_after_global_stop(self):
        module = load_module()
        jobs = self._sample_jobs(6)["jobs"]

        def always_fail(job, cdp_port, stop_event=None, limiter=None):
            return {"ok": False, "detail": None, "job_id": job["job_id"],
                    "reason": "cdp_session", "message": "boom"}

        with mock.patch.object(module, "_scrape_one_detail",
                               new=always_fail), \
                mock.patch.object(module, "SCRAPE_LOCK_PATH",
                                  str(pathlib.Path(tempfile.mkdtemp())
                                      / "scrape.lock")), \
                mock.patch.object(module, "AdaptiveRateLimiter") as limiter_cls, \
                mock.patch.object(module, "load_existing_detail_ids", return_value=set()), \
                mock.patch.object(module, "load_pending_ids", return_value=set()), \
                mock.patch.object(module.time, "sleep"):
            results, pending_out = module._scrape_details_parallel(
                jobs, cdp_port=9222, concurrency=3,
                limiter=limiter_cls.return_value)
        self.assertEqual(results, [])
        # 连续失败熔断后不再执行剩余任务（部分任务可能已提交）
        self.assertLessEqual(len(pending_out), 6)

    def test_parallel_bounded_submission_after_circuit_break(self):
        module = load_module()
        # 20 个任务、并发 2 → 提交窗口 4；熔断后不应把剩余任务全部提交
        jobs = self._sample_jobs(20)["jobs"]
        calls = []

        def always_fail(job, cdp_port, stop_event=None, limiter=None):
            calls.append(job["job_id"])
            return {"ok": False, "detail": None, "job_id": job["job_id"],
                    "reason": "cdp_session", "message": "boom"}

        with mock.patch.object(module, "_scrape_one_detail",
                               new=always_fail), \
                mock.patch.object(module, "SCRAPE_LOCK_PATH",
                                  str(pathlib.Path(tempfile.mkdtemp())
                                      / "scrape.lock")), \
                mock.patch.object(module, "AdaptiveRateLimiter") as limiter_cls, \
                mock.patch.object(module, "load_existing_detail_ids",
                                  return_value=set()), \
                mock.patch.object(module, "load_pending_ids",
                                  return_value={}), \
                mock.patch.object(module.time, "sleep"):
            module._scrape_details_parallel(
                jobs, cdp_port=9222, concurrency=2,
                limiter=limiter_cls.return_value)
        self.assertLess(len(calls), 20,
                        "熔断后不应继续提交剩余任务（有界提交窗口）")
        self.assertGreaterEqual(len(calls), 3, "至少执行到熔断阈值")

    def test_parallel_counts_requests_against_budget(self):
        module = load_module()
        jobs = self._sample_jobs(3)["jobs"]
        state = (threading.Lock(), [0], [0])
        with mock.patch.object(module, "_scrape_one_detail",
                               new=self._fake_parallel_worker(state, set())), \
                mock.patch.object(module, "AdaptiveRateLimiter") as limiter_cls, \
                mock.patch.object(module, "incr_request") as incr_mock, \
                mock.patch.object(module, "load_existing_detail_ids",
                                  return_value=set()), \
                mock.patch.object(module, "load_pending_ids",
                                  return_value={}), \
                mock.patch.object(module.time, "sleep"):
            module._scrape_details_parallel(
                jobs, cdp_port=9222, concurrency=2,
                limiter=limiter_cls.return_value)
        self.assertEqual(incr_mock.call_count, 3,
                         "每个提交的详情都应计入全局请求预算（与串行一致）")

    def test_parallel_writes_progressively_and_keeps_existing(self):
        module = load_module()
        jobs = self._sample_jobs(5)["jobs"]
        state = (threading.Lock(), [0], [0])
        old_detail = {"job_id": "old", "title": "old", "jd": "x" * 200}
        with tempfile_profile() as paths:
            out = str(paths["cdp_profile"] / "details.json")
            with mock.patch.object(module, "_scrape_one_detail",
                                   new=self._fake_parallel_worker(state, set())), \
                    mock.patch.object(module, "AdaptiveRateLimiter") as limiter_cls, \
                    mock.patch.object(module, "_atomic_write_json",
                                      wraps=module._atomic_write_json) as atomic, \
                    mock.patch.object(module, "load_existing_detail_ids",
                                      return_value=set()), \
                    mock.patch.object(module, "load_pending_ids",
                                      return_value=set()), \
                    mock.patch.object(module.time, "sleep"):
                results, pending_out = module._scrape_details_parallel(
                    jobs, cdp_port=9222, concurrency=2,
                    limiter=limiter_cls.return_value,
                    existing_results=[old_detail], output_path=out, write_every=2)
            self.assertEqual(len(results), 6, "返回应包含已有 + 本次新增")
            self.assertGreaterEqual(atomic.call_count, 3,
                                    "write_every=2、5 个任务应至少 3 次渐进写盘")
            with open(out, "r", encoding="utf-8") as f:
                saved = json.load(f)
            self.assertEqual(len(saved), 6, "落盘文件应含已有 + 本次新增")

    def test_parallel_prints_progress_per_completion(self):
        module = load_module()
        jobs = self._sample_jobs(3)["jobs"]
        state = (threading.Lock(), [0], [0])
        with mock.patch.object(module, "_scrape_one_detail",
                               new=self._fake_parallel_worker(state, set())), \
                mock.patch.object(module, "AdaptiveRateLimiter") as limiter_cls, \
                mock.patch.object(module, "load_existing_detail_ids",
                                  return_value=set()), \
                mock.patch.object(module, "load_pending_ids",
                                  return_value=set()), \
                mock.patch.object(module.time, "sleep"), \
                mock.patch("builtins.print") as print_mock:
            module._scrape_details_parallel(
                jobs, cdp_port=9222, concurrency=2,
                limiter=limiter_cls.return_value)
        progress_lines = [c[0][0] for c in print_mock.call_args_list
                          if isinstance(c[0][0], str) and "并发" in c[0][0]]
        self.assertGreaterEqual(len(progress_lines), 3, "每个任务完成都应打印进度")

    # ----- 并发详情抓取：CLI 分派 -----

    def test_scrape_details_uses_parallel_path_when_concurrency_gt_1(self):
        module = load_module()
        list_data = {"jobs": self._sample_jobs(3)["jobs"]}
        fake_detail = {"job_id": "job-0", "title": "t0", "jd": "x" * 200}
        with tempfile_profile() as paths:
            out = str(paths["cdp_profile"] / "details.json")
            captured = {}

            def fake_parallel(jobs, cdp_port, concurrency, limiter=None,
                              existing_ids=None, pending_ids=None,
                              existing_results=None, output_path=None,
                              write_every=5, list_output_path=None,
                              keyword="", city=""):
                # 模拟真实并发层的落盘行为（写盘在并行层内部完成）
                captured["concurrency"] = concurrency
                merged = list(existing_results or []) + [fake_detail]
                module._atomic_write_json(output_path, merged)
                return merged, set()

            with mock.patch.object(module, "_scrape_details_parallel",
                                   new=fake_parallel), \
                    mock.patch.object(module, "load_existing_detail_ids",
                                      return_value=set()), \
                    mock.patch.object(module, "load_pending_ids",
                                      return_value=set()), \
                    mock.patch.object(module.time, "sleep"):
                results = module.scrape_details(list_data, output_path=out,
                                                cdp_port=9222, concurrency=3)
            self.assertEqual(len(results), 1, "并发路径应返回新抓详情（与已有合并）")
            self.assertEqual(captured["concurrency"], 3, "并发度应透传")
            # 并发结果应已落盘
            with open(out, "r", encoding="utf-8") as f:
                saved = json.load(f)
            self.assertEqual(len(saved), 1)

    def test_scrape_details_keeps_serial_path_when_concurrency_is_1(self):
        module = load_module()
        list_data = {"jobs": self._sample_jobs(1)["jobs"]}
        with tempfile_profile() as paths:
            out = str(paths["cdp_profile"] / "details.json")
            with mock.patch.object(module, "_scrape_details_parallel") as parallel, \
                    mock.patch.object(module, "_scrape_one_detail",
                                      return_value={"ok": False, "detail": None,
                                                    "job_id": "job-0",
                                                    "reason": "cdp_session",
                                                    "message": "boom"}) as worker, \
                    mock.patch.object(module, "CDPSession",
                                      side_effect=TimeoutError("cdp down")), \
                    mock.patch.object(module, "load_existing_detail_ids",
                                      return_value=set()), \
                    mock.patch.object(module, "load_pending_ids",
                                      return_value={}), \
                    mock.patch.object(module.time, "sleep"):
                module.scrape_details(list_data, output_path=out,
                                      cdp_port=9222, concurrency=1)
            parallel.assert_not_called(), "默认并发 1 不应走并发路径"
            worker.assert_called_once()

    def test_wait_for_login_explicitly_uses_foreground_target(self):
        module = load_module()
        cdp = mock.Mock()
        with mock.patch.object(module, "CDPSession", return_value=cdp), \
                mock.patch.object(
                    module,
                    "create_page_session",
                    return_value=("login-target", "login-session"),
                ) as create_session, \
                mock.patch.object(
                    module,
                    "probe_login_state",
                    return_value=module.LoginProbeResult(module.LoginProbeStatus.AVAILABLE),
                ):
            self.assertTrue(module.wait_for_login(cdp_port=9333, timeout=1))

        create_session.assert_called_once_with(
            cdp,
            background=False,
        )
        self.assertEqual(
            cdp.send.call_args_list,
            [
                mock.call(
                    "Page.navigate",
                    {"url": "https://www.zhipin.com/web/user/"},
                    "login-session",
                ),
                mock.call(
                    "Target.closeTarget",
                    {"targetId": "login-target"},
                ),
            ],
        )
        cdp.close.assert_called_once_with()

    def test_default_city_is_shanghai_when_not_provided(self):
        module = load_module()

        self.assertEqual(module.DEFAULT_CITY_INPUT, "上海")
        self.assertEqual(module.resolve_city(module.DEFAULT_CITY_INPUT), ("上海", "101020100"))

    # ----- 本地静态城市码表（data/city_codes.json，见 issue #24）-----

    def test_local_city_map_loads_and_valid(self):
        """本地码表能加载、是字典、非空、value 全是数字字符串。"""
        module = load_module()
        name_to_code, code_to_name = module.load_local_city_map()

        self.assertIsInstance(name_to_code, dict)
        self.assertGreater(len(name_to_code), 100, "码表应包含上百个城市")
        for name, code in name_to_code.items():
            self.assertIsInstance(name, str)
            self.assertIsInstance(code, str)
            self.assertTrue(code.isdigit(), f"城市码应为数字字符串: {name}={code!r}")
        # 反向映射一致
        self.assertEqual(code_to_name.get("101020100"), "上海")

    def test_local_city_map_contains_known_cities(self):
        """码表覆盖一线城市 + 三/四线城市（验证是全量，非旧 24 城）。"""
        module = load_module()
        name_to_code, _ = module.load_local_city_map()

        for city in ("全国", "北京", "上海", "深圳"):
            self.assertIn(city, name_to_code, f"缺少常见城市: {city}")
        # 三/四线城市（旧内置码表没有的），证明已扩展到全量
        for tier34 in ("赣州", "洛阳", "临沂", "襄阳"):
            self.assertIn(tier34, name_to_code, f"缺少三四线城市: {tier34}")

    def test_local_city_map_is_superset_of_old_builtin(self):
        """防回归：新静态码表必须 ⊇ 原内置 24 城且码值一致。"""
        module = load_module()
        name_to_code, _ = module.load_local_city_map()

        old_builtin = {
            "全国": "100010000",
            "北京": "101010100", "上海": "101020100", "广州": "101280100",
            "深圳": "101280600", "杭州": "101210100", "成都": "101270100",
            "西安": "101110100", "重庆": "101040100", "南京": "101190100",
            "长沙": "101250100", "福州": "101230100", "武汉": "101200100",
            "合肥": "101220100", "济南": "101120100", "大连": "101070200",
            "青岛": "101120200", "宁波": "101210400", "厦门": "101230200",
            "天津": "101030100", "苏州": "101190400", "郑州": "101180100",
            "东莞": "101281600", "佛山": "101280800", "沈阳": "101070100",
        }
        for name, code in old_builtin.items():
            self.assertEqual(name_to_code.get(name), code,
                             f"原内置城市 {name}={code} 在新码表中缺失或码值不一致")

    # ----- resolve_city 三级查询链 -----

    def test_resolve_city_hit_local_map(self):
        """本地静态码表命中（含三四线城市）。"""
        module = load_module()

        for name, code in [("上海", "101020100"), ("赣州", "101240700")]:
            self.assertEqual(module.resolve_city(name), (name, code))

    def test_resolve_city_reverse_lookup(self):
        """用城市码反查中文名。"""
        module = load_module()

        self.assertEqual(module.resolve_city("101020100"), ("上海", "101020100"))
        self.assertEqual(module.resolve_city("101240700"), ("赣州", "101240700"))

    def test_resolve_city_fallback_to_live(self):
        """本地码表没有时降级到运行时拉取（mock）。"""
        module = load_module()

        with mock.patch.object(module, "load_local_city_map",
                               return_value=({}, {})), \
             mock.patch.object(module, "load_live_city_maps",
                               return_value=({"长春": "101060100"},
                                             {"101060100": "长春"})):
            self.assertEqual(module.resolve_city("长春"), ("长春", "101060100"))
            self.assertEqual(module.resolve_city("101060100"), ("长春", "101060100"))

    def test_resolve_city_fallback_to_raw(self):
        """正反向映射均未命中时，仍接受 9 位裸 city code。"""
        module = load_module()

        with mock.patch.object(module, "load_local_city_map",
                               return_value=({}, {})) as local_loader, \
             mock.patch.object(module, "load_live_city_maps",
                               return_value=({}, {})) as live_loader:
            self.assertEqual(module.resolve_city("999999999"), ("999999999", "999999999"))
        local_loader.assert_called_once_with()
        live_loader.assert_called_once_with()

    def test_resolve_city_rejects_unknown_chinese_city(self):
        """未知中文城市不能原样作为 city 参数继续抓取。"""
        module = load_module()

        with mock.patch.object(module, "load_local_city_map",
                               return_value=({}, {})), \
             mock.patch.object(module, "load_live_city_maps",
                               return_value=({}, {})):
            with self.assertRaisesRegex(module.CityResolutionError,
                                        "无法解析城市 '不存在市'"):
                module.resolve_city("不存在市")

    def test_resolve_city_rejects_when_local_map_missing_and_live_api_fails(self):
        """本地码表缺失且在线接口失败时明确报错。"""
        module = load_module()

        with mock.patch.object(module, "load_local_city_map",
                               return_value=({}, {})), \
             mock.patch.object(module, "fetch_boss_json",
                               side_effect=OSError("network unavailable")):
            with self.assertLogs(module.log, level="WARNING") as logs:
                with self.assertRaises(module.CityResolutionError):
                    module.resolve_city("长春")
        self.assertIn("加载 BOSS 在线城市映射失败", "\n".join(logs.output))

    def test_fetch_boss_json_rejects_nonzero_business_code(self):
        """HTTP 200 下的 code: 35 不能静默当作空城市表。"""
        module = load_module()
        fake_requests = mock.Mock()
        resp = mock.Mock()
        resp.json.return_value = {
            "code": 35,
            "message": "您的IP地址存在异常行为.",
            "zpData": {},
        }
        fake_requests.get.return_value = resp

        with mock.patch.object(module, "requests", fake_requests):
            with self.assertRaisesRegex(module.CityAPIResponseError,
                                        "code=35"):
                module.fetch_boss_json(module.HOT_CITY_URL)

    def test_main_rejects_unknown_city_before_login_probe(self):
        """CLI 城市预校验失败以 exit 2（CLI 误用）退出，不进入登录探测。"""
        module = load_module()

        with mock.patch.object(sys, "argv", [
                "boss_cdp_raw.py", "--city", "不存在市",
        ]), \
             mock.patch.object(module, "require_runtime_dependencies",
                               return_value=True), \
             mock.patch.object(module, "resolve_city",
                               side_effect=module.CityResolutionError("无法解析城市")), \
             mock.patch.object(module, "check_login_state") as login_probe, \
             mock.patch("sys.stderr", new_callable=io.StringIO) as err, \
             redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as exit_context:
                module.main()

        self.assertEqual(exit_context.exception.code, 2)
        self.assertIn("无法解析城市", output.getvalue() + err.getvalue())
        login_probe.assert_not_called()

    def test_resolve_city_empty_input(self):
        module = load_module()

        self.assertEqual(module.resolve_city(""), ("", ""))

    # ----- --list-cities -----

    def test_list_cities_prints_all(self):
        """--list-cities 打印全部城市（用本地码表，mock 掉联网）。"""
        module = load_module()

        with mock.patch.object(module, "load_live_city_maps",
                               return_value=({}, {})):
            with mock.patch("sys.stdout", new_callable=__import__("io").StringIO) as out:
                module.list_cities(keyword=None)
            text = out.getvalue()
        self.assertIn("个城市", text)
        self.assertIn("上海", text)
        self.assertIn("赣州", text)

    def test_list_cities_with_filter(self):
        """关键词过滤只打印匹配的城市。"""
        module = load_module()

        with mock.patch.object(module, "load_live_city_maps",
                               return_value=({}, {})):
            with mock.patch("sys.stdout", new_callable=__import__("io").StringIO) as out:
                module.list_cities(keyword="江")
            text = out.getvalue()
        self.assertIn("江", text)
        self.assertNotIn("上海", text)
        self.assertNotIn("赣州", text)

    def test_list_cities_offline_uses_local(self):
        """联网失败时回退本地静态码表，不报错。"""
        module = load_module()

        with mock.patch.object(module, "load_live_city_maps",
                               return_value=({}, {})):
            with mock.patch("sys.stdout", new_callable=__import__("io").StringIO) as out:
                module.list_cities(keyword=None)
            text = out.getvalue()
        # 本地码表非空时应有输出
        self.assertIn("个城市", text)

    def test_filter_maps_match_current_boss_condition_snapshot(self):
        module = load_module()

        self.assertEqual(
            module.SALARY_MAP,
            {
                "不限": "0",
                "3K以下": "402",
                "3-5K": "403",
                "5-10K": "404",
                "10-20K": "405",
                "20-50K": "406",
                "50K以上": "407",
            },
        )
        self.assertEqual(
            module.EXPERIENCE_MAP,
            {
                "不限": "0",
                "在校生": "108",
                "应届生": "102",
                "经验不限": "101",
                "1年以内": "103",
                "1-3年": "104",
                "3-5年": "105",
                "5-10年": "106",
                "10年以上": "107",
            },
        )
        self.assertEqual(
            module.DEGREE_MAP,
            {
                "不限": "0",
                "初中及以下": "209",
                "中专/中技": "208",
                "高中": "206",
                "大专": "202",
                "本科": "203",
                "硕士": "204",
                "博士": "205",
            },
        )

    def test_login_probe_requires_plaintext_salary(self):
        module = load_module()

        hidden_salary = {"code": 0, "zpData": {"jobList": [{"jobName": "Java", "salaryDesc": ""}]}}
        visible_salary = {"code": 0, "zpData": {"jobList": [{"jobName": "Java", "salaryDesc": "20-40K"}]}}

        self.assertFalse(module.is_logged_in_search_response(hidden_salary))
        self.assertTrue(module.is_logged_in_search_response(visible_salary))
        self.assertFalse(module.is_logged_in_search_response({"code": 7, "zpData": {"jobList": []}}))

    def test_login_probe_classifies_distinct_failure_states(self):
        module = load_module()

        cases = [
            (
                {"code": 0, "zpData": {"jobList": [{"salaryDesc": "20-40K"}]}},
                module.LoginProbeStatus.AVAILABLE,
            ),
            (
                {"code": 0, "zpData": {"jobList": [{"salaryDesc": ""}]}},
                module.LoginProbeStatus.UNAUTHENTICATED,
            ),
            (
                {"code": 0, "zpData": {"jobList": []}},
                module.LoginProbeStatus.EMPTY,
            ),
            (
                {"code": 31, "message": "访问受限"},
                module.LoginProbeStatus.RESTRICTED,
            ),
            (
                # 实测风控码：已登录但被 BOSS 判「环境存在异常」（issue #33）
                {"code": 37, "message": "您的环境存在异常."},
                module.LoginProbeStatus.RESTRICTED,
            ),
            (
                # 未知风控码但 message 命中风控关键词，兜底归 RESTRICTED
                {"code": 9999, "message": "检测到访问频繁，请稍后再试"},
                module.LoginProbeStatus.RESTRICTED,
            ),
            (
                # 未知非零 code 一律归 RESTRICTED（降速语义），不再 RESPONSE_ERROR——
                # 新风控形态不被误判为不可恢复（第三轮调研落地）
                {"code": 7, "message": "业务异常"},
                module.LoginProbeStatus.RESTRICTED,
            ),
        ]

        for response, expected in cases:
            with self.subTest(expected=expected):
                result = module.classify_login_probe_response(response)
                self.assertIs(result.status, expected)

        restricted = module.classify_login_probe_response({"code": 31, "message": "访问受限"})
        self.assertEqual(restricted.code, 31)
        self.assertEqual(restricted.message, "访问受限")

        # 已登录但被风控（issue #33）：必须归 RESTRICTED 而非误判为登录失败
        risk_control = module.classify_login_probe_response(
            {"code": 37, "message": "您的环境存在异常."}
        )
        self.assertIs(risk_control.status, module.LoginProbeStatus.RESTRICTED)
        self.assertEqual(risk_control.code, 37)

    def test_login_probe_classifies_http_failures(self):
        module = load_module()

        self.assertIs(
            module.classify_login_probe_response({}, http_status=401).status,
            module.LoginProbeStatus.UNAUTHENTICATED,
        )
        self.assertIs(
            module.classify_login_probe_response({}, http_status=429).status,
            module.LoginProbeStatus.RESTRICTED,
        )
        server_error = module.classify_login_probe_response({}, http_status=503)
        self.assertIs(server_error.status, module.LoginProbeStatus.RESPONSE_ERROR)
        self.assertTrue(server_error.retryable)

    def test_run_check_reports_restriction_instead_of_logged_out(self):
        module = load_module()
        response = mock.Mock()
        response.json.return_value = {"Browser": "Chrome/140"}
        restricted = module.LoginProbeResult(
            module.LoginProbeStatus.RESTRICTED,
            code=31,
            message="访问受限",
        )
        requests_mock = mock.Mock()
        requests_mock.get.return_value = response
        stdout = io.StringIO()
        with mock.patch.object(module, "require_runtime_dependencies", return_value=True), \
                mock.patch.object(module, "requests", requests_mock), \
                mock.patch.object(module, "check_login_state", return_value=restricted), \
                redirect_stdout(stdout):
            self.assertEqual(module.run_check(cdp_port=9333), 1)

        output = stdout.getvalue()
        self.assertIn("限制状态", output)
        self.assertIn("code: 31", output)
        self.assertNotIn("未登录 —", output)

    def test_detail_record_preserves_job_id_and_job_link(self):
        module = load_module()
        job = {
            "job_id": "abc123",
            "title": "AI Engineer",
            "boss_name": "Acme",
            "salary": "30-60K",
            "salary_source": "api",
            "location": "上海",
            "tags": "3-5年 | 本科",
            "job_link": "https://www.zhipin.com/job_detail/abc.html",
        }
        extracted = {
            "tags": ["Python"],
            "jd": "Build AI agents",
            "boss_active_status": "今日活跃",
        }

        detail = module.build_detail_record(job, extracted)

        self.assertEqual(detail["job_id"], "abc123")
        self.assertEqual(detail["job_link"], job["job_link"])
        self.assertEqual(detail["link"], job["job_link"])
        self.assertEqual(detail["salary"], "30-60K")
        self.assertEqual(detail["salary_source"], "api")
        self.assertEqual(detail["boss_active_status"], "今日活跃")

    def test_detail_record_falls_back_to_list_active_status(self):
        module = load_module()
        job = {
            "job_id": "abc123",
            "title": "AI Engineer",
            "boss_name": "Acme",
            "salary": "30-60K",
            "salary_source": "api",
            "location": "上海",
            "tags": "3-5年 | 本科",
            "job_link": "https://www.zhipin.com/job_detail/abc.html",
            "boss_active_status": "本周活跃",
        }
        extracted = {"tags": ["Python"], "jd": "Build AI agents"}

        detail = module.build_detail_record(job, extracted)

        self.assertEqual(detail["boss_active_status"], "本周活跃")

    def test_merge_unique_deduplicates_by_job_id_keeping_old(self):
        module = load_module()
        existing = [{"job_id": "a", "title": "old-a"}, {"job_id": "b", "title": "old-b"}]
        incoming = [{"job_id": "b", "title": "new-b"}, {"job_id": "c", "title": "new-c"}]

        merged = module.merge_unique(existing, incoming)

        self.assertEqual([j["job_id"] for j in merged], ["a", "b", "c"])
        self.assertEqual(merged[1]["title"], "old-b", "同 id 应保留旧记录")

    def test_merge_unique_new_overrides_old(self):
        module = load_module()
        existing = [{"job_id": "a", "title": "old-a"}]
        incoming = [{"job_id": "a", "title": "new-a"}, {"job_id": "b", "title": "new-b"}]

        merged = module.merge_unique(existing, incoming, new_overrides=True)

        self.assertEqual([j["job_id"] for j in merged], ["a", "b"])
        self.assertEqual(merged[0]["title"], "new-a", "同 id 应保留新记录")

    def test_merge_unique_skips_non_dict_items_without_crashing(self):
        module = load_module()
        existing = [{"job_id": "a", "title": "old-a"}, "not-a-dict"]
        incoming = [None, {"job_id": "b", "title": "new-b"}, 42]

        merged = module.merge_unique(existing, incoming)
        # existing 非 dict 项原样保留（不静默丢弃旧数据），incoming 非 dict 项跳过
        self.assertEqual(len(merged), 3)
        dict_items = [j for j in merged if isinstance(j, dict)]
        self.assertEqual([j["job_id"] for j in dict_items], ["a", "b"])

    def test_atomic_write_json_writes_payload_and_cleans_tmp(self):
        module = load_module()
        with tempfile_profile() as paths:
            target = str(paths["cdp_profile"] / "out.json")
            payload = {"jobs": [{"job_id": "a"}]}

            module._atomic_write_json(target, payload)

            with open(target, "r", encoding="utf-8") as f:
                self.assertEqual(json.load(f), payload)
            leftovers = [
                name for name in os.listdir(paths["cdp_profile"])
                if name.startswith("out.json.tmp")
            ]
            self.assertEqual(leftovers, [], "失败/成功路径都不应残留 .tmp 文件")

    def test_atomic_write_json_preserves_original_on_serialize_failure(self):
        module = load_module()
        with tempfile_profile() as paths:
            target = str(paths["cdp_profile"] / "out.json")
            os.makedirs(paths["cdp_profile"], exist_ok=True)
            with open(target, "w", encoding="utf-8") as f:
                f.write('{"keep": true}')

            with mock.patch.object(
                module.json, "dump", side_effect=TypeError("cannot serialize")
            ):
                with self.assertRaises(TypeError):
                    module._atomic_write_json(target, {"bad": object()})

            with open(target, "r", encoding="utf-8") as f:
                self.assertEqual(json.load(f), {"keep": True}, "失败时不应破坏原文件")

    def test_flush_jobs_deduplicates_across_incremental_writes(self):
        module = load_module()
        with tempfile_profile() as paths:
            target = str(paths["cdp_profile"] / "jobs.json")
            meta = {"keyword": "Java"}

            def full(jid):
                return {"job_id": jid, "title": f"T-{jid}", "location": "深圳",
                        "job_link": f"https://www.zhipin.com/job_detail/{jid}.html",
                        "company_name": "某科技"}

            module.flush_jobs(target, dict(meta), [full("a"), full("b")])
            module.flush_jobs(target, dict(meta), [full("b"), full("c")])

            with open(target, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual([j["job_id"] for j in data["jobs"]], ["a", "b", "c"])
            self.assertEqual(data["total"], 3)
            self.assertEqual(data["keyword"], "Java")

    def test_detail_extractor_never_uses_body_text_as_jd_fallback(self):
        module = load_module()

        self.assertNotIn("jd = body.substring", module.EXTRACT_DETAIL_JS)
        self.assertIn("page_text", module.EXTRACT_DETAIL_JS)
        self.assertIn("text.indexOf('职位描述')", module.EXTRACT_DETAIL_JS)

    def test_extract_job_description_removes_header_and_recruiter_footer(self):
        module = load_module()
        description = (
            "公司介绍\n这段属于招聘方发布的岗位正文，应当保留。\n"
            + "负责 AI 产品规划、需求分析、研发协作和上线复盘。\n" * 8
        ).strip()
        page_text = (
            "微信扫码分享 举报\n职位描述\n"
            f"{description}\n"
            "张女士\n今日活跃\n示例公司\n·\n招聘者\n竞争力分析\n"
            "查看完整个人竞争力\nBOSS 安全提示\n公司工商信息\n更多职位"
        )

        jd = module.extract_job_description({"jd": page_text, "page_text": page_text})

        self.assertEqual(jd, description)
        self.assertIn("公司介绍", jd)
        self.assertNotIn("张女士", jd)
        self.assertNotIn("竞争力分析", jd)

    def test_detail_extracts_page_update_date(self):
        """页面更新时间：详情页含"页面更新时间：YYYY-MM-DD" → 提取为 page_update_date；缺失留空。"""
        module = load_module()
        jd = "职位描述\n负责 AI 产品规划。\n" * 8
        page_text = f"{jd}\n页面更新时间：2026-08-13\n张女士\n今日活跃"
        fields = module.extract_detail_fields({"jd": jd, "page_text": page_text})
        self.assertEqual(fields["page_update_date"], "2026-08-13")

        fields2 = module.extract_detail_fields({"jd": jd, "page_text": "无更新时间字段"})
        self.assertEqual(fields2["page_update_date"], "")

    def test_build_detail_record_carries_page_update_date(self):
        """详情记录透传 page_update_date（缺省空串）。"""
        module = load_module()
        job = {"job_id": "j1", "title": "T", "job_link": "https://www.zhipin.com/job/x.html",
               "boss_name": "C", "salary": "20-30K", "location": "杭州", "tags": ""}
        rec = module.build_detail_record(job, {"jd": "JD", "page_update_date": "2026-08-13"})
        self.assertEqual(rec["page_update_date"], "2026-08-13")
        rec2 = module.build_detail_record(job, {"jd": "JD"})
        self.assertEqual(rec2["page_update_date"], "")

    def test_extract_job_description_rejects_login_truncation(self):
        module = load_module()
        page_text = (
            "职位描述\n负责产品规划和需求分析。\n"
            "登录查看完整内容\n招聘者\nBOSS 安全提示"
        )

        with self.assertRaises(module.DetailLoginRequiredError):
            module.extract_job_description({"jd": "", "page_text": page_text})

    def test_extract_job_description_preserves_competitiveness_heading_in_jd(self):
        module = load_module()
        description = (
            "岗位职责\n负责产品规划、需求分析和跨团队项目推进。\n"
            "竞争力分析\n负责持续研究竞品并制定差异化产品策略。\n" * 5
        )

        jd = module.extract_job_description({"jd": f"职位描述\n{description}"})

        self.assertIn("竞争力分析", jd)
        self.assertIn("制定差异化产品策略", jd)

    def test_extract_job_description_removes_trailing_recruiter_card(self):
        module = load_module()
        description = "负责 AI 产品规划、需求分析和跨团队项目推进。\n" * 8
        page_text = (
            f"职位描述\n{description}"
            "李女士\n在线\n示例公司\n·\n招聘专员"
        )

        jd = module.extract_job_description({"jd": page_text, "page_text": page_text})

        self.assertEqual(jd, description.strip())
        self.assertNotIn("李女士", jd)
        self.assertNotIn("招聘专员", jd)

    def test_extract_detail_fields_returns_boss_active_status_separately(self):
        module = load_module()
        description = (
            "公司介绍\n这段属于招聘方发布的岗位正文，应当保留。\n"
            + "负责 AI 产品规划、需求分析、研发协作和上线复盘。\n" * 8
        ).strip()
        page_text = (
            "微信扫码分享 举报\n职位描述\n"
            f"{description}\n"
            "张女士\n今日活跃\n示例公司\n·\n招聘者\n竞争力分析\n"
            "查看完整个人竞争力\nBOSS 安全提示\n公司工商信息\n更多职位"
        )

        fields = module.extract_detail_fields({"jd": page_text, "page_text": page_text})

        self.assertEqual(fields["jd"], description)
        self.assertEqual(fields["boss_active_status"], "今日活跃")
        self.assertNotIn("今日活跃", fields["jd"])
        self.assertNotIn("张女士", fields["jd"])

    def test_extract_detail_fields_online_status(self):
        module = load_module()
        description = "负责 AI 产品规划、需求分析和跨团队项目推进。\n" * 8
        page_text = (
            f"职位描述\n{description}"
            "李女士\n在线\n示例公司\n·\n招聘专员"
        )

        fields = module.extract_detail_fields({"jd": page_text, "page_text": page_text})

        self.assertEqual(fields["jd"], description.strip())
        self.assertEqual(fields["boss_active_status"], "在线")
        self.assertNotIn("在线", fields["jd"])

    def test_map_list_boss_active_status_from_representative_responses(self):
        module = load_module()

        # List API typically has bossOnline but not activeTimeDesc.
        self.assertEqual(
            module.map_list_boss_active_status({"bossOnline": True}),
            "在线",
        )
        # Prefer detailed label when list unexpectedly has activeTimeDesc.
        self.assertEqual(
            module.map_list_boss_active_status({
                "activeTimeDesc": "刚刚活跃",
                "bossOnline": True,
            }),
            "刚刚活跃",
        )
        self.assertEqual(module.map_list_boss_active_status({}), "")
        self.assertEqual(
            module.map_list_boss_active_status({"bossOnline": False}),
            "",
        )

    def test_resolve_boss_active_status_prefers_detail_over_list(self):
        module = load_module()

        self.assertEqual(
            module.resolve_boss_active_status(
                list_status="在线",
                detail_status="刚刚活跃",
            ),
            "刚刚活跃",
        )
        self.assertEqual(
            module.resolve_boss_active_status(list_status="在线", detail_status=""),
            "在线",
        )
        self.assertEqual(
            module.resolve_boss_active_status(list_status="", detail_status=""),
            "",
        )

    def test_fetch_api_js_maps_bossonline_fallback(self):
        module = load_module()
        js = module.FETCH_API_JS_TEMPLATE

        self.assertIn("j.activeTimeDesc", js)
        self.assertIn("j.bossOnline", js)
        self.assertIn("boss_active_status: j.activeTimeDesc || (j.bossOnline ?", js)

    def test_extract_job_description_removes_recruiter_card_before_safety_footer(self):
        module = load_module()
        description = "负责视觉算法研发、模型部署和业务场景落地。\n" * 8
        page_text = (
            f"职位描述\n{description}"
            "认证资质\n人力资源服务许可证\n"
            "曾先生\n示例猎头\n·\n猎头顾问\n\n"
            "BOSS 安全提示\n公司介绍\n更多职位"
        )

        jd = module.extract_job_description({"jd": page_text, "page_text": page_text})

        self.assertEqual(
            jd,
            f"{description}认证资质\n人力资源服务许可证".strip(),
        )
        self.assertNotIn("曾先生", jd)
        self.assertNotIn("猎头顾问", jd)

    def test_extract_job_description_rejects_navigation_page(self):
        module = load_module()
        page_text = "首页\n职位\n公司\n校园\n无障碍专区\n热门职位\n产品经理"

        with self.assertRaisesRegex(module.DetailExtractionError, "navigation chrome"):
            module.extract_job_description({"jd": "", "page_text": page_text})

    def test_extract_job_description_rejects_short_text(self):
        module = load_module()

        with self.assertRaisesRegex(module.DetailExtractionError, "too short"):
            module.extract_job_description({"jd": "职位描述\n只有一句话"})

    def test_detail_url_adds_security_context_without_changing_job_link(self):
        module = load_module()
        job = {
            "job_link": "https://www.zhipin.com/job_detail/abc.html",
            "security_id": "sec value",
            "lid": "lid-123",
        }

        detail_url = module.build_detail_url(job)

        self.assertEqual(job["job_link"], "https://www.zhipin.com/job_detail/abc.html")
        self.assertEqual(
            detail_url,
            "https://www.zhipin.com/job_detail/abc.html?lid=lid-123&securityId=sec+value",
        )

    def test_detail_url_rejects_non_zhipin_hosts(self):
        """job_link 来自外部数据（--merge/--input 不可信文件）时，防止导航到
        任意站点（对照开源 boss-agent-cli 的 hostname 精确校验实践）。"""
        module = load_module()
        for bad in (
            "https://evil.example/job_detail/abc.html",
            "https://zhipin.com.evil.example/job",
            "http://localhost:8080/job",
            "file:///C:/windows/temp/evil.json",
        ):
            self.assertEqual(
                module.build_detail_url({"job_link": bad}),
                "",
                f"非 zhipin.com 主机应拒绝: {bad}",
            )
        self.assertEqual(module.build_detail_url({"job_link": ""}), "")
        self.assertEqual(
            module.build_detail_url(
                {"job_link": "https://www.zhipin.com/job_detail/abc.html"}),
            "https://www.zhipin.com/job_detail/abc.html",
            "合法 zhipin 链接不受影响",
        )
        self.assertEqual(
            module.build_detail_url(
                {"job_link": "https://hz.zhipin.com/job/x"}),
            "https://hz.zhipin.com/job/x",
            "zhipin 子域合法",
        )

    def test_api_extraction_keeps_detail_context_fields(self):
        module = load_module()

        self.assertIn("security_id: j.securityId", module.FETCH_API_JS_TEMPLATE)
        self.assertIn("lid: j.lid", module.FETCH_API_JS_TEMPLATE)
        self.assertIn("encrypt_job_id: j.encryptJobId", module.FETCH_API_JS_TEMPLATE)

    def test_api_extraction_includes_market_fields(self):
        """市场信息字段：job_valid_status（官方在招状态）/icon（急·新标签）/proxy（代招标记）/job_type。"""
        module = load_module()
        js = module.FETCH_API_JS_TEMPLATE

        self.assertIn("job_valid_status: j.jobValidStatus", js)
        self.assertIn("icon_flags: (j.iconFlagList || []).join('|')", js)
        self.assertIn("icon_word: j.iconWord", js)
        self.assertIn("proxy_job: j.proxyJob", js)
        self.assertIn("proxy_type: j.proxyType", js)
        self.assertIn("job_type: j.jobType", js)

    def test_dom_fallback_is_opt_in(self):
        module = load_module()

        self.assertFalse(module.should_use_dom_fallback([], allow_dom_fallback=False))
        self.assertTrue(module.should_use_dom_fallback([], allow_dom_fallback=True))
        self.assertFalse(module.should_use_dom_fallback([{"title": "Java"}], allow_dom_fallback=True))

    def test_api_job_parser_rejects_error_rows(self):
        module = load_module()

        self.assertEqual(module.parse_api_jobs_eval_value(json.dumps([{"error": 403}])), [])
        self.assertEqual(
            module.parse_api_jobs_eval_value(json.dumps([{"title": "Java", "job_link": "https://example.com"}])),
            [{"title": "Java", "job_link": "https://example.com"}],
        )

    def test_login_probe_uses_one_budgeted_request(self):
        module = load_module()
        cdp = mock.Mock()
        cdp.eval_js.return_value = json.dumps({
            "httpStatus": 200,
            "body": json.dumps({
                "code": 0,
                "zpData": {"jobList": [{"jobName": "Java", "salaryDesc": "20-40K"}]},
            }),
        })
        module._request_counter = 0

        result = module.probe_login_state(cdp, "sid", query="Java", city_code="101020100")

        self.assertIs(result.status, module.LoginProbeStatus.AVAILABLE)
        self.assertEqual(cdp.eval_js.call_count, 1)
        self.assertEqual(module._request_counter, 1)
        probe_js = cdp.eval_js.call_args.args[0]
        self.assertIn("query=Java", probe_js)
        self.assertIn("city=101020100", probe_js)

    def test_wait_for_login_rotates_targets_and_backs_off(self):
        module = load_module()
        cdp = mock.Mock()
        results = [
            module.LoginProbeResult(module.LoginProbeStatus.EMPTY),
            module.LoginProbeResult(module.LoginProbeStatus.AVAILABLE),
        ]
        with mock.patch.object(module, "CDPSession", return_value=cdp), \
                mock.patch.object(
                    module,
                    "create_page_session",
                    return_value=("login-target", "login-session"),
                ), \
                mock.patch.object(module, "probe_login_state", side_effect=results) as probe, \
                mock.patch.object(module.time, "sleep") as sleep:
            self.assertTrue(module.wait_for_login(cdp_port=9333, timeout=10, interval=3))

        self.assertEqual(
            probe.call_args_list,
            [
                mock.call(
                    cdp,
                    "login-session",
                    query=module.LOGIN_PROBE_TARGETS[0][0],
                    city_code=module.LOGIN_PROBE_TARGETS[0][1],
                ),
                mock.call(
                    cdp,
                    "login-session",
                    query=module.LOGIN_PROBE_TARGETS[1][0],
                    city_code=module.LOGIN_PROBE_TARGETS[1][1],
                ),
            ],
        )
        sleep.assert_called_once()
        self.assertAlmostEqual(sleep.call_args.args[0], 3, delta=0.1)

    def test_wait_for_login_stops_immediately_when_restricted(self):
        module = load_module()
        cdp = mock.Mock()
        restricted = module.LoginProbeResult(
            module.LoginProbeStatus.RESTRICTED,
            code=31,
            message="访问受限",
        )
        stdout = io.StringIO()
        with mock.patch.object(module, "CDPSession", return_value=cdp), \
                mock.patch.object(
                    module,
                    "create_page_session",
                    return_value=("login-target", "login-session"),
                ), \
                mock.patch.object(module, "probe_login_state", return_value=restricted) as probe, \
                mock.patch.object(module.time, "sleep") as sleep, \
                redirect_stdout(stdout):
            self.assertFalse(module.wait_for_login(cdp_port=9333, timeout=300))

        probe.assert_called_once()
        sleep.assert_not_called()
        self.assertIn("code: 31", stdout.getvalue())
        self.assertIn("已停止登录探测", stdout.getvalue())

    def test_wait_for_login_treats_code37_risk_control_as_restricted(self):
        # issue #33：已登录但被 BOSS 风控（code 37「您的环境存在异常」），
        # 必须走 RESTRICTED 文案分支，而非误判为不可恢复的登录失败。
        module = load_module()
        cdp = mock.Mock()
        restricted = module.LoginProbeResult(
            module.LoginProbeStatus.RESTRICTED,
            code=37,
            message="您的环境存在异常.",
        )
        stdout = io.StringIO()
        with mock.patch.object(module, "CDPSession", return_value=cdp), \
                mock.patch.object(
                    module,
                    "create_page_session",
                    return_value=("login-target", "login-session"),
                ), \
                mock.patch.object(module, "probe_login_state", return_value=restricted) as probe, \
                mock.patch.object(module.time, "sleep") as sleep, \
                redirect_stdout(stdout):
            self.assertFalse(module.wait_for_login(cdp_port=9333, timeout=300))

        probe.assert_called_once()
        sleep.assert_not_called()
        output = stdout.getvalue()
        self.assertIn("code: 37", output)
        self.assertIn("已停止登录探测", output)
        # 应提示用户「完成验证/稍后再试」，而不是误导性的「登录探测响应异常」
        self.assertIn("请先在浏览器中完成验证或稍后再试", output)
        self.assertNotIn("登录探测响应异常", output)

    def test_wait_for_login_limits_transient_response_errors(self):
        module = load_module()
        cdp = mock.Mock()
        transient_error = module.LoginProbeResult(
            module.LoginProbeStatus.RESPONSE_ERROR,
            message="响应为空",
            retryable=True,
        )
        stdout = io.StringIO()
        with mock.patch.object(module, "CDPSession", return_value=cdp), \
                mock.patch.object(
                    module,
                    "create_page_session",
                    return_value=("login-target", "login-session"),
                ), \
                mock.patch.object(
                    module,
                    "probe_login_state",
                    return_value=transient_error,
                ) as probe, \
                mock.patch.object(module.time, "sleep") as sleep, \
                redirect_stdout(stdout):
            self.assertFalse(module.wait_for_login(cdp_port=9333, timeout=300))

        self.assertEqual(probe.call_count, module.LOGIN_PROBE_MAX_TRANSIENT_ERRORS + 1)
        self.assertEqual(sleep.call_count, module.LOGIN_PROBE_MAX_TRANSIENT_ERRORS)
        self.assertIn("连续异常次数过多", stdout.getvalue())

    def test_find_latest_detail_file_uses_default_result_dir(self):
        module = load_module()
        with tempfile_profile() as paths:
            result_dir = paths["cdp_profile"] / "job-result"
            result_dir.mkdir(parents=True)
            older = result_dir / "boss_details_20260612_1000.json"
            newer = result_dir / "boss_details_20260612_1100.json"
            older.write_text("[]", encoding="utf-8")
            newer.write_text("[]", encoding="utf-8")

            self.assertEqual(module.find_latest_detail_file(str(result_dir)), str(newer))

    def test_find_latest_detail_file_ignores_pending_files(self):
        module = load_module()
        with tempfile_profile() as paths:
            result_dir = paths["cdp_profile"] / "job-result"
            result_dir.mkdir(parents=True)
            detail = result_dir / "boss_details_20260612_1100.json"
            pending = result_dir / "boss_details_20260612_1100.json.pending.json"
            detail.write_text("[]", encoding="utf-8")
            pending.write_text("[]", encoding="utf-8")
            os.utime(pending, (3000, 3000))
            os.utime(detail, (2000, 2000))

            self.assertEqual(module.find_latest_detail_file(str(result_dir)), str(detail),
                             "pending 是活动文件，不应被当作最新详情")

    def test_existing_detail_loader_prefers_sibling_detail_file(self):
        module = load_module()
        with tempfile_profile() as paths:
            result_dir = paths["cdp_profile"] / "job-result"
            result_dir.mkdir(parents=True)
            list_path = result_dir / "boss_jobs_20260612_1100.json"
            detail_path = result_dir / "boss_details_20260612_1100.json"
            list_path.write_text('{"jobs":[]}', encoding="utf-8")
            detail_path.write_text('[{"job_id":"abc123"}]', encoding="utf-8")

            details = module.load_existing_details(
                input_path=str(list_path),
                detail_output=None,
                result_dir=str(result_dir),
            )

        self.assertEqual(details, [{"job_id": "abc123"}])

    def test_windows_default_paths_use_localappdata(self):
        module = load_module()
        env = {
            "LOCALAPPDATA": r"C:\Users\leon\AppData\Local",
            "PROGRAMFILES": r"C:\Program Files",
            "PROGRAMFILES(X86)": r"C:\Program Files (x86)",
        }
        expected_chrome = r"C:\Users\leon\AppData\Local\Google\Chrome\Application\chrome.exe"
        with mock.patch.object(module.platform, "system", return_value="Windows"), \
                mock.patch.dict(module.os.environ, env, clear=False), \
                mock.patch.object(module.os.path, "exists", side_effect=lambda p: p == expected_chrome):
            self.assertEqual(module.get_default_chrome_path(), expected_chrome)
            self.assertEqual(
                module.get_default_profile_dir(),
                r"C:\Users\leon\AppData\Local\Google\Chrome\User Data",
            )

    def test_windows_process_parsing_matches_user_data_dir_and_cdp_port(self):
        module = load_module()
        ps_json = json.dumps([{
            "ProcessId": 456,
            "CommandLine": (
                r'"C:\Program Files\Google\Chrome\Application\chrome.exe" '
                r'--remote-debugging-port=9333 '
                r'--user-data-dir="C:\Users\leon\.boss-zhipin-scraper\chrome-profile"'
            ),
        }])
        with mock.patch.object(module.platform, "system", return_value="Windows"), \
                mock.patch.object(module.subprocess, "run", return_value=type("Completed", (), {"stdout": ps_json, "returncode": 0})()):
            self.assertEqual(
                module.chrome_pids_for_user_data_dir(r"C:\Users\leon\.boss-zhipin-scraper\chrome-profile"),
                [456],
            )
            self.assertEqual(
                module.chrome_user_data_dirs_for_cdp_port(9333),
                [r"C:\Users\leon\.boss-zhipin-scraper\chrome-profile"],
            )

    def test_smoke_jobs_require_api_salary_and_link(self):
        module = load_module()

        self.assertTrue(module.has_usable_smoke_jobs([{
            "title": "AI Engineer",
            "salary": "30-60K",
            "salary_source": "api",
            "job_link": "https://www.zhipin.com/job_detail/abc.html",
        }]))
        self.assertFalse(module.has_usable_smoke_jobs([{
            "title": "AI Engineer",
            "salary": "",
            "salary_source": "api_empty",
            "job_link": "https://www.zhipin.com/job_detail/abc.html",
        }]))

    def test_write_detail_csv_exports_detail_fields(self):
        module = load_module()
        with tempfile_profile() as paths:
            csv_path = paths["cdp_profile"] / "details.csv"
            module.write_detail_csv(str(csv_path), [{
                "job_id": "abc123",
                "title": "AI Engineer",
                "company": "Acme",
                "salary": "30-60K",
                "salary_source": "api",
                "location": "上海",
                "tags_list": "3-5年 | 本科",
                "job_link": "https://www.zhipin.com/job_detail/abc.html",
                "skill_tags": ["Python", "LLM"],
                "jd": "Build AI agents",
            }])

            with open(csv_path, encoding="utf-8-sig", newline="") as f:
                rows = list(csv.DictReader(f))

        self.assertEqual(rows[0]["job_id"], "abc123")
        self.assertEqual(rows[0]["salary_source"], "api")
        self.assertEqual(rows[0]["skill_tags"], "Python | LLM")
        self.assertEqual(rows[0]["jd"], "Build AI agents")

    def test_scrape_details_final_save_handles_bare_filename(self):
        """--detail-output 传不带目录的裸文件名时，最终保存不应崩溃。

        空 jobs 列表不触发 CDP，可直接走到最终保存逻辑；此前最终保存用
        os.makedirs(os.path.dirname(path))，dirname 为空字符串会抛
        FileNotFoundError，丢掉收尾保存和 CSV 导出。
        """
        module = load_module()
        with tempfile_profile() as paths:
            workdir = paths["cdp_profile"]
            workdir.mkdir(parents=True, exist_ok=True)
            cwd = os.getcwd()
            os.chdir(workdir)
            try:
                module.scrape_details({"jobs": []}, output_path="boss_details.json")
                self.assertTrue((workdir / "boss_details.json").exists())
            finally:
                os.chdir(cwd)

    def test_scrape_details_stops_before_writing_login_truncation(self):
        module = load_module()
        session = mock.Mock()

        def send(method, params=None, sid=None):
            if method == "Target.createTarget":
                return {"result": {"targetId": "target-1"}}
            if method == "Target.attachToTarget":
                return {"result": {"sessionId": "session-1"}}
            return {}

        session.send.side_effect = send
        session.eval_js.side_effect = lambda script, sid: (
            json.dumps(
                {
                    "jd": "",
                    "page_text": "职位描述\n负责产品规划\n登录查看完整内容",
                    "tags": [],
                }
            )
            if script == module.EXTRACT_DETAIL_JS
            else None
        )
        job = {
            "job_id": "blocked",
            "title": "AI Product Manager",
            "job_link": "https://www.zhipin.com/job_detail/blocked.html",
        }

        with tempfile_profile() as paths:
            output = paths["cdp_profile"] / "details.json"
            with mock.patch.object(module, "CDPSession", return_value=session), \
                    mock.patch.object(module.time, "sleep"):
                with self.assertRaisesRegex(RuntimeError, "login expired"):
                    module.scrape_details({"jobs": [job]}, output_path=str(output))

        self.assertFalse(output.exists())
        session.send.assert_any_call(
            "Target.closeTarget", {"targetId": "target-1"}
        )
        session.close.assert_called_once()

    def test_setup_defaults_do_not_copy_cookies_or_kill_all_chrome(self):
        module = load_module()
        calls = {"copy2": [], "run": [], "popen": []}
        fake_requests = mock.Mock()
        responses = iter([
            ConnectionError("not ready"),
            type("Resp", (), {"status_code": 200})(),
        ])

        def fake_get(*args, **kwargs):
            response = next(responses)
            if isinstance(response, BaseException):
                raise response
            return response

        with tempfile_profile() as paths:
            expected_profile_arg = f"--user-data-dir={paths['cdp_profile']}"
            with mock.patch.object(module, "DEFAULT_PROFILE_DIR", str(paths["source_profile"])), \
                    mock.patch.object(module, "DEFAULT_CDP_DATA_DIR", str(paths["cdp_profile"])), \
                    mock.patch.object(module, "requests", fake_requests), \
                    mock.patch.object(module.shutil, "copy2", side_effect=lambda src, dst: calls["copy2"].append((src, dst))), \
                    mock.patch.object(module.subprocess, "run", side_effect=lambda *args, **kwargs: fake_run(calls, *args, **kwargs)), \
                    mock.patch.object(module.subprocess, "Popen", side_effect=lambda cmd, **kwargs: calls["popen"].append(cmd)), \
                    mock.patch.object(module.time, "sleep", return_value=None), \
                    mock.patch.object(module, "wait_for_login", return_value=True) as wait_login:
                fake_requests.get.side_effect = fake_get
                self.assertEqual(module.run_setup_chrome(cdp_port=9333), 0)

        self.assertEqual(calls["copy2"], [])
        self.assertTrue(all("killall" not in cmd for cmd in calls["run"]))
        self.assertTrue(calls["popen"])
        launched = calls["popen"][0]
        self.assertIn(expected_profile_arg, launched)
        wait_login.assert_called_once_with(9333, timeout=module.DEFAULT_LOGIN_TIMEOUT)

    def test_copy_login_state_is_explicit_and_does_not_copy_password_databases(self):
        module = load_module()
        copied = []
        with tempfile_profile() as paths:
            with mock.patch.object(module, "DEFAULT_PROFILE_DIR", str(paths["source_profile"])), \
                    mock.patch.object(module, "DEFAULT_CDP_DATA_DIR", str(paths["cdp_profile"])), \
                    mock.patch.object(module.shutil, "copy2", side_effect=lambda src, dst: copied.append((pathlib.Path(src), pathlib.Path(dst)))):
                result = module.prepare_cdp_profile(copy_login_state=True, reset=False)

        copied_names = [src.name for src, _ in copied]
        copied_rel_paths = [src.relative_to(paths["source_profile"]) for src, _ in copied]
        self.assertEqual(result["copied"], 4)
        self.assertIn("Local State", copied_names)
        self.assertIn("Cookies", copied_names)
        self.assertIn(pathlib.Path("Default/Cookies-journal"), copied_rel_paths)
        self.assertIn(pathlib.Path("Default/Network/Cookies"), copied_rel_paths)
        self.assertNotIn("Login Data", copied_names)
        self.assertNotIn("Web Data", copied_names)

    def test_setup_rejects_ready_cdp_port_owned_by_other_profile(self):
        module = load_module()
        fake_requests = mock.Mock()
        fake_requests.get.return_value = type("Resp", (), {"status_code": 200})()

        with tempfile_profile() as paths:
            ps_output = process_query_stdout([
                (123, chrome_cmdline(9333, "/tmp/chrome-cdp-data")),
            ])
            with mock.patch.object(module, "DEFAULT_CDP_DATA_DIR", str(paths["cdp_profile"])), \
                    mock.patch.object(module, "requests", fake_requests), \
                    mock.patch.object(module.subprocess, "run", return_value=type("Completed", (), {"stdout": ps_output, "returncode": 0})()), \
                    mock.patch.object(module.subprocess, "Popen") as popen:
                self.assertEqual(module.run_setup_chrome(cdp_port=9333), 1)

        popen.assert_not_called()

    def test_setup_reuses_ready_cdp_port_owned_by_dedicated_profile(self):
        module = load_module()
        fake_requests = mock.Mock()
        fake_requests.get.return_value = type("Resp", (), {"status_code": 200})()

        with tempfile_profile() as paths:
            ps_output = process_query_stdout([
                (123, chrome_cmdline(9333, str(paths["cdp_profile"]))),
            ])
            with mock.patch.object(module, "DEFAULT_CDP_DATA_DIR", str(paths["cdp_profile"])), \
                    mock.patch.object(module, "requests", fake_requests), \
                    mock.patch.object(module.subprocess, "run", return_value=type("Completed", (), {"stdout": ps_output, "returncode": 0})()), \
                    mock.patch.object(module.subprocess, "Popen") as popen, \
                    mock.patch.object(module, "wait_for_login", return_value=True) as wait_login:
                self.assertEqual(module.run_setup_chrome(cdp_port=9333), 0)

        popen.assert_not_called()
        wait_login.assert_called_once_with(9333, timeout=module.DEFAULT_LOGIN_TIMEOUT)

    def test_setup_can_skip_waiting_for_login(self):
        module = load_module()
        fake_requests = mock.Mock()
        fake_requests.get.return_value = type("Resp", (), {"status_code": 200})()

        with tempfile_profile() as paths:
            ps_output = process_query_stdout([
                (123, chrome_cmdline(9333, str(paths["cdp_profile"]))),
            ])
            with mock.patch.object(module, "DEFAULT_CDP_DATA_DIR", str(paths["cdp_profile"])), \
                    mock.patch.object(module, "requests", fake_requests), \
                    mock.patch.object(module.subprocess, "run", return_value=type("Completed", (), {"stdout": ps_output, "returncode": 0})()), \
                    mock.patch.object(module, "wait_for_login") as wait_login:
                self.assertEqual(module.run_setup_chrome(cdp_port=9333, wait_login=False), 0)

        wait_login.assert_not_called()

    def test_chrome_process_parsing_matches_unquoted_user_data_dir(self):
        module = load_module()

        with tempfile_profile() as paths:
            ps_output = process_query_stdout([
                (123, chrome_cmdline(9333, str(paths["cdp_profile"]))),
                (456, chrome_cmdline(9334, "/tmp/other-profile")),
            ])
            with mock.patch.object(module.subprocess, "run", return_value=type("Completed", (), {"stdout": ps_output, "returncode": 0})()):
                self.assertEqual(module.chrome_pids_for_user_data_dir(str(paths["cdp_profile"])), [123])
                self.assertEqual(module.chrome_user_data_dirs_for_cdp_port(9333), [str(paths["cdp_profile"])])
                self.assertTrue(module.cdp_port_uses_profile(9333, str(paths["cdp_profile"])))

    def test_stop_cdp_chrome_terminates_only_matching_profile(self):
        module = load_module()

        terminated = []
        # chrome_pids_for_user_data_dir 第一次返回 scraper profile 的 pid（111），
        # SIGTERM 后轮询返回空 -> 成功关闭，不升级 SIGKILL。
        # （按 user-data-dir 过滤出 111、不关其它 profile 的进程，该过滤逻辑由
        #   test_chrome_process_parsing_matches_unquoted_user_data_dir 独立覆盖）
        pid_lookups = iter([[111], []])
        with mock.patch.object(module, "chrome_pids_for_user_data_dir",
                               side_effect=lambda _dir: next(pid_lookups)), \
             mock.patch.object(module, "terminate_process",
                               side_effect=lambda pid, force=False: terminated.append((pid, force))), \
             mock.patch.object(module.time, "sleep"):
            stopped = module.stop_cdp_chrome("/fake/scraper-profile")

        self.assertEqual(stopped, 1)
        # 只对 scraper 的 pid 用 SIGTERM（force=False），且只一次
        self.assertEqual(terminated, [(111, False)])

    def test_stop_cdp_chrome_no_processes_returns_zero(self):
        module = load_module()

        with mock.patch.object(module, "chrome_pids_for_user_data_dir", return_value=[]):
            stopped = module.stop_cdp_chrome("/fake/scraper-profile")
        self.assertEqual(stopped, 0)

    def test_stop_cdp_chrome_escalates_to_force_kill(self):
        module = load_module()

        terminated = []
        # SIGTERM 后进程始终在 -> 轮询 10 次都不为空 -> 升级 SIGKILL
        with mock.patch.object(module, "chrome_pids_for_user_data_dir", return_value=[333]), \
             mock.patch.object(module, "terminate_process",
                               side_effect=lambda pid, force=False: terminated.append((pid, force))), \
             mock.patch.object(module.time, "sleep"):
            stopped = module.stop_cdp_chrome("/fake/scraper-profile")

        self.assertEqual(stopped, 1)
        # 先 SIGTERM（force=False），10 次轮询后升级 SIGKILL（force=True）
        self.assertIn((333, False), terminated)
        self.assertIn((333, True), terminated)
        self.assertLess(terminated.index((333, False)), terminated.index((333, True)))

    def test_run_stop_chrome_closes_dedicated_profile(self):
        module = load_module()

        with tempfile_profile() as paths:
            scraper_dir = str(paths["cdp_profile"])
            captured = {}

            def fake_prepare(**kwargs):
                # run_stop_chrome 必须以 copy_login_state=False, reset=False 调用（只定位，不动 profile）
                captured["prepare_kwargs"] = kwargs
                return {"path": scraper_dir, "copied": 0, "reset": False, "copy_login_state": False}

            def fake_stop(directory):
                captured["stopped_dir"] = directory
                return 1

            with mock.patch.object(module, "require_runtime_dependencies", return_value=True), \
                 mock.patch.object(module, "prepare_cdp_profile", side_effect=fake_prepare), \
                 mock.patch.object(module, "stop_cdp_chrome", side_effect=fake_stop):
                rc = module.run_stop_chrome()

            self.assertEqual(rc, 0)
            # 只定位 profile，绝不复制登录态 / 重置
            self.assertEqual(captured["prepare_kwargs"], {"copy_login_state": False, "reset": False})
            # 关的就是 scraper 隔离 profile 目录
            self.assertEqual(captured["stopped_dir"], scraper_dir)

    def test_run_stop_chrome_returns_zero_when_no_chrome_running(self):
        module = load_module()

        with tempfile_profile() as paths:
            scraper_dir = str(paths["cdp_profile"])
            with mock.patch.object(module, "require_runtime_dependencies", return_value=True), \
                 mock.patch.object(module, "prepare_cdp_profile",
                                   return_value={"path": scraper_dir, "copied": 0,
                                                 "reset": False, "copy_login_state": False}), \
                 mock.patch.object(module, "stop_cdp_chrome", return_value=0):
                rc = module.run_stop_chrome()
            self.assertEqual(rc, 0)

    def test_help_does_not_require_cdp_runtime_dependencies(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--help"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )

        self.assertEqual(result.returncode, 0)
        self.assertIn("--setup-chrome", result.stdout)
        self.assertIn("--reset-chrome-profile", result.stdout)
        self.assertIn("--no-wait-login", result.stdout)
        self.assertIn("--login-timeout", result.stdout)
        self.assertIn("--stop-chrome", result.stdout)
        self.assertIn("--close-chrome", result.stdout)
        self.assertIn("--verbose", result.stdout)
        self.assertIn("--concurrency", result.stdout)


class _FakeFile:
    """记录 write/flush/fsync 调用的假文件对象。"""

    def __init__(self):
        self.flushed = False
        self.fsynced = False

    def write(self, s):
        pass

    def flush(self):
        self.flushed = True

    def fileno(self):
        return 1

    def fsync(self):
        self.fsynced = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class BestPracticesBatch2Tests(unittest.TestCase):
    """第二批最佳实践：argparse 分组+退出码 / fsync+tmp 清扫 / 入口 schema 校验 / -v/-q。"""

    CONSUMER_VALIDATOR = (
        pathlib.Path(__file__).resolve().parents[0]
        / "fixtures" / "consumer_validator" / "scripts" / "validate_export.py"
    )

    # ----- #6 argparse 分组 + 退出码文档化 -----

    def test_parser_help_groups_arguments_and_shows_defaults(self):
        """--help 按分组展示（不再是一堵墙），且显示参数默认值。"""
        module = load_module()
        parser = module.build_parser()
        help_text = parser.format_help()
        for title in ("搜索参数", "筛选参数", "输出参数", "详情抓取",
                      "工具命令", "Chrome 管理", "通用参数"):
            self.assertIn(title, help_text, f"help 缺少分组: {title}")
        self.assertIn("default: 3", help_text,
                      "ArgumentDefaultsHelpFormatter 应显示默认值")

    def test_main_catch_all_prints_clean_message_without_traceback(self):
        """未预期异常 → 干净错误消息（无 traceback）+ 退出码 1。"""
        module = load_module()
        with mock.patch.object(module, "run_cli",
                               side_effect=RuntimeError("boom")) as run_cli, \
             mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            with self.assertRaises(SystemExit) as exit_context:
                module.main()
        self.assertEqual(exit_context.exception.code, 1)
        self.assertIn("boom", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())
        run_cli.assert_called_once()

    # ----- #7 原子写 fsync + 启动清扫 .tmp -----

    def test_atomic_write_json_fsyncs_before_replace(self):
        """写盘前 flush+fsync（断电不丢数据），再 os.replace 原子替换。"""
        module = load_module()
        with tempfile_profile() as paths:
            target = str(paths["cdp_profile"] / "out.json")
            fake = _FakeFile()
            with mock.patch("builtins.open", return_value=fake), \
                 mock.patch.object(module.os, "fsync") as fsync, \
                 mock.patch.object(module.os, "replace") as replace:
                module._atomic_write_json(target, {"jobs": []})
            self.assertTrue(fake.flushed, "写盘前应 flush")
            fsync.assert_called_once_with(1)
            replace.assert_called_once()

    def test_scrape_lock_write_fsyncs(self):
        """锁文件原子写同样补 fsync（与 _atomic_write_json 同级保障）。"""
        module = load_module()
        fake = _FakeFile()
        with mock.patch("builtins.open", return_value=fake), \
             mock.patch.object(module.os, "fsync") as fsync, \
             mock.patch.object(module.os, "replace") as replace:
            module._write_scrape_lock(1, ["123"])
        fsync.assert_called_once_with(1)
        replace.assert_called_once()

    def test_cleanup_stale_tmp_files_removes_only_stale(self):
        """启动清扫：超过保留期（默认 300s）的 .tmp 残留删除，新鲜的不误删。"""
        module = load_module()
        with tempfile_profile() as paths:
            result_dir = paths["cdp_profile"] / "job-result"
            result_dir.mkdir(parents=True, exist_ok=True)
            stale = result_dir / "boss_jobs_x.json.tmp999"
            stale.write_text("x", encoding="utf-8")
            old_ts = time.time() - 3600
            os.utime(stale, (old_ts, old_ts))
            fresh = result_dir / "boss_jobs_y.json.tmp888"
            fresh.write_text("y", encoding="utf-8")
            module.cleanup_stale_tmp_files(result_dir, max_age_seconds=300)
            self.assertFalse(stale.exists(), "超过保留期应删除")
            self.assertTrue(fresh.exists(), "新鲜 tmp 不应误删")

    # ----- 入口 schema 校验（写盘前必填字段 quarantine）-----

    def test_flush_jobs_quarantines_jobs_missing_required_fields(self):
        """缺必填字段的 job 从导出剔除并记入 meta.quarantine（附原因）。"""
        module = load_module()
        with tempfile_profile() as paths:
            target = str(paths["cdp_profile"] / "jobs.json")
            good = {"job_id": "a", "title": "T", "location": "深圳",
                    "job_link": "https://www.zhipin.com/job_detail/x.html",
                    "company_name": "某科技"}
            bad = {"job_id": "b", "title": "", "location": "深圳",
                   "job_link": "https://www.zhipin.com/job_detail/x.html",
                   "company_name": "某科技"}
            module.flush_jobs(target, {"keyword": "AI"}, [good, bad])
            with open(target, encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual([j["job_id"] for j in data["jobs"]], ["a"])
            self.assertEqual(data["job_count"], 1)
            self.assertEqual(data["quarantine"][0]["job_id"], "b")
            self.assertIn("title", data["quarantine"][0]["reason"])

    def test_flush_jobs_no_quarantine_key_when_all_valid(self):
        """全部合法时 meta 不出现 quarantine 键（保持输出干净）。"""
        module = load_module()
        with tempfile_profile() as paths:
            target = str(paths["cdp_profile"] / "jobs.json")
            good = {"job_id": "a", "title": "T", "location": "深圳",
                    "job_link": "https://www.zhipin.com/job_detail/x.html",
                    "company_name": "某科技"}
            module.flush_jobs(target, {"keyword": "AI"}, [good])
            with open(target, encoding="utf-8") as f:
                data = json.load(f)
            self.assertNotIn("quarantine", data, "全部合法时不应出现 quarantine 键")

    def test_flush_jobs_quarantine_passes_vendor_validator(self):
        """quarantine 是 meta 扩展，不破坏消费端契约校验（vendor v1.0.0 ok=True）。"""
        module = load_module()
        with tempfile_profile() as paths:
            target = str(paths["cdp_profile"] / "jobs.json")
            good = {"job_id": "a", "title": "T", "location": "深圳",
                    "job_link": "https://www.zhipin.com/job_detail/x.html",
                    "company_name": "某科技"}
            bad = {"job_id": "b", "title": "", "location": "深圳",
                   "job_link": "https://www.zhipin.com/job_detail/x.html",
                   "company_name": "某科技"}
            module.flush_jobs(target, {"keyword": "AI"}, [good, bad])
            result = subprocess.run(
                [sys.executable, str(BestPracticesBatch2Tests.CONSUMER_VALIDATOR),
                 str(target)],
                capture_output=True, text=True, encoding="utf-8", timeout=60,
            )
            out = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0,
                             f"quarantine 产物应通过契约校验:\n{out}")
            self.assertIn("ok=True", out)

    # ----- -v/-q verbosity -----

    def test_parser_accepts_verbose_count_and_quiet(self):
        """-v 可叠加（count）、-q 独立开关；默认 0/False。"""
        module = load_module()
        parser = module.build_parser()
        args = parser.parse_args(["-vv", "-q"])
        self.assertEqual(args.verbose, 2)
        self.assertTrue(args.quiet)
        args2 = parser.parse_args([])
        self.assertEqual(args2.verbose, 0)
        self.assertFalse(args2.quiet)

    def test_apply_verbosity_maps_to_log_levels(self):
        """verbosity 映射：默认 INFO / -v DEBUG / -q WARNING（quiet 优先）。"""
        module = load_module()
        root = logging.getLogger()
        module._apply_verbosity(0, False)
        self.assertEqual(root.level, logging.INFO)
        module._apply_verbosity(1, False)
        self.assertEqual(root.level, logging.DEBUG)
        module._apply_verbosity(0, True)
        self.assertEqual(root.level, logging.WARNING)
        module._apply_verbosity(2, True)
        self.assertEqual(root.level, logging.WARNING, "quiet 优先于 verbose")


class BestPracticesBatch3Tests(unittest.TestCase):
    """第三轮调研实施：flag 互斥/CLI 误用退出码/subprocess UTF-8/端口/风控码/scrubber/CDP 会话。"""

    # ----- E 梯度降级（修订版：验证码命中 → 全停 → 返回）+ B observed_jobs -----

    def test_serial_stops_all_on_risk_timeout(self):
        """E 降级：详情验证码命中 → 后续 job 不再处理 + 降级提示 + 列表文件 warnings 更新。"""
        module = load_module()
        jobs = [{"job_id": f"j{i}", "title": f"T{i}",
                 "job_link": f"https://www.zhipin.com/job/{i}"} for i in range(3)]
        calls = []

        def fake_one(job, cdp_port, stop_event=None, limiter=None, verbose=False):
            calls.append(job["job_id"])
            return {"ok": False, "detail": None, "job_id": job["job_id"],
                    "reason": "risk_timeout", "message": "验证码命中"}

        with tempfile_profile() as paths:
            out = str(paths["cdp_profile"] / "details.json")
            list_path = str(paths["cdp_profile"] / "jobs.json")
            os.makedirs(paths["cdp_profile"], exist_ok=True)
            with open(list_path, "w", encoding="utf-8") as f:
                json.dump({"keyword": "AI", "jobs": []}, f)
            with mock.patch.object(module, "_scrape_one_detail", new=fake_one), \
                 mock.patch.object(module, "send_alert"), \
                 mock.patch.object(module, "load_existing_detail_ids",
                                   return_value=set()), \
                 mock.patch.object(module, "load_pending_ids",
                                   return_value={}), \
                 mock.patch.object(module.time, "sleep"), \
                 mock.patch("sys.stdout",
                            new_callable=__import__("io").StringIO) as outbuf:
                module.scrape_details({"jobs": jobs}, output_path=out,
                                      cdp_port=9222, concurrency=1,
                                      list_output_path=list_path)
            printed = outbuf.getvalue()
            with open(list_path, encoding="utf-8") as f:
                meta = json.load(f)
        self.assertEqual(len(calls), 1, "验证码命中后不应继续处理剩余详情")
        self.assertIn("验证码命中", printed, "应输出降级提示")
        self.assertIn("已全部停止", printed)
        self.assertTrue(any("detail_risk_blocked" in w for w in meta.get("warnings", [])),
                        "列表文件 warnings 应含降级原因")

    def test_parallel_stops_all_on_risk_timeout(self):
        """E 降级（并行）：risk_timeout 触发全局 stop，后续不再提交。"""
        module = load_module()
        jobs = [{"job_id": f"j{i}", "title": f"T{i}",
                 "job_link": f"https://www.zhipin.com/job/{i}"} for i in range(6)]
        calls = []

        def fake_worker(job, cdp_port, stop_event=None, limiter=None, verbose=False):
            calls.append(job["job_id"])
            return {"ok": False, "detail": None, "job_id": job["job_id"],
                    "reason": "risk_timeout", "message": "验证码命中"}

        with mock.patch.object(module, "_scrape_one_detail", new=fake_worker), \
             mock.patch.object(module, "send_alert"), \
             mock.patch.object(module, "AdaptiveRateLimiter") as limiter_cls, \
             mock.patch.object(module, "load_existing_detail_ids",
                               return_value=set()), \
             mock.patch.object(module, "load_pending_ids", return_value={}), \
             mock.patch.object(module.time, "sleep"), \
             mock.patch("sys.stdout",
                        new_callable=__import__("io").StringIO) as outbuf:
            module._scrape_details_parallel(
                jobs, cdp_port=9222, concurrency=2,
                limiter=limiter_cls.return_value)
        printed = outbuf.getvalue()
        self.assertLess(len(calls), len(jobs),
                        "risk_timeout 后不应处理全部任务（应被全局停止）")
        self.assertIn("已全部停止", printed, "应输出降级提示")

    def test_scrape_list_emits_observed_jobs(self):
        """B 方案 A：meta 含 observed_jobs（本 run 观察集合）+ mode=incremental。"""
        module = load_module()
        cdp = mock.Mock()

        def fake_eval_js(script, sid=None):
            if "xhr.open" not in script:
                return None
            m = re.search(r"page=(\d+)", script)
            pg = int(m.group(1)) if m else 1
            if pg > 2:
                return json.dumps([])
            jobs = [{"job_id": f"job-{pg}-{i}", "title": f"T{pg}-{i}",
                     "job_link": f"https://www.zhipin.com/job/{pg}-{i}",
                     "salary": "20-40K", "boss_name": f"公司{pg}-{i}",
                     "location": "上海·浦东", "company_name": f"公司{pg}-{i}"}
                    for i in range(2)]
            return json.dumps(jobs)

        cdp.eval_js.side_effect = fake_eval_js
        with tempfile_profile() as paths:
            out = str(paths["cdp_profile"] / "jobs.json")
            with mock.patch.object(module, "resolve_city",
                                   return_value=("上海", "101020100")), \
                 mock.patch.object(module, "CDPSession", return_value=cdp), \
                 mock.patch.object(module, "create_page_session",
                                   return_value=("t", "s")), \
                 mock.patch.object(module, "probe_risk_page", return_value={}), \
                 mock.patch.object(module.time, "sleep"), \
                 mock.patch("sys.stdout",
                            new_callable=__import__("io").StringIO):
                module.scrape_list("AI", "上海", 3, {}, out)
            with open(out, encoding="utf-8") as f:
                data = json.load(f)
        observed = data.get("observed_jobs", [])
        self.assertEqual(len(observed), 4, "2 页各 2 条 = 4 个观察 id")
        self.assertEqual(data.get("mode"), "incremental")
        file_ids = {j["job_id"] for j in data["jobs"]}
        self.assertEqual(set(observed), file_ids,
                         "observed_jobs 应等于本 run 观察集合（与文件 jobs 的 job_id 一致）")
        self.assertEqual(len(observed), len(set(observed)), "观察集合应去重")

    # ----- 告警推送模块（规格侧 Worker /webhook/alert）-----

    def test_load_alert_config_reads_env_file(self):
        """.env 解析：ALERT_WEBHOOK_URL/ALERT_WEBHOOK_TOKEN 读取；缺失返回空配置。"""
        module = load_module()
        with tempfile_profile() as paths:
            env = str(paths["cdp_profile"] / ".env")
            os.makedirs(paths["cdp_profile"], exist_ok=True)
            with open(env, "w", encoding="utf-8") as f:
                f.write("ALERT_WEBHOOK_URL=https://example.com/alert\n")
                f.write("ALERT_WEBHOOK_TOKEN=secret-token\n")
                f.write("OTHER_KEY=ignored\n")
            cfg = module.load_alert_config(env)
            self.assertEqual(cfg["url"], "https://example.com/alert")
            self.assertEqual(cfg["token"], "secret-token")
            cfg2 = module.load_alert_config("/nonexistent/.env")
            self.assertEqual(cfg2, {"url": "", "token": ""})

    def test_send_alert_returns_true_on_200(self):
        """send_alert：200 → True；配置缺失 → False 不抛；失败不抛异常。"""
        module = load_module()
        fake_requests = mock.Mock()
        fake_requests.post.return_value.status_code = 200
        with mock.patch.object(module, "load_alert_config",
                               return_value={"url": "https://example.com/alert",
                                             "token": "t"}), \
             mock.patch.object(module, "requests", fake_requests) as fr:
            self.assertTrue(module.send_alert("标题", "内容"))
            _, kwargs = fr.post.call_args
            self.assertEqual(kwargs["headers"]["Authorization"], "Bearer t")
            body = kwargs["json"]
            self.assertEqual(body["source"], "boss-zhipin-scraper")
        fake_requests2 = mock.Mock()
        fake_requests2.post.side_effect = ConnectionError("net down")
        with mock.patch.object(module, "load_alert_config",
                               return_value={"url": "https://example.com/alert",
                                             "token": "t"}), \
             mock.patch.object(module, "requests", fake_requests2):
            self.assertFalse(module.send_alert("标题", "内容"),
                             "发送失败应返回 False 不抛异常")
        with mock.patch.object(module, "load_alert_config",
                               return_value={"url": "", "token": ""}):
            self.assertFalse(module.send_alert("标题", "内容"),
                             "配置缺失应静默禁用")

    # ----- C 组：动作型 flag 互斥 + parser.error 语义 -----

    def test_parser_rejects_conflicting_action_flags(self):
        """同时给多个动作型命令（--check + --verify）→ argparse 拒绝 exit 2。"""
        module = load_module()
        parser = module.build_parser()
        for flags in (["--check", "--verify"], ["--list-results", "--archive"],
                      ["--setup-chrome", "--stop-chrome"]):
            with self.assertRaises(SystemExit) as ctx:
                parser.parse_args(flags)
            self.assertEqual(ctx.exception.code, 2, f"冲突 flag 应 exit 2: {flags}")

    def test_parser_allows_auxiliary_flags_with_setup_chrome(self):
        """--setup-chrome 的辅助 flag（--no-wait-login/--login-timeout）不被互斥组误伤。"""
        module = load_module()
        parser = module.build_parser()
        args = parser.parse_args(["--setup-chrome", "--no-wait-login",
                                  "--login-timeout", "60"])
        self.assertTrue(args.setup_chrome)
        self.assertTrue(args.no_wait_login)
        self.assertEqual(args.login_timeout, 60)

    def test_main_invalid_archive_arg_exits_2(self):
        """--archive 非整数是 CLI 误用 → exit 2（不再 exit 1）。"""
        module = load_module()
        with mock.patch.object(sys, "argv", ["boss_cdp_raw.py", "--archive", "abc"]), \
             mock.patch.object(module, "require_runtime_dependencies",
                               return_value=True), \
             mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            with self.assertRaises(SystemExit) as ctx:
                module.main()
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("--archive", err.getvalue())

    # ----- B 组：未知风控码归受限 -----

    def test_probe_unknown_nonzero_code_returns_restricted(self):
        """未知非零 code（不在已知风控集合）→ RESTRICTED（降速语义），不再 RESPONSE_ERROR。"""
        module = load_module()
        result = module.classify_login_probe_response(
            {"code": 9999, "message": "新风控形态"})
        self.assertEqual(result.status, module.LoginProbeStatus.RESTRICTED)

    # ----- B 组：凭证 scrubber -----

    def test_scrub_secrets_redacts_credentials(self):
        """日志/错误输出中的 cookie/token/__zp_stoken__ 值被脱敏。"""
        module = load_module()
        text = "URL: https://www.zhipin.com/wapi?__zp_stoken__=abcdef&wt2=secret"
        scrubbed = module._scrub_secrets(text)
        self.assertNotIn("abcdef", scrubbed)
        self.assertNotIn("secret", scrubbed)
        self.assertIn("__zp_stoken__=***", scrubbed)

    def test_main_catch_all_scrubs_secrets_in_error(self):
        """catch-all 打印的错误消息经 scrubber 脱敏（凭证不进 stderr）。"""
        module = load_module()
        with mock.patch.object(module, "run_cli",
                               side_effect=RuntimeError(
                                   "failed: __zp_stoken__=topsecret123")), \
             mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            with self.assertRaises(SystemExit):
                module.main()
        self.assertNotIn("topsecret123", err.getvalue())

    # ----- A 组：CDP 会话心跳/快速失败 -----

    def test_cdp_session_send_fails_fast_when_connection_dead(self):
        """连接标记死亡后 send 立即抛错（不等 30s 超时挂起）。"""
        module = load_module()
        fake_requests = mock.Mock()
        fake_requests.get.return_value.json.return_value = {
            "webSocketDebuggerUrl": "ws://x"}
        fake_websocket = mock.Mock()
        fake_websocket.create_connection.return_value = mock.Mock()
        with mock.patch.object(module, "require_runtime_dependencies",
                               return_value=True), \
             mock.patch.object(module, "requests", fake_requests), \
             mock.patch.object(module, "websocket", fake_websocket):
            sess = module.CDPSession(9999)
            sess._dead = True
            with self.assertRaises(ConnectionError):
                sess.send("Browser.getVersion")

    def test_cdp_session_recv_break_detects_target_crashed_event(self):
        """收到 Inspector.detached / Target.targetCrashed 事件 → 抛目标崩溃异常（快速失败）。"""
        module = load_module()
        sess = object.__new__(module.CDPSession)
        sess.mid = 5
        sess.ws = mock.Mock()
        sess._dead = False
        sess.ws.recv.side_effect = [
            json.dumps({"method": "Inspector.detached",
                        "params": {"reason": "Render process gone.", "sessionId": "s1"}}),
        ]
        with self.assertRaises(module.TargetCrashedError):
            sess.send("Runtime.evaluate", {"expression": "1"}, "s1")

    # ----- 中价值：run 级血缘字段 + 键冲突检测（flush_jobs）-----

    def test_flush_jobs_accumulates_record_counts_across_writes(self):
        """渐进写盘多次 flush：record_counts.new/duplicate/quarantine 跨次累积。"""
        module = load_module()
        with tempfile_profile() as paths:
            target = str(paths["cdp_profile"] / "jobs.json")

            def full(jid):
                return {"job_id": jid, "title": f"T-{jid}", "location": "深圳",
                        "job_link": f"https://www.zhipin.com/job_detail/{jid}.html",
                        "company_name": "某科技"}

            module.flush_jobs(target, {"keyword": "AI", "run_id": "r1"}, [full("a")])
            module.flush_jobs(target, {"keyword": "AI", "run_id": "r1"},
                              [full("a"), full("b")])
            with open(target, encoding="utf-8") as f:
                data = json.load(f)
            counts = data["record_counts"]
            self.assertEqual(counts["new"], 2, "两次写入共新增 2 个新 job")
            self.assertEqual(counts["duplicate"], 1, "第二次写入中 job a 是重复")
            self.assertEqual(data["run_id"], "r1", "血缘 run_id 应保留")

    def test_flush_jobs_quarantines_key_conflict(self):
        """同 job_id 不同 payload（旧版本已存在）→ 记 key_conflict 不静默覆盖。"""
        module = load_module()
        with tempfile_profile() as paths:
            target = str(paths["cdp_profile"] / "jobs.json")

            def full(jid, title):
                return {"job_id": jid, "title": title, "location": "深圳",
                        "job_link": f"https://www.zhipin.com/job_detail/{jid}.html",
                        "company_name": "某科技"}

            module.flush_jobs(target, {"keyword": "AI"}, [full("a", "旧标题")])
            module.flush_jobs(target, {"keyword": "AI"}, [full("a", "新标题")])
            with open(target, encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(data["jobs"][0]["title"], "旧标题", "旧版本优先不被覆盖")
            self.assertIn("key_conflict", data["quarantine"][0]["reason"])

    # ----- 中价值：冷却恢复分级 -----

    def test_cdp_cooldown_writes_recovery_phase(self):
        """熔断冷却标记带恢复期：冷却结束前恢复期生效，冷却后恢复期结束。"""
        module = load_module()
        with tempfile_profile() as paths:
            cooldown = str(paths["cdp_profile"] / "cdp.cooldown")
            with mock.patch.object(module, "SCRAPE_LOCK_PATH",
                                   str(paths["cdp_profile"] / "scrape.lock")):
                module.mark_cdp_cooldown(seconds=5)
                recovery = module.check_cdp_recovery()
                self.assertGreater(recovery, 0, "冷却期间应处于恢复期")
                remaining = module.check_cdp_cooldown()
                self.assertGreater(remaining, 0, "冷却检查兼容（读首行）")
                deadline = time.time() + 5 + module.CDP_RECOVERY_SECONDS + 1
                with open(cooldown, "w", encoding="utf-8") as f:
                    f.write(f"{time.time() - 1}\n{deadline}\n")
                self.assertEqual(module.check_cdp_cooldown(), None, "冷却结束")
                self.assertGreater(module.check_cdp_recovery(), 0, "冷却结束仍在恢复期")

    # ----- 中价值：预算分账 -----

    def test_incr_request_tracks_per_kind_budget(self):
        """请求计数分账：probe/list/detail 各自累计，总量语义不变。"""
        module = load_module()
        before = dict(module._request_budget)
        module.incr_request("probe")
        module.incr_request("detail")
        self.assertEqual(module._request_budget["probe"], before["probe"] + 1)
        self.assertEqual(module._request_budget["detail"], before["detail"] + 1)
        self.assertEqual(module._request_budget["list"], before["list"])


class tempfile_profile:
    def __enter__(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        source_profile = root / "Google" / "Chrome"
        default = source_profile / "Default"
        default.mkdir(parents=True)
        for name in ["Cookies", "Cookies-journal", "Login Data", "Web Data"]:
            (default / name).write_text(name, encoding="utf-8")
        network = default / "Network"
        network.mkdir()
        (network / "Cookies").write_text("network cookies", encoding="utf-8")
        (source_profile / "Local State").write_text("state", encoding="utf-8")
        self.paths = {
            "source_profile": source_profile,
            "cdp_profile": root / "persistent-profile",
        }
        return self.paths

    def __exit__(self, exc_type, exc, tb):
        self.tmp.cleanup()


def fake_run(calls, *args, **kwargs):
    calls["run"].append(args[0])
    return type("Completed", (), {"stdout": "", "returncode": 0})()


def chrome_cmdline(cdp_port, user_data_dir):
    """返回符合当前平台的 chrome 命令行（unquoted user-data-dir，测试 unquoted 解析）。"""
    if platform.system() == "Windows":
        exe = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"
        return f'{exe} --remote-debugging-port={cdp_port} --user-data-dir={user_data_dir}'
    return (
        f"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome "
        f"--remote-debugging-port={cdp_port} --user-data-dir={user_data_dir}"
    )


def process_query_stdout(entries):
    """把 (pid, cmdline) 列表转成 iter_chrome_process_commands 当前平台的 mock 输出。

    Windows 分支解析 PowerShell ConvertTo-Json 输出；POSIX 分支解析 ps 行文本。
    这两个分支的输出格式不同，测试必须按平台提供对应格式，否则在另一平台解析为空。
    """
    if platform.system() == "Windows":
        return json.dumps(
            [{"ProcessId": pid, "CommandLine": cmdline} for pid, cmdline in entries]
        )
    return "".join(f"{pid} {cmdline}\n" for pid, cmdline in entries)


ROOT_PATH = SCRIPT_PATH.parents[1]


def _normalize_version(raw):
    """统一版本号格式，去掉 'v' 前缀和 patch 段，只比较 major.minor。

    README/SKILL.md 里常写成 'v2.0'，pyproject/脚本里是 '2.0.0'，
    只要 major.minor 一致即视为同步，避免 patch 号差异造成误报。
    """
    text = str(raw).strip().lstrip("vV")
    parts = text.split(".")
    major = parts[0] if len(parts) > 0 else "0"
    minor = parts[1] if len(parts) > 1 else "0"
    return f"{major}.{minor}"


class VersionConsistencyTests(unittest.TestCase):
    """校验版本号在 README / pyproject.toml / SKILL.md / 脚本四处保持一致。

    发版时只改一处会漏掉其他几处，这个测试在 CI/本地跑测试时就能拦住。
    """

    def _read_text(self, name):
        return (ROOT_PATH / name).read_text(encoding="utf-8")

    def test_script_version_is_defined(self):
        module = load_module()
        self.assertTrue(getattr(module, "__version__", None),
                        "脚本缺少 __version__")

    def test_versions_are_in_sync_across_all_sources(self):
        module = load_module()
        script_ver = _normalize_version(module.__version__)

        # pyproject.toml: version = "2.0.0"
        pyproject = self._read_text("pyproject.toml")
        m = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE)
        self.assertIsNotNone(m, "pyproject.toml 未找到 version 字段")
        pyproject_ver = _normalize_version(m.group(1))

        # SKILL.md frontmatter: version: 2.0.0
        skill = self._read_text("SKILL.md")
        m = re.search(r"^version:\s*([^\n]+)$", skill, re.MULTILINE)
        self.assertIsNotNone(m, "SKILL.md 未找到 version 字段")
        skill_ver = _normalize_version(m.group(1))

        # README.md 标题: # ... v2.0
        readme = self._read_text("README.md")
        m = re.search(r"v(\d+\.\d+(?:\.\d+)?)", readme)
        self.assertIsNotNone(m, "README.md 未找到版本号")
        readme_ver = _normalize_version(m.group(1))

        self.assertEqual(script_ver, pyproject_ver,
                         f"脚本({script_ver}) 与 pyproject.toml({pyproject_ver}) 版本不一致")
        self.assertEqual(script_ver, skill_ver,
                         f"脚本({script_ver}) 与 SKILL.md({skill_ver}) 版本不一致")
        self.assertEqual(script_ver, readme_ver,
                         f"脚本({script_ver}) 与 README.md({readme_ver}) 版本不一致")


class ProjectScopeTests(unittest.TestCase):
    """项目边界守卫：只保留抓取和聚合分析，不内置简历匹配打分。"""

    def _read_text(self, name):
        return (ROOT_PATH / name).read_text(encoding="utf-8")

    def test_resume_matching_feature_is_not_packaged_or_documented(self):
        self.assertFalse(
            (ROOT_PATH / "scripts" / "resume_score.py").exists(),
            "简历匹配打分脚本不应作为项目功能保留",
        )
        self.assertFalse(
            (ROOT_PATH / "tests" / "test_resume_score.py").exists(),
            "删除简历匹配功能时也应删除对应测试",
        )

        combined = "\n".join(
            self._read_text(name)
            for name in ("README.md", "CHANGELOG.md", "SKILL.md", "pyproject.toml", "requirements.txt", "uv.lock")
        )
        for forbidden in (
            "resume_score",
            "pdfplumber",
            "pypdf",
            "python-docx",
            "openai",
            "langchain",
            "sentence-transformers",
            "简历匹配打分",
            "enable-llm",
        ):
            self.assertNotIn(forbidden, combined)


if __name__ == "__main__":
    unittest.main()
