"""登录探测的"请求级受限"处理（2026-10-22 实测驱动的根因修法）。

背景：2026-10-22 08:25 的 10 分钟实测里，**两次列表拉取**都因为登录探测返回
`status=RESTRICTED` 而整次放弃（08:25:15 / 08:28:33，risk_events.jsonl 有记录），
供给只剩 295 岗，10 分钟只跑到 4 分 55 秒就因供给耗尽停住。

根因有两层：

1. **探测被单个目标短路**：探测准备了 3 个 (query, city) 目标，本意是"一个不行换
   下一个"，但代码遇到第一个 RESTRICTED 就直接返回 → 后两个目标根本没试。
2. **code 未分级**：已知的账号级风控码（31/35/36/37/38）与**未知的非零 code** 被
   同样对待，一律"整次判死"。而代码注释写明未知 code 的意图是"降速语义"——**意图与
   实现不一致**。审计里又没记 code，事后无法定位是哪个码。

修法：账号级码 → 立即判定（不变）；未知非零码 → 退避后重试同一目标一次，仍失败才判受限；
审计里带上 code 与 message。
"""

import importlib.util
import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_module():
    spec = importlib.util.spec_from_file_location(
        "boss_cdp_raw_probe", ROOT / "scripts" / "boss_cdp_raw.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class LoginProbeRetryTests(unittest.TestCase):

    def setUp(self):
        self.module = load_module()

    def test_account_level_code_stops_immediately(self):
        """账号级风控码（31/35/36/37/38）→ 立即判定，不重试、不试其它目标。"""
        result = self.module.classify_login_probe_response(
            {"code": 31, "message": "访问受限"})
        self.assertEqual(result.status, self.module.LoginProbeStatus.RESTRICTED)
        self.assertEqual(result.code, 31)
        self.assertIn(31, self.module.LOGIN_RESTRICTED_CODES)

    def test_unknown_code_is_restricted_but_not_account_level(self):
        """未知非零 code → RESTRICTED，但**不在**账号级码集合里（→ 可重试）。"""
        result = self.module.classify_login_probe_response(
            {"code": 9999, "message": "未知"})
        self.assertEqual(result.status, self.module.LoginProbeStatus.RESTRICTED)
        self.assertNotIn(9999, self.module.LOGIN_RESTRICTED_CODES)

    def _run_probe(self, results_per_target):
        """跑一次未缓存探测，按目标顺序喂入预设结果，返回 (结果, 探测次数)。"""
        module = self.module
        calls = []

        def fake_probe(cdp, sid, query="", city_code=""):
            index = len(calls)
            calls.append((query, city_code))
            return results_per_target[index]

        class FakeSession:
            def send(self, method, params=None, sid=None):
                return {}

            def close(self):
                pass

        with mock.patch.object(module, "CDPSession", return_value=FakeSession()), \
                mock.patch.object(module, "create_page_session",
                                  return_value=("tid", "sid")), \
                mock.patch.object(module, "probe_login_state", side_effect=fake_probe), \
                mock.patch.object(module.time, "sleep"):
            result = module._probe_login_state_uncached()
        return result, calls

    def test_request_level_restriction_retries_same_target_once(self):
        """请求级受限（未知码）→ 退避后重试**同一个目标**一次；成功即算可用。"""
        restricted = self.module.LoginProbeResult(
            self.module.LoginProbeStatus.RESTRICTED, code=9999, message="未知")
        available = self.module.LoginProbeResult(
            self.module.LoginProbeStatus.AVAILABLE, code=0)
        result, calls = self._run_probe([restricted, available])
        self.assertEqual(result.status, self.module.LoginProbeStatus.AVAILABLE)
        # 第一次 + 重试一次，都打在同一个目标上
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1])

    def test_request_level_restriction_falls_through_to_next_target(self):
        """第一个目标请求级受限且重试仍受限 → 继续试下一个目标（不再短路）。"""
        restricted = self.module.LoginProbeResult(
            self.module.LoginProbeStatus.RESTRICTED, code=9999, message="未知")
        available = self.module.LoginProbeResult(
            self.module.LoginProbeStatus.AVAILABLE, code=0)
        result, calls = self._run_probe([restricted, restricted, available])
        self.assertEqual(result.status, self.module.LoginProbeStatus.AVAILABLE)
        self.assertGreaterEqual(len(calls), 3)

    def test_account_level_code_does_not_try_other_targets(self):
        """账号级码 → 立即判定，后面的目标不再试。"""
        account = self.module.LoginProbeResult(
            self.module.LoginProbeStatus.RESTRICTED, code=31, message="访问受限")
        available = self.module.LoginProbeResult(
            self.module.LoginProbeStatus.AVAILABLE, code=0)
        result, calls = self._run_probe([account, available])
        self.assertEqual(result.status, self.module.LoginProbeStatus.RESTRICTED)
        self.assertEqual(len(calls), 1)

    def test_audit_records_code_and_message(self):
        """审计必须带 code 与 message（否则事后无法定位是哪个码）。"""
        source = (ROOT / "scripts" / "boss_cdp_raw.py").read_text(encoding="utf-8")
        self.assertIn("status=RESTRICTED ", source)
        self.assertIn("code={login_result.code}", source)
        self.assertIn("message={login_result.message}", source)


if __name__ == "__main__":
    unittest.main()
