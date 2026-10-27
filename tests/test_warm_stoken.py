"""stoken 热刷新与 code 37 自愈的行为锁定（issue #195）。

离线：CDP 会话全部 mock，不打开真实 Chrome、不发 BOSS 请求。
"""

import contextlib
import importlib.util
import io
import pathlib
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class WarmStokenTests(unittest.TestCase):

    def setUp(self):
        self.module = load_module("warm_stoken", SCRIPTS / "warm_stoken.py")
        self.tmp = tempfile.TemporaryDirectory(prefix="warm-stoken-")
        self.addCleanup(self.tmp.cleanup)

    def test_search_url_encodes_query_and_city(self):
        url = self.module.build_search_url("AI产品实习生", "101210100")
        self.assertIn("query=AI%E4%BA%A7%E5%93%81%E5%AE%9E%E4%B9%A0%E7%94%9F", url)
        self.assertIn("city=101210100", url)

    def test_wait_seconds_precedence(self):
        self.assertEqual(self.module.wait_seconds(30), 30)
        with mock.patch.dict("os.environ", {"BOSS_WARM_STOKEN_SECONDS": "90"}):
            self.assertEqual(self.module.wait_seconds(), 90)
            self.assertEqual(self.module.wait_seconds(10), 10)
        with mock.patch.dict("os.environ", {}, clear=False):
            self.assertEqual(self.module.wait_seconds(), self.module.DEFAULT_WAIT_SECONDS)

    def test_warm_stoken_navigates_waits_and_closes_tab(self):
        fake_cdp = mock.Mock()
        fake_session = types.SimpleNamespace(
            CDPSession=mock.Mock(return_value=fake_cdp),
            create_page_session=mock.Mock(return_value=("tid-1", "sid-1")),
            page_background_default=lambda: True,
        )
        with mock.patch.object(self.module, "_cdp", return_value=fake_session), \
                mock.patch.object(self.module.time, "sleep") as sleep:
            summary = self.module.warm_stoken(45222, seconds=5)
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["waited"], 5)
        sleep.assert_called_once_with(5)
        navigate = [call for call in fake_cdp.send.call_args_list
                    if call.args and call.args[0] == "Page.navigate"]
        self.assertEqual(len(navigate), 1)
        self.assertIn("zhipin.com/web/geek/job", navigate[0].args[1]["url"])
        self.assertTrue(any(call.args and call.args[0] == "Target.closeTarget"
                            for call in fake_cdp.send.call_args_list))
        fake_cdp.close.assert_called_once()


class LoginProbeWarmTests(unittest.TestCase):
    """code 37 命中 → 热刷新一次 → 重探；仍失败才保持受限（不循环）。"""

    def setUp(self):
        self.module = load_module("login_probe", SCRIPTS / "login_probe.py")
        self.module._warm_disabled = lambda: False
        self.module._warm_once = mock.Mock(return_value=True)
        self.fake_core = types.SimpleNamespace(
            _LOGIN_PROBE_CACHE={"ts": 0.0, "result": None},
            LOGIN_PROBE_CACHE_TTL=600,
            DEFAULT_CDP_PORT=45222,
            time=__import__("time"),
        )
        self.module._core = lambda: self.fake_core

    def test_restricted_37_warms_then_retries_once(self):
        restricted = self.module.LoginProbeResult(
            self.module.LoginProbeStatus.RESTRICTED, code=37, message="环境异常")
        available = self.module.LoginProbeResult(
            self.module.LoginProbeStatus.AVAILABLE, code=0)
        self.module._probe_login_state_uncached = mock.Mock(
            side_effect=[restricted, available])
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            result = self.module.check_login_state(45222)
        self.assertIs(result.status, self.module.LoginProbeStatus.AVAILABLE)
        self.assertEqual(self.module._probe_login_state_uncached.call_count, 2)
        self.module._warm_once.assert_called_once_with(45222)
        self.assertIn("code 37", buffer.getvalue())

    def test_restricted_37_after_warm_stays_restricted(self):
        restricted = self.module.LoginProbeResult(
            self.module.LoginProbeStatus.RESTRICTED, code=37, message="环境异常")
        self.module._probe_login_state_uncached = mock.Mock(
            side_effect=[restricted, restricted])
        with contextlib.redirect_stdout(io.StringIO()):
            result = self.module.check_login_state(45222)
        self.assertIs(result.status, self.module.LoginProbeStatus.RESTRICTED)
        self.assertEqual(self.module._probe_login_state_uncached.call_count, 2)

    def test_warm_disabled_skips(self):
        self.module._warm_disabled = lambda: True
        restricted = self.module.LoginProbeResult(
            self.module.LoginProbeStatus.RESTRICTED, code=37, message="环境异常")
        self.module._probe_login_state_uncached = mock.Mock(return_value=restricted)
        result = self.module.check_login_state(45222)
        self.assertIs(result.status, self.module.LoginProbeStatus.RESTRICTED)
        self.assertEqual(self.module._probe_login_state_uncached.call_count, 1)
        self.module._warm_once.assert_not_called()

    def test_warm_disabled_under_unittest_by_default(self):
        # 真实函数（未被 setUp 替换）：测试进程里默认不做 75s 热刷新
        fresh = load_module("login_probe_fresh", SCRIPTS / "login_probe.py")
        self.assertTrue(fresh._warm_disabled())
        with mock.patch.dict("os.environ", {"BOSS_WARM_STOKEN_FORCE": "1"}):
            self.assertFalse(fresh._warm_disabled())
        with mock.patch.dict("os.environ", {"BOSS_NO_WARM_STOKEN": "1"}):
            self.assertTrue(fresh._warm_disabled())


if __name__ == "__main__":
    unittest.main()
