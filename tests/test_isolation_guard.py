"""测试隔离守卫：断言单测运行不会污染真实结果目录。

背景（2026-08-12 事故）：test_scrape_list_* 未 mock flush_jobs，
scrape_list(output_path=None) 落到真实 ~/.boss-zhipin-scraper/job-result，
每次跑单测都写 mock 假文件（47 个，~2800 条，污染规格侧消费目录）。
修复后本守卫把"手工验证 60→60"固化为自动测试，防复发。
"""

import pathlib
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
REAL_RESULT_DIR = pathlib.Path.home() / ".boss-zhipin-scraper" / "job-result"


class TestIsolationGuardTests(unittest.TestCase):

    def test_unit_tests_do_not_pollute_real_result_dir(self):
        """跑完全套单测后，真实结果目录的 boss_jobs_*.json 文件数不变。"""
        before = set(REAL_RESULT_DIR.glob("boss_jobs_*.json")) \
            if REAL_RESULT_DIR.exists() else set()

        result = subprocess.run(
            [sys.executable, "-m", "unittest",
             "tests.test_chrome_setup", "tests.test_job_summary"],
            cwd=str(ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=300,
        )
        self.assertEqual(result.returncode, 0,
                         f"单测应全绿:\n{result.stdout[-500:]}\n{result.stderr[-500:]}")

        after = set(REAL_RESULT_DIR.glob("boss_jobs_*.json")) \
            if REAL_RESULT_DIR.exists() else set()
        new_files = after - before
        self.assertEqual(len(new_files), 0,
                         f"单测不应向真实结果目录写文件，新增: {[f.name for f in new_files]}")


if __name__ == "__main__":
    unittest.main()
