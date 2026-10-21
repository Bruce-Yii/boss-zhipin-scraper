"""全套单测的真实状态隔离守卫。

历史事故：
- 2026-08-12：未 mock ``flush_jobs`` 的列表测试向真实 job-result 写入 47 个假文件。
- 2026-09-23：未 mock ``send_alert`` 的登录失败测试写真实 risk_events.jsonl。
- 2026-09-24 接管审计：catch-all 测试可能关闭真实 CDP tab；``main()`` 测试可能删除真实 sidecar/tmp。

本守卫只在外层运行一次。子进程设置 ``BOSS_ISOLATION_GUARD_CHILD=1`` 跳过本类，
再用临时 HOME/USERPROFILE 跑完整 discovery，避免递归和真实用户状态污染。
"""

import hashlib
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
REAL_STATE_ROOT = pathlib.Path.home() / ".boss-zhipin-scraper"
_IS_CHILD = os.environ.get("BOSS_ISOLATION_GUARD_CHILD") == "1"


def _file_digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _profile_snapshot(root):
    """Chrome profile 可能很大；只记录可检测增删改的聚合元数据。"""
    profile = pathlib.Path(root) / "chrome-profile"
    if not profile.exists():
        return None
    count = 0
    total_size = 0
    newest_mtime = 0
    for path in profile.rglob("*"):
        if not path.is_file():
            continue
        stat = path.stat()
        count += 1
        total_size += stat.st_size
        newest_mtime = max(newest_mtime, stat.st_mtime_ns)
    return (count, total_size, newest_mtime)


def snapshot_protected_state(root=REAL_STATE_ROOT):
    """返回可比较的状态快照，检测新增、删除、内容修改和大 profile 变化。"""
    root = pathlib.Path(root)
    files = {}
    if root.exists():
        for path in root.rglob("*"):
            if not path.is_file() or "chrome-profile" in path.parts:
                continue
            stat = path.stat()
            files[str(path.relative_to(root))] = (
                stat.st_size,
                stat.st_mtime_ns,
                _file_digest(path),
            )
    return {"files": files, "chrome_profile": _profile_snapshot(root)}


@unittest.skipIf(_IS_CHILD, "隔离守卫只在外层测试进程运行")
class TestIsolationGuardTests(unittest.TestCase):

    def test_snapshot_protected_state_detects_all_mutation_kinds(self):
        """状态快照必须同时发现新增、删除和内容修改。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "job-result").mkdir()
            sample = root / "job-result" / "sample.json"
            sample.write_text("before", encoding="utf-8")
            before = snapshot_protected_state(root)
            (root / "job-result" / "new.json").write_text("new", encoding="utf-8")
            after_add = snapshot_protected_state(root)
            self.assertNotEqual(before, after_add)
            (root / "job-result" / "new.json").unlink()
            sample.write_text("after", encoding="utf-8")
            after_modify = snapshot_protected_state(root)
            self.assertNotEqual(after_add, after_modify)
            sample.unlink()
            self.assertNotEqual(after_modify, snapshot_protected_state(root))

    def test_full_suite_does_not_mutate_protected_state(self):
        """完整 discovery 必须在隔离 HOME 中运行且不触碰真实状态。"""
        before = snapshot_protected_state()
        with tempfile.TemporaryDirectory() as fake_home:
            fake = pathlib.Path(fake_home)
            env = os.environ.copy()
            env.update({
                "BOSS_ISOLATION_GUARD_CHILD": "1",
                "HOME": str(fake),
                "USERPROFILE": str(fake),
                "BOSS_AUDIT_PATH": str(fake / "audit" / "risk_events.jsonl"),
                "PYTHONDONTWRITEBYTECODE": "1",
            })
            result = subprocess.run(
                [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
                cwd=str(ROOT),
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=600,
            )
        self.assertEqual(
            result.returncode,
            0,
            f"全套单测应全绿:\n{result.stdout[-4000:]}\n{result.stderr[-4000:]}",
        )
        self.assertEqual(snapshot_protected_state(), before,
                         "全套单测不得新增、删除或修改真实 scraper 状态")


if __name__ == "__main__":
    unittest.main()
