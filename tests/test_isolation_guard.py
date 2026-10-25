"""全套单测的真实状态隔离守卫。

历史事故：
- 2026-08-12：未 mock ``flush_jobs`` 的列表测试向真实 job-result 写入 47 个假文件。
- 2026-09-23：未 mock ``send_alert`` 的登录失败测试写真实 risk_events.jsonl。
- 2026-09-24 接管审计：catch-all 测试可能关闭真实 CDP tab；``main()`` 测试可能删除真实 sidecar/tmp。
- 2026-10-25：\`--db\` 默认启用后，4 个 CLI 测试把夹具数据写进真实 boss.db，
  守卫的中段快照完全没覆盖（污染发生在守卫测试之前 + 之后的写入无人复核）。

本守卫只在外层运行一次。子进程设置 ``BOSS_ISOLATION_GUARD_CHILD=1`` 跳过本类，
再用临时 HOME/USERPROFILE 跑完整 discovery，避免递归和真实用户状态污染。
"""

import atexit
import hashlib
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
REAL_STATE_ROOT = pathlib.Path.home() / ".boss-zhipin-scraper"
REAL_DB_PATH = REAL_STATE_ROOT / "boss.db"
_IS_CHILD = os.environ.get("BOSS_ISOLATION_GUARD_CHILD") == "1"
# 真实 Chrome 的 CDP 端口（仅用于「Chrome 是否在跑」的存活探测，不发 CDP 命令）
_REAL_CDP_PORT = 45222


def _file_digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _real_db_digest():
    """真实 boss.db 的内容摘要；不存在返回 None。"""
    if not REAL_DB_PATH.exists():
        return None
    return _file_digest(REAL_DB_PATH)


# discovery 先**收集**（导入）全部测试模块再执行 → 导入时刻先于任何测试体，
# 在这里拍 boss.db 快照，才能覆盖"守卫测试之前"的外层测试污染（#177 事故）。
_DB_DIGEST_AT_IMPORT = None if _IS_CHILD else _real_db_digest()


def _scrape_run_lock_held():
    """真实 run 锁是否被持有（并发真实抓取 → boss.db 变化属正常，跳过核对）。"""
    lock = REAL_STATE_ROOT / "scrape.lock"
    try:
        return lock.exists()
    except OSError:
        return False


def _verify_real_db_at_exit():
    """进程退出兜底：boss.db 相对导入时刻变了 → stderr 大声告警。

    不改退出码：并发真实抓取也会改库（守卫宁可漏报也不误杀套件）；
    硬失败由 test_full_suite 里的核对承担（持锁时跳过）。
    """
    if _IS_CHILD or _DB_DIGEST_AT_IMPORT is None:
        return
    now = _real_db_digest()
    if now != _DB_DIGEST_AT_IMPORT:
        print("\n❌ 隔离守卫：真实 boss.db 在本次测试进程期间被写入——"
              "检查未加 --no-db 的 CLI 测试（详见 tests/test_isolation_guard.py）",
              file=sys.stderr)
        sys.stderr.flush()


if not _IS_CHILD:
    atexit.register(_verify_real_db_at_exit)


def _chrome_profile_in_use(port=_REAL_CDP_PORT):
    """真实 Chrome 是否正在使用该 profile（TCP 存活探测，不发任何 CDP 命令）。

    Chrome 活着时它会持续自变更 profile（缓存、会话文件、Local State），
    聚合快照（文件数/总大小/最新 mtime）**不是稳定信号**——单是正常浏览就会让
    守卫误报"单测改了真实状态"。这种场景下守卫改为只比对非 profile 状态
    （scraper 自己的 job-result / 审计日志 / sidecar 等），profile 项跳过。
    """
    profile = pathlib.Path(REAL_STATE_ROOT) / "chrome-profile"
    if not profile.exists():
        return False
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


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
        chrome_live = _chrome_profile_in_use()
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
        after = snapshot_protected_state()
        if chrome_live or _chrome_profile_in_use():
            # 活 Chrome 会自变更 profile（缓存/会话/Local State）——
            # 聚合快照不是稳定信号，这种场景只比对非 profile 状态
            before.pop("chrome_profile", None)
            after.pop("chrome_profile", None)
        self.assertEqual(after, before,
                         "全套单测不得新增、删除或修改真实 scraper 状态")
        # boss.db 专项（#177 事故）：核对**导入时刻 → 守卫测试运行时**的窗口
        # （中段快照的 before 里包含之前的污染，上面那条看不见）。
        # 持真实 run 锁 = 并发真实抓取在写库 → 跳过，避免误报。
        if _DB_DIGEST_AT_IMPORT is not None and not _scrape_run_lock_held():
            self.assertEqual(
                _real_db_digest(), _DB_DIGEST_AT_IMPORT,
                "真实 boss.db 在本套件前段被测试写入——"
                "检查未加 --no-db 的 CLI 测试（main()/run_cli() 都要查）")


if __name__ == "__main__":
    unittest.main()
