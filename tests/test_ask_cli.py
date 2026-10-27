"""``--ask`` 只读分派契约（agent 会话入口）。

全离线：临时 HOME/result_dir + 假 scripts.ask，不发网络、不拿 run 锁。
覆盖：status/today/templates/diff/why 分派、--json 纯 JSON 输出、缺 job_id /
未知子命令 / 交互层缺失 / 交互层抛错 → exit 2，以及只读命令不进锁路径。
"""

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(ROOT))

ENV_KEYS = (
    "BOSS_STATE_DIR", "BOSS_RUN_STATE_PATH", "BOSS_AUDIT_PATH",
    "BOSS_DAILY_BUDGET_PATH", "BOSS_BATCH_HEALTH_PATH", "BOSS_PENDING_DIR",
)


def load_boss(name="boss_cdp_raw_ask_cli"):
    spec = importlib.util.spec_from_file_location(
        name, SCRIPTS / "boss_cdp_raw.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeAsk:
    """假交互层：记录调用参数，可注入返回/异常。"""

    def __init__(self, result=None, raises=None, human=None):
        self.result = result if result is not None else {
            "ok": True, "data": {"demo": 1}, "human": "人类可读", "errors": []}
        self.raises = raises
        self.human_text = human
        self.calls = []

    def run_ask(self, command, args, result_dir=None):
        self.calls.append({"command": command, "args": dict(args),
                           "result_dir": result_dir})
        if self.raises is not None:
            raise self.raises
        return self.result

    def format_answer(self, payload, as_json=False):
        if self.human_text is not None:
            return self.human_text
        return str(payload.get("human") or "")


class AskCliTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ask-cli-")
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.result_dir = os.path.join(self.dir, "job-result")
        os.makedirs(self.result_dir, exist_ok=True)
        self._env_backup = {key: os.environ.get(key) for key in ENV_KEYS}
        self.addCleanup(self._restore_env)
        self.boss = load_boss()
        self.addCleanup(sys.modules.pop, "boss_cdp_raw_ask_cli", None)
        os.environ["BOSS_STATE_DIR"] = os.path.join(self.dir, "state")
        os.environ["BOSS_RUN_STATE_PATH"] = os.path.join(
            self.dir, "state", "run_state.json")
        os.environ["BOSS_AUDIT_PATH"] = os.path.join(
            self.dir, "state", "risk_events.jsonl")
        os.environ["BOSS_DAILY_BUDGET_PATH"] = os.path.join(
            self.dir, "state", "daily_budget.json")
        os.environ["BOSS_BATCH_HEALTH_PATH"] = os.path.join(
            self.dir, "state", "batch_health.jsonl")
        os.environ["BOSS_PENDING_DIR"] = os.path.join(
            self.dir, "state", "pending")
        patcher = mock.patch.object(self.boss, "DEFAULT_RESULT_DIR",
                                    self.result_dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _restore_env(self):
        for key, value in self._env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _run_cli(self, argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        code = None
        with mock.patch.object(sys, "argv", ["boss_cdp_raw.py", *argv]), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            try:
                self.boss.run_cli()
            except SystemExit as exc:
                code = 0 if exc.code is None else exc.code
        return code, stdout.getvalue(), stderr.getvalue()

    def _ask(self, argv, fake):
        with mock.patch.object(self.boss, "_load_ask_module",
                               return_value=fake), \
                mock.patch.object(self.boss, "acquire_scrape_lock") as lock:
            code, out, err = self._run_cli(argv)
        return code, out, err, lock

    # ---- 正常分派 ----

    def test_status_json_is_pure_json_and_passes_result_dir(self):
        fake = FakeAsk()
        code, out, err, lock = self._ask(["--ask", "status", "--json"], fake)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), fake.result)
        self.assertEqual(fake.calls, [{"command": "status", "args": {},
                                       "result_dir": self.result_dir}])
        self.assertEqual(err, "")
        lock.assert_not_called()

    def test_why_passes_job_id(self):
        fake = FakeAsk()
        code, out, _ = self._ask(
            ["--ask", "why", "d86a000c3d5d50e3", "--json"], fake)[:3]
        self.assertEqual(code, 0)
        json.loads(out)
        self.assertEqual(fake.calls[0]["command"], "why")
        self.assertEqual(fake.calls[0]["args"], {"job_id": "d86a000c3d5d50e3"})

    def test_all_readonly_subcommands_are_dispatched(self):
        for command in ("status", "today", "templates", "diff"):
            fake = FakeAsk()
            code, out, _ = self._ask(["--ask", command, "--json"], fake)[:3]
            self.assertEqual(code, 0, command)
            self.assertEqual(fake.calls[0]["command"], command)
            json.loads(out)

    def test_human_output_uses_format_answer(self):
        fake = FakeAsk(human="HUMAN-ANSWER")
        code, out, err, lock = self._ask(["--ask", "today"], fake)
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "HUMAN-ANSWER")
        self.assertEqual(err, "")
        lock.assert_not_called()

    def test_human_output_falls_back_to_human_field(self):
        fake = FakeAsk(result={"ok": True, "human": "FALLBACK", "data": {}})
        # 用一个没有 format_answer 的极简对象
        bare = type("BareAsk", (), {"run_ask": fake.run_ask})()
        with mock.patch.object(self.boss, "_load_ask_module", return_value=bare):
            code, out, _ = self._run_cli(["--ask", "templates"])
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "FALLBACK")

    def test_negative_result_exits_1_but_still_prints_json(self):
        fake = FakeAsk(result={"ok": False, "errors": ["没找到"], "human": "没找到"})
        code, out, _ = self._ask(["--ask", "why", "nope", "--json"], fake)[:3]
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["errors"], ["没找到"])

    # ---- 误用 / 失败（exit 2）----

    def test_why_without_job_id_exits_2(self):
        fake = FakeAsk()
        code, _, err, _ = self._ask(["--ask", "why"], fake)
        self.assertEqual(code, 2)
        self.assertIn("why <job_id>", err)
        self.assertEqual(fake.calls, [])

    def test_unknown_subcommand_exits_2(self):
        fake = FakeAsk()
        code, _, err, _ = self._ask(["--ask", "bogus"], fake)
        self.assertEqual(code, 2)
        self.assertIn("未知 --ask 子命令", err)
        self.assertEqual(fake.calls, [])

    def test_extra_args_for_non_why_exits_2(self):
        fake = FakeAsk()
        code, _, err, _ = self._ask(["--ask", "status", "extra"], fake)
        self.assertEqual(code, 2)
        self.assertIn("不接受额外参数", err)
        self.assertEqual(fake.calls, [])

    def test_missing_ask_module_exits_2(self):
        with mock.patch.object(self.boss, "_load_ask_module",
                               return_value=None):
            code, _, err = self._run_cli(["--ask", "status", "--json"])
        self.assertEqual(code, 2)
        self.assertIn("scripts/ask.py", err)

    def test_ask_module_without_run_ask_exits_2(self):
        bare = type("NoRunner", (), {})()
        with mock.patch.object(self.boss, "_load_ask_module",
                               return_value=bare):
            code, _, err = self._run_cli(["--ask", "status"])
        self.assertEqual(code, 2)
        self.assertIn("run_ask", err)

    def test_runner_exception_exits_2_with_clean_message(self):
        fake = FakeAsk(raises=ValueError("boom"))
        code, _, err, _ = self._ask(["--ask", "status"], fake)
        self.assertEqual(code, 2)
        self.assertIn("--ask status 执行失败", err)
        self.assertIn("boom", err)
        self.assertNotIn("Traceback", err)

    def test_non_dict_result_exits_2(self):
        fake = FakeAsk(result=["not", "a", "dict"])
        code, _, err, _ = self._ask(["--ask", "status", "--json"], fake)
        self.assertEqual(code, 2)
        self.assertIn("格式非法", err)

    def test_json_without_readonly_command_exits_2(self):
        code, _, err = self._run_cli(["--json"])
        self.assertEqual(code, 2)
        self.assertIn("--json 需要配合", err)

    # ---- 只读语义 ----

    def test_readonly_commands_do_not_use_outer_run_lock(self):
        parser = self.boss.build_parser()
        for argv in (["--ask", "status"], ["--batches"], ["--batches", "3"]):
            args = parser.parse_args(argv)
            self.assertFalse(self.boss._cli_uses_outer_run_lock(args), argv)

    def test_help_documents_ask_and_batches(self):
        text = " ".join(self.boss.build_parser().format_help().split())
        self.assertIn("--ask", text)
        self.assertIn("--batches", text)
        self.assertIn("status / today / templates / diff / why", text)

    def test_ask_does_not_create_run_lock_file(self):
        fake = FakeAsk()
        self._ask(["--ask", "status", "--json"], fake)
        lock_path = os.path.join(self.dir, "state", "scrape.lock")
        self.assertFalse(os.path.exists(lock_path))


if __name__ == "__main__":
    unittest.main()
