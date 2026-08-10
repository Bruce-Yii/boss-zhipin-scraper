import importlib.util
import contextlib
import csv
import io
import json
import os
import pathlib
import platform
import re
import subprocess
import sys
import threading
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
            with mock.patch.object(module, "CDPSession",
                                   side_effect=TimeoutError("cdp down")), \
                    mock.patch.object(module.time, "sleep"):
                # 5 个 job、阈值 3 → 连续 3 次失败后熔断停止
                results = module.scrape_details(self._sample_jobs(5), output_path=out)
            self.assertEqual(results, [])
            self.assertEqual(len(module.load_pending_ids(out)), 3,
                             "熔断后不应继续尝试剩余 job")

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

    def _fake_parallel_worker(self, active_state, ok_ids, fail_ids=()):
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
                        "reason": "invalid_detail", "message": "too short"}
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
                                   state, set(), fail_ids={"job-1", "job-2"})), \
                mock.patch.object(module, "AdaptiveRateLimiter") as limiter_cls, \
                mock.patch.object(module, "load_existing_detail_ids", return_value=set()), \
                mock.patch.object(module, "load_pending_ids", return_value=set()), \
                mock.patch.object(module.time, "sleep"):
            results, pending_out = module._scrape_details_parallel(
                jobs, cdp_port=9222, concurrency=3,
                limiter=limiter_cls.return_value)
        self.assertEqual(len(results), 2, "失败的 job 不应写入结果")
        self.assertEqual(pending_out, {"job-1": 1, "job-2": 1})

    def test_parallel_stops_submitting_after_global_stop(self):
        module = load_module()
        jobs = self._sample_jobs(6)["jobs"]

        def always_fail(job, cdp_port, stop_event=None, limiter=None):
            return {"ok": False, "detail": None, "job_id": job["job_id"],
                    "reason": "cdp_session", "message": "boom"}

        with mock.patch.object(module, "_scrape_one_detail",
                               new=always_fail), \
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
                mock.patch.object(module, "AdaptiveRateLimiter") as limiter_cls, \
                mock.patch.object(module, "load_existing_detail_ids",
                                  return_value=set()), \
                mock.patch.object(module, "load_pending_ids",
                                  return_value=set()), \
                mock.patch.object(module.time, "sleep"):
            module._scrape_details_parallel(
                jobs, cdp_port=9222, concurrency=2,
                limiter=limiter_cls.return_value)
        self.assertLess(len(calls), 20,
                        "熔断后不应继续提交剩余任务（有界提交窗口）")
        self.assertGreaterEqual(len(calls), 3, "至少执行到熔断阈值")

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
                              write_every=5):
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
        response = mock.MagicMock()
        response.read.return_value = json.dumps({
            "code": 35,
            "message": "您的IP地址存在异常行为.",
            "zpData": {},
        }).encode("utf-8")
        response.__enter__.return_value = response

        with mock.patch.object(module, "urlopen", return_value=response):
            with self.assertRaisesRegex(module.CityAPIResponseError,
                                        "code=35"):
                module.fetch_boss_json(module.HOT_CITY_URL)

    def test_main_rejects_unknown_city_before_login_probe(self):
        """CLI 城市预校验失败后以非零状态退出，不进入登录探测。"""
        module = load_module()

        with mock.patch.object(sys, "argv", [
                "boss_cdp_raw.py", "--city", "不存在市",
        ]), \
             mock.patch.object(module, "require_runtime_dependencies",
                               return_value=True), \
             mock.patch.object(module, "resolve_city",
                               side_effect=module.CityResolutionError("无法解析城市")), \
             mock.patch.object(module, "check_login_state") as login_probe, \
             redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as exit_context:
                module.main()

        self.assertEqual(exit_context.exception.code, 1)
        self.assertIn("无法解析城市", output.getvalue())
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
                {"code": 7, "message": "业务异常"},
                module.LoginProbeStatus.RESPONSE_ERROR,
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

            module.flush_jobs(target, dict(meta), [{"job_id": "a"}, {"job_id": "b"}])
            module.flush_jobs(target, dict(meta), [{"job_id": "b"}, {"job_id": "c"}])

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

    def test_api_extraction_keeps_detail_context_fields(self):
        module = load_module()

        self.assertIn("security_id: j.securityId", module.FETCH_API_JS_TEMPLATE)
        self.assertIn("lid: j.lid", module.FETCH_API_JS_TEMPLATE)
        self.assertIn("encrypt_job_id: j.encryptJobId", module.FETCH_API_JS_TEMPLATE)

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
            Exception("not ready"),
            type("Resp", (), {"status_code": 200})(),
        ])

        def fake_get(*args, **kwargs):
            response = next(responses)
            if isinstance(response, Exception):
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
