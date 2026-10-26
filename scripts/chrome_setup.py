# -*- coding: utf-8 -*-
"""Chrome CDP 环境准备与进程管理（从 boss_cdp_raw.py 增量拆分）。"""

import os
import json
import time
import signal
import shutil
import platform
import ntpath
import subprocess
import logging
import re

log = logging.getLogger("boss_cdp")

def _core():
    import sys as _sys
    candidates = [m for m in _sys.modules.values()
                 if getattr(m, "__file__", None)
                 and str(m.__file__).endswith("boss_cdp_raw.py")]
    if candidates:
        return candidates[-1]
    try:
        from . import boss_cdp_raw as _m
        return _m
    except Exception:
        if "boss_cdp_raw" in _sys.modules:
            return _sys.modules["boss_cdp_raw"]
        import boss_cdp_raw as _m
        return _m
def prepare_cdp_profile(copy_login_state=False, reset=False):
    """Prepare an isolated persistent Chrome profile for CDP."""
    cdp_data_dir = _core().DEFAULT_CDP_DATA_DIR
    cdp_default = os.path.join(cdp_data_dir, "Default")

    if reset and os.path.exists(cdp_data_dir):
        shutil.rmtree(cdp_data_dir)

    os.makedirs(cdp_default, exist_ok=True)

    copied = 0
    if copy_login_state:
        default_profile = _core().DEFAULT_PROFILE_DIR
        default_default = os.path.join(default_profile, "Default")
        cookie_files = []
        for rel_dir in ("", "Network"):
            for name in ("Cookies", "Cookies-journal", "Cookies-wal", "Cookies-shm"):
                rel_path = os.path.join(rel_dir, name) if rel_dir else name
                cookie_files.append((os.path.join(default_default, rel_path), os.path.join(cdp_default, rel_path)))

        copy_files = [(os.path.join(default_profile, "Local State"), os.path.join(cdp_data_dir, "Local State"))]
        copy_files.extend(cookie_files)
        for src, dst in copy_files:
            if os.path.exists(src):
                try:
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copy2(src, dst)
                    copied += 1
                except Exception as e:  # 有意宽捕：单个 cookie 文件复制失败不应中断其余文件
                    print(f"  ⚠️  复制 {os.path.basename(src)} 失败: {e}")

    return {
        "path": cdp_data_dir,
        "copied": copied,
        "reset": reset,
        "copy_login_state": copy_login_state,
    }


def is_cdp_ready(cdp_port):
    try:
        resp = _core().requests.get(f"http://127.0.0.1:{cdp_port}/json/version", timeout=2)
        return resp.status_code == 200
    except (OSError, TimeoutError):
        # requests 的 ConnectionError/Timeout 都是 OSError 子类；只吞网络层错误，
        # 其余意外异常（如 ValueError）照常抛出便于排查
        return False


def is_chrome_command(command):
    lower = (command or "").lower()
    return any(token in lower for token in (
        "google chrome",
        "google-chrome",
        "chromium",
        "chrome.exe",
    ))


def normalize_profile_path(path):
    clean = (path or "").strip("\"'")
    if platform.system() == "Windows":
        return ntpath.normcase(ntpath.normpath(clean))
    return os.path.realpath(os.path.expanduser(clean))


def extract_user_data_dir(command):
    match = re.search(r"--user-data-dir=(\"[^\"]+\"|'[^']+'|\S+)", command or "")
    if not match:
        return None
    return match.group(1).strip("\"'")


def iter_chrome_process_commands():
    """Return (pid, command line) tuples for Chrome-like browser processes."""
    if platform.system() == "Windows":
        ps_script = (
            "Get-CimInstance Win32_Process -Filter \"name = 'chrome.exe'\" | "
            "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
        )
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_script],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            # 进程枚举失败不阻塞主流程：调用方按"无进程"处理
            return []
        if not r.stdout.strip():
            return []
        try:
            data = json.loads(r.stdout)
        except (json.JSONDecodeError, ValueError):
            return []
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            return []
        processes = []
        for item in data:
            command = item.get("CommandLine") or ""
            if not _core().is_chrome_command(command):
                continue
            try:
                processes.append((int(item.get("ProcessId")), command))
            except (TypeError, ValueError):
                continue
        return processes

    try:
        r = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        # 进程枚举失败不阻塞主流程：调用方按"无进程"处理
        return []

    processes = []
    for line in r.stdout.splitlines():
        if not _core().is_chrome_command(line):
            continue
        try:
            pid_text, command = line.strip().split(None, 1)
            pid = int(pid_text)
        except ValueError:
            continue
        processes.append((pid, command))
    return processes


def chrome_pids_for_user_data_dir(user_data_dir):
    """Return Chrome PIDs using the given user-data-dir."""
    pids = []
    real_dir = _core().normalize_profile_path(user_data_dir)
    for pid, command in _core().iter_chrome_process_commands():
        if "--user-data-dir=" not in command:
            continue
        path = _core().extract_user_data_dir(command)
        if path and _core().normalize_profile_path(path) == real_dir:
            pids.append(pid)
    return pids


def chrome_user_data_dirs_for_cdp_port(cdp_port):
    """Return user-data-dir paths for Chrome processes using the given CDP port."""
    dirs = []
    port_arg = f"--remote-debugging-port={cdp_port}"
    for _pid, command in _core().iter_chrome_process_commands():
        if port_arg not in command:
            continue
        path = _core().extract_user_data_dir(command)
        if path:
            dirs.append(path)
    return dirs


def cdp_port_uses_profile(cdp_port, cdp_data_dir):
    expected = _core().normalize_profile_path(cdp_data_dir)
    return any(_core().normalize_profile_path(path) == expected for path in _core().chrome_user_data_dirs_for_cdp_port(cdp_port))


def terminate_process(pid, force=False):
    if platform.system() == "Windows":
        cmd = ["taskkill", "/PID", str(pid), "/T"]
        if force:
            cmd.append("/F")
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        return
    os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)


def stop_cdp_chrome(cdp_data_dir):
    """Stop only Chrome processes that use the scraper's isolated profile."""
    pids = _core().chrome_pids_for_user_data_dir(cdp_data_dir)
    if not pids:
        return 0

    for pid in pids:
        try:
            _core().terminate_process(pid, force=False)
        except ProcessLookupError:
            pass
    for _ in range(10):
        time.sleep(0.5)
        if not _core().chrome_pids_for_user_data_dir(cdp_data_dir):
            return len(pids)

    for pid in _core().chrome_pids_for_user_data_dir(cdp_data_dir):
        try:
            _core().terminate_process(pid, force=True)
        except ProcessLookupError:
            pass
    time.sleep(0.5)
    return len(pids)


def wait_for_cdp(cdp_port, timeout=30):
    print("等待 CDP 可用", end="")
    for _ in range(timeout):
        time.sleep(1)
        print(".", end="", flush=True)
        if _core().is_cdp_ready(cdp_port):
            print(f"\n✅ CDP 已就绪 (端口 {cdp_port})")
            return True
    print(f"\n❌ 等待超时 ({timeout}s)，CDP 未就绪")
    print(f"   请手动检查 Chrome 是否启动，端口 {cdp_port} 是否开放")
    return False


def launch_chrome(cmd):
    kwargs = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if platform.system() == "Windows":
        creationflags = 0
        creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        creationflags |= getattr(subprocess, "DETACHED_PROCESS", 0)
        if creationflags:
            kwargs["creationflags"] = creationflags
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(cmd, **kwargs)


def run_setup_chrome(cdp_port=_core().DEFAULT_CDP_PORT, copy_login_state=False,
                     reset_profile=False, wait_login=True,
                     login_timeout=_core().DEFAULT_LOGIN_TIMEOUT):
    """自动配置并启动 Chrome CDP 模式"""
    if not _core().require_runtime_dependencies("requests"):
        return 1

    print("=" * 50)
    print("  设置 Chrome CDP 调试模式")
    print("=" * 50)
    print()

    profile = _core().prepare_cdp_profile(copy_login_state=copy_login_state, reset=reset_profile)
    cdp_data_dir = profile["path"]
    print(f"✅ 使用独立 Chrome profile: {cdp_data_dir}")
    if reset_profile:
        print("   已按 --reset-chrome-profile 重建 profile")
    if copy_login_state:
        print(f"   已复制 {profile['copied']} 个登录态文件（Local State + Cookie 相关文件）")
    else:
        print("   默认、首次启动、重复启动都不复制主 Chrome Cookie；首次使用请在此专用 Chrome 中登录 zhipin.com")

    if _core().is_cdp_ready(cdp_port):
        if _core().cdp_port_uses_profile(cdp_port, cdp_data_dir):
            print(f"\n✅ CDP 已就绪 (端口 {cdp_port})")
            if wait_login:
                return 0 if _core().wait_for_login(cdp_port, timeout=login_timeout) else 1
            return 0
        print(f"\n❌ 端口 {cdp_port} 已被其他 Chrome CDP profile 占用")
        print("   请关闭旧 CDP Chrome，或改用 --cdp-port 指定其他端口")
        return 1

    stopped = _core().stop_cdp_chrome(cdp_data_dir)
    if stopped:
        print(f"\n已关闭 {stopped} 个旧的 BOSS CDP Chrome 进程")

    print(f"\n启动 Chrome (CDP 端口: {cdp_port})...")
    cmd = [
        _core().DEFAULT_CHROME_PATH,
        f"--remote-debugging-port={cdp_port}",
        f"--user-data-dir={cdp_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        f"--remote-allow-origins=http://localhost:{cdp_port},http://127.0.0.1:{cdp_port}",
    ]
    _core().launch_chrome(cmd)

    if not _core().wait_for_cdp(cdp_port):
        return 1

    print()
    print("Chrome 已启动。请在这个专用浏览器中登录 zhipin.com。")
    if wait_login:
        print()
        if not _core().wait_for_login(cdp_port, timeout=login_timeout):
            return 1
    print()
    print("示例:")
    print("  uv run python3 scripts/boss_cdp_raw.py --keyword \"AI Agent\" --city 上海 --pages 3")
    print("  uv run python3 scripts/boss_cdp_raw.py --check")
    print("  uv run python3 scripts/boss_cdp_raw.py --stop-chrome   # 抓完关闭专用 Chrome")
    print()
    return 0


def run_stop_chrome():
    """关闭 BOSS 专用 CDP Chrome（按隔离 user-data-dir 精准匹配，不碰主 Chrome）。"""
    if not _core().require_runtime_dependencies("requests"):
        return 1

    print("=" * 50)
    print("  关闭 BOSS 专用 CDP Chrome")
    print("=" * 50)
    print()

    # 只定位 scraper 专用 profile 目录，不复制、不重置
    profile = _core().prepare_cdp_profile(copy_login_state=False, reset=False)
    cdp_data_dir = profile["path"]

    stopped = _core().stop_cdp_chrome(cdp_data_dir)
    if stopped:
        print(f"\n✅ 已关闭 {stopped} 个 BOSS 专用 Chrome 进程 (profile: {cdp_data_dir})")
    else:
        print(f"\nℹ️  没有找到运行中的 BOSS 专用 Chrome 进程 (profile: {cdp_data_dir})")
    print()
    print("提示：仅关闭 scraper 隔离 profile 的 Chrome，不影响你的主 Chrome。")
    print()
    return 0



