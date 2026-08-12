"""CLI 契约测试：锁定 CLI 表面行为（--version/--help/退出码），防回归。

不依赖 Chrome/网络/结果目录；subprocess 直接跑脚本（argparse 层）。
"""

import pathlib
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "boss_cdp_raw.py"


class CliContractTests(unittest.TestCase):

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=60,
        )

    def test_version_output_and_exit_code(self):
        """--version：exit 0 + 输出格式 '<name> <semver>'（GNU 合规）。"""
        r = self._run("--version")
        self.assertEqual(r.returncode, 0)
        self.assertRegex(r.stdout.strip(), r"\S+ \d+\.\d+\.\d+")

    def test_help_lists_grouped_sections(self):
        """--help：exit 0 + 7 个分组标题齐全。"""
        r = self._run("--help")
        self.assertEqual(r.returncode, 0)
        for title in ("搜索参数", "筛选参数", "输出参数", "详情抓取",
                      "工具命令", "Chrome 管理", "通用参数"):
            self.assertIn(title, r.stdout, f"--help 缺少分组: {title}")

    def test_unknown_flag_exits_2(self):
        """未知参数 = CLI 误用 → exit 2（argparse 默认）。"""
        r = self._run("--definitely-not-a-flag")
        self.assertEqual(r.returncode, 2)
        self.assertIn("unrecognized arguments", r.stderr)

    def test_conflicting_action_flags_exit_2(self):
        """动作型命令互斥：--check + --verify 同时给 → exit 2。"""
        r = self._run("--check", "--verify")
        self.assertEqual(r.returncode, 2)

    def test_invalid_archive_value_exits_2(self):
        """--archive 非整数 → exit 2（CLI 误用，非运行期错误）。"""
        r = self._run("--archive", "abc")
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
