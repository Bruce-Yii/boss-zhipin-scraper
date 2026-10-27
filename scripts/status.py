"""状态与诊断命令（从 boss_cdp_raw.py 增量拆分）。"""

import glob
import logging

log = logging.getLogger("boss_cdp")

def _core():
    import sys as _sys
    # 1) 调用栈优先。
    try:
        _frame = _sys._getframe(1)
    except ValueError:
        _frame = None
    _depth = 0
    while _frame is not None and _depth < 64:
        _depth += 1
        try:
            _g = _frame.f_globals
            _fpath = _g.get("__file__", "") if isinstance(_g, dict) else ""
        except Exception:
            _fpath = ""
        if _fpath and str(_fpath).endswith("boss_cdp_raw.py"):
            for _m in list(_sys.modules.values()):
                try:
                    if getattr(_m, "__dict__", None) is _g:
                        return _m
                except Exception:
                    continue
            break
        try:
            _frame = _frame.f_back
        except Exception:
            break
    _candidates = [m for m in _sys.modules.values()
                   if getattr(m, "__file__", None)
                   and str(m.__file__).endswith("boss_cdp_raw.py")]
    if _candidates:
    # 2) 直接调用取最近 exec 副本。
        _best = _candidates[-1]
        try:
            _best_seq = getattr(_best, "_BOSS_EXEC_SEQ", -1)
            for _m in _candidates[:-1]:
                _seq = getattr(_m, "_BOSS_EXEC_SEQ", -1)
                if _seq is not None and _best_seq is not None and _seq > _best_seq:
                    _best, _best_seq = _m, _seq
        except Exception:
            pass
        return _best
    try:
        from . import boss_cdp_raw as _m
    # 3) 兜底按路径加载。
        return _m
    except Exception:
        if "boss_cdp_raw" in _sys.modules:
            return _sys.modules["boss_cdp_raw"]
        import boss_cdp_raw as _m
        return _m
def run_status(cdp_port=_core().DEFAULT_CDP_PORT, result_dir=_core().DEFAULT_RESULT_DIR):
    """一条命令看清"运行态 + 缓存态"（换窗口接管用；**不发 BOSS 请求**）。

    - 运行态：CDP 端口、Chrome profile、互斥锁、熔断冷却/恢复期
    - 缓存态：结果目录文件统计 + 最新列表文件 meta 摘要（含 jd 覆盖率）
    - 技能/入口：指向项目与工作区文档（不复制内容）
    """
    print("=" * 54)
    print("  BOSS直聘 爬虫状态总览（接管用）")
    print("=" * 54)

    # ── 运行态 ──
    print("\n[运行态]")
    _core().require_runtime_dependencies("requests")
    if _core().requests is None:
        print("  CDP 端口        : ⚠️ 缺少 requests，无法探测")
    else:
        try:
            resp = _core().requests.get(f"http://127.0.0.1:{cdp_port}/json/version", timeout=5)
            print(f"  CDP 端口        : ✅ {cdp_port} 可连（{resp.json().get('Browser', '未知')}）")
        except (_core().requests.ConnectionError, _core().requests.Timeout):
            print(f"  CDP 端口        : ❌ {cdp_port} 不可连（可跑 --setup-chrome 启动）")
        except (_core().json.JSONDecodeError, KeyError) as e:
            print(f"  CDP 端口        : ⚠️ 响应异常: {e}")
    try:
        profile = _core().prepare_cdp_profile(copy_login_state=False, reset=False)
        print(f"  Chrome profile  : ✅ {_core().os.path.normpath(profile['path'])}")
    except Exception as e:  # 有意宽捕：状态命令不因探测失败而崩
        print(f"  Chrome profile  : ⚠️ 探测失败: {e}")
    max_conc, holders, risk = _core()._read_scrape_lock()
    if risk:
        print(f"  互斥锁          : ⛔ 熔断挂起（上限 {max_conc}，持有 {len(holders)}）"
              "——人工处理后 --reset-lock")
    elif holders:
        print(f"  互斥锁          : 🔒 被持有（上限 {max_conc}，"
              f"持有 {len(holders)}: {', '.join(holders[:5])}）")
    else:
        print(f"  互斥锁          : 🔓 空闲（上限 {max_conc}）")
    cooldown = _core().check_cdp_cooldown()
    recovery = _core().check_cdp_recovery()
    if cooldown is not None:
        tail = f"（后接恢复期 {recovery:.0f}s）" if recovery > 0 else ""
        print(f"  熔断冷却        : ⏳ 剩余约 {cooldown}s{tail}")
    elif recovery > 0:
        print(f"  熔断恢复期      : ⏳ 剩余约 {recovery:.0f}s（限速减半）")
    else:
        print("  熔断冷却        : 无")
    # 详情页日预算（跨 run 累计的详情页请求次数）
    try:
        used_today = _core()._daily_budget.read_state()["detail"]
        if _core().DAILY_DETAIL_BUDGET:
            print(f"  详情页日预算    : 今日已用 {used_today}/{_core().DAILY_DETAIL_BUDGET}"
                  f"（剩余 {max(_core().DAILY_DETAIL_BUDGET - used_today, 0)} 次请求）")
        else:
            print(f"  详情页日预算    : 今日已用 {used_today}（未设上限）")
    except Exception:
        print("  详情页日预算    : 读取失败（不影响抓取）")
    print("  登录态          : 未探测（跑 --check 探测；会发一次请求）")

    # ── 缓存态 ──
    print(f"\n[缓存态] {result_dir}")
    jobs = sorted(glob.glob(_core().os.path.join(result_dir, "boss_jobs_*.json")))
    details = sorted(
        path for path in glob.glob(_core().os.path.join(result_dir, "boss_details_*.json"))
        if not _core()._is_detail_sidecar(_core().os.path.basename(path))
    )
    pending = sorted(glob.glob(_core().os.path.join(result_dir, "*.pending.json")))
    archive = glob.glob(_core().os.path.join(result_dir, "archive", "*.json"))
    if jobs:
        latest = jobs[-1]
        print(f"  列表文件        : {len(jobs)} 个｜最新 {_core().os.path.basename(latest)}")
        try:
            with open(latest, encoding="utf-8") as f:
                m = _core().json.load(f)
            cov = m.get("jd_coverage") or {}
            extra = (f" jd={cov.get('with_jd')}/{cov.get('total_before')}" if cov else "")
            print(f"    └ keyword={m.get('keyword')} city={m.get('city')} "
                  f"jobs={m.get('job_count')}{extra}")
        except (OSError, _core().json.JSONDecodeError, ValueError):
            print("    └ （meta 读取失败）")
    else:
        print("  列表文件        : 0 个")
    print(f"  详情文件        : {len(details)} 个"
          + (f"｜最新 {_core().os.path.basename(details[-1])}" if details else ""))
    print(f"  pending 文件    : {len(pending)} 个")
    print(f"  归档文件        : {len(archive)} 个（archive/）")
    try:
        total = sum(_core().os.path.getsize(p) for p in jobs + details + pending)
        print(f"  本目录占用      : {total / 1024 / 1024:.1f} MB")
    except OSError:
        pass

    # ── 技能 / 入口 ──
    print("\n[技能 / 入口]（文档为准，不在此复制）")
    print(f"  项目            : {_core().os.path.dirname(_core().os.path.dirname(_core().os.path.abspath(__file__)))}")
    print("    说明: SKILL.md · AGENTS.md · README.md")
    print("  工作区文档      : docs/projects/boss-zhipin-scraper/"
          "（架构与链路地图 / 待办总表 / 任务记录）")
    sha = _core()._git_short_sha()
    if sha:
        print(f"  版本            : HEAD {sha}")
    print()
    return 0


def probe_cdp_port(port, timeout=2):
    """探测某端口是否有 CDP 服务（/json/version 可达且含 webSocketDebuggerUrl）。"""
    try:
        import requests as _req_probe
    except ImportError:
        return False
    try:
        resp = _req_probe.get(f"http://127.0.0.1:{port}/json/version", timeout=timeout)
        return bool(resp.json().get("webSocketDebuggerUrl"))
    except (_req_probe.RequestException, ValueError, KeyError):
        return False


def detect_cdp_port(preferred=_core().DEFAULT_CDP_PORT, candidates=_core().CDP_CANDIDATE_PORTS, probe=None):
    """返回第一个可达的 CDP 端口（preferred 优先，其后候选）；都不可达返回 None。

    专题 §3.2（Snseam 三级端口探测）：用户可能用标准端口（9222/9229/19222）起了 Chrome。
    """
    probe = probe or probe_cdp_port
    for port in (preferred, *candidates):
        if probe(port):
            return port
    return None


def run_check(cdp_port=_core().DEFAULT_CDP_PORT):
    """运行环境诊断检查"""
    print("=" * 50)
    print("  BOSS直聘 CDP 环境检查")
    print("=" * 50)
    print()

    all_pass = True

    # 检查 1: Python 依赖
    print("[1/3] Python 依赖...")
    deps_ok = _core().require_runtime_dependencies("websocket", "requests")
    if _core().requests is not None:
        print("  ✅ requests 可导入")
    if _core().websocket is not None:
        print("  ✅ websocket-client 可导入")
    if deps_ok:
        print("  ✅ 依赖完整")
    else:
        all_pass = False

    # 检查 2: CDP 端口连通性
    print("[2/3] CDP 端口连通性...")
    if _core().requests is None:
        print("  ❌ 跳过 — 缺少 requests")
        all_pass = False
    else:
        try:
            resp = _core().requests.get(f"http://127.0.0.1:{cdp_port}/json/version", timeout=5)
            data = resp.json()
            browser = data.get("Browser", "未知")
            print(f"  ✅ 通过 — CDP 服务: {browser}")
        except (_core().requests.ConnectionError, _core().requests.Timeout):
            print(f"  ❌ 失败 — 无法连接 127.0.0.1:{cdp_port}")
            found = _core().detect_cdp_port(cdp_port, _core().CDP_CANDIDATE_PORTS)
            if found and found != cdp_port:
                print(f"     发现其他端口有 CDP 服务: {found}（可用 --cdp-port {found}）")
            else:
                print(f"     请先启动 Chrome CDP: {_core().sys.executable} {__file__} --setup-chrome")
            all_pass = False
        except (_core().json.JSONDecodeError, KeyError) as e:
            print(f"  ❌ 失败 — CDP 响应异常: {e}")
            all_pass = False

    # 检查 3: BOSS直聘登录状态
    print("[3/3] BOSS直聘登录状态...")
    if not deps_ok:
        print("  ❌ 跳过 — 缺少运行依赖")
        all_pass = False
    else:
        try:
            login_result = _core().check_login_state(cdp_port)
            if login_result.status is _core().LoginProbeStatus.AVAILABLE:
                print("  ✅ 已登录")
            elif login_result.status is _core().LoginProbeStatus.EMPTY:
                print(f"  ⚠️  {_core().describe_login_probe_result(login_result)}")
                all_pass = False
            else:
                print(f"  ❌ {_core().describe_login_probe_result(login_result)}")
                all_pass = False
        except Exception as e:  # 有意宽捕：--check 是诊断命令，任何异常都应报错而非崩溃
            print(f"  ❌ 检测失败: {e}")
            all_pass = False

    print()
    if all_pass:
        print("✅ 所有检查通过，可以开始抓取")
    else:
        print("❌ 部分检查未通过，请修复后重试")
    print()

    return 0 if all_pass else 1

