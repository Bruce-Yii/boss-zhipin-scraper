"""日常作业：一条命令跑完「列表 + 详情 → 从库重建交付物 → 今日简报」。

为什么要有它
------------
项目在 agent 会话里运行，用户下指令后 agent 要跑一串命令（检查→列表→详情→
出交付物→汇报）。把「每天固定的跑法」固化成一个**带注释的 JSON 配置**，
以后一句 ``--daily`` 就能复现，不用每次从对话里重新拼参数。

配置（默认 ``~/.boss-zhipin-scraper/daily.json``）示例见
``data/daily_profile.example.json``；JSON 没有行注释，说明用 ``_comment`` 字段。

流水线：
1. 子进程跑抓取（列表 + 详情；进度写 run_state，agent 可用 ``--progress`` 轮询）；
2. 子进程从 SQLite 原材料库重建该口径的交付物（走生产合并路径，含验收/CSV）；
3. 刷新产物清单 + 打印今日简报（``--json`` 输出最终结构化结果）。

失败语义：任一步非 0 → 停并返回非 0；子进程输出落 ``daily_last_run.log``。
"""

import json
import os
import subprocess
import sys
import time

try:
    from scripts import artifacts as _artifacts
    from scripts import brief as _brief
    from scripts import db_store as _db_store
    from scripts import run_state as _run_state
except ImportError:  # pragma: no cover - 直接运行脚本时走此分支
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import artifacts as _artifacts
    import brief as _brief
    import db_store as _db_store
    import run_state as _run_state

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRAPE_SCRIPT = os.path.join(REPO_ROOT, "scripts", "boss_cdp_raw.py")
DELIVERABLE_SCRIPT = os.path.join(REPO_ROOT, "scripts", "build_deliverable.py")
PROFILES_DIR_ENV = "BOSS_DAILY_PROFILES_DIR"
DEFAULT_PROFILES_DIR = os.path.expanduser("~/.boss-zhipin-scraper/profiles")
LEGACY_CONFIG_PATH = os.path.expanduser("~/.boss-zhipin-scraper/daily.json")
DEFAULT_LOG_PATH = os.path.expanduser("~/.boss-zhipin-scraper/daily_last_run.log")
LOG_PATH_ENV = "BOSS_DAILY_LOG_PATH"
FILTER_KEYS = ("salary", "experience", "degree", "industry", "scale", "stage")
RECRUIT_CHOICES = ("intern", "campus", "social")
SOURCE_CHOICES = ("db", "artifacts")


def profiles_dir():
    return os.path.abspath(os.path.expanduser(
        os.environ.get(PROFILES_DIR_ENV) or DEFAULT_PROFILES_DIR))


def _profile_paths():
    """模板文件列表：profiles/*.json；目录为空时兼容旧的单文件 daily.json。"""
    directory = profiles_dir()
    paths = []
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        names = []
    for name in names:
        if name.endswith(".json"):
            paths.append(os.path.join(directory, name))
    if not paths and os.path.isfile(LEGACY_CONFIG_PATH):
        paths.append(LEGACY_CONFIG_PATH)
    return paths


def _read_raw(path):
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError as exc:
        raise ValueError(f"日常作业模板不存在：{path}"
                         "（参考 data/daily_profile.example.json）") from exc
    except (OSError, ValueError) as exc:
        raise ValueError(f"日常作业模板读取失败：{path}：{exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"日常作业模板必须是 JSON 对象：{path}")
    return raw


def list_profiles():
    """列出全部日常作业模板（不含校验失败项，坏模板带 error 字段）。"""
    result = []
    for path in _profile_paths():
        item = {"path": path, "file": os.path.basename(path)}
        try:
            raw = _read_raw(path)
        except ValueError as exc:
            item.update({"name": item["file"], "error": str(exc)})
            result.append(item)
            continue
        item.update({
            "name": str(raw.get("name") or os.path.splitext(item["file"])[0]),
            "city": str(raw.get("city") or ""),
            "keywords": len(raw.get("keywords") or []),
            "recruit_types": raw.get("recruit_types") or [],
            "source": str(raw.get("source") or "db"),
            "deliverable": str(raw.get("deliverable") or ""),
            "comment": str(raw.get("comment") or ""),
        })
        result.append(item)
    return result


def resolve_config(config_ref=None):
    """模板引用 → 文件路径：路径直接认；名字按 精确→包含 匹配；空 → None（列表）。

    多个模板同时存在时**不默认选一个**——必须明确说用哪套（用户口径）。
    """
    if config_ref:
        candidate = os.path.abspath(os.path.expanduser(str(config_ref)))
        if os.path.isfile(candidate):
            return candidate
        paths = _profile_paths()
        exact, fuzzy = [], []
        wanted = str(config_ref).strip().lower()
        for path in paths:
            try:
                raw = _read_raw(path)
            except ValueError:
                continue
            name = str(raw.get("name") or os.path.splitext(
                os.path.basename(path))[0])
            stem = os.path.splitext(os.path.basename(path))[0]
            if wanted in (name.lower(), stem.lower()):
                exact.append(path)
            elif wanted in name.lower() or wanted in stem.lower():
                fuzzy.append(path)
        matches = exact or fuzzy
        if len(matches) == 1:
            return matches[0]
        names = [os.path.splitext(os.path.basename(p))[0] for p in paths]
        if matches:
            raise ValueError(f"模板名有歧义：{config_ref!r} → "
                             f"{[os.path.basename(p) for p in matches]}")
        raise ValueError(f"没有找到日常作业模板 {config_ref!r}；"
                         f"现有：{names or '（无，目录 ' + profiles_dir() + '）'}")
    # 不带模板名：只列模板，**绝不默认挑一套跑**（用户口径：用哪套必须明说）
    return None


def config_path(ref=None):
    path = resolve_config(ref)
    if path is None:
        raise ValueError(f"请指定日常作业模板名（模板目录 {profiles_dir()}）；"
                         "不带参数只列模板")
    return path


def load_config(ref=None):
    """读并校验日常作业模板；非法项抛 ValueError（不静默用错口径）。"""
    path = config_path(ref)
    raw = _read_raw(path)
    cfg = {
        "name": str(raw.get("name") or "日常作业").strip(),
        "city": str(raw.get("city") or "").strip(),
        "keywords": [str(item).strip() for item in (raw.get("keywords") or [])
                     if str(item).strip()],
        "pages": int(raw.get("pages") or 3),
        "recruit_types": raw.get("recruit_types") or [],
        "detail": bool(raw.get("detail", True)),
        "max_details": raw.get("max_details"),
        "max_jobs": raw.get("max_jobs"),
        "filters": raw.get("filters") or {},
        "scope": str(raw.get("scope") or "").strip(),
        "scope_key": str(raw.get("scope_key") or "").strip(),
        "deliverable": str(raw.get("deliverable") or "").strip(),
        "db": str(raw.get("db") or "").strip(),
        "source": str(raw.get("source") or "db").strip(),
        "exclude_job_ids_file": str(raw.get("exclude_job_ids_file") or "").strip(),
        "comment": str(raw.get("comment") or "").strip(),
        "path": path,
    }
    if not cfg["city"]:
        raise ValueError("日常作业模板缺少 city")
    if not cfg["keywords"]:
        raise ValueError("日常作业模板缺少 keywords（至少一个关键词）")
    if not 1 <= cfg["pages"] <= 10:
        raise ValueError(f"pages 必须在 1-10：{cfg['pages']}")
    if isinstance(cfg["recruit_types"], str):
        cfg["recruit_types"] = [item.strip() for item in cfg["recruit_types"].split(",")
                                if item.strip()]
    unknown = [item for item in cfg["recruit_types"] if item not in RECRUIT_CHOICES]
    if unknown:
        raise ValueError(f"recruit_types 非法：{unknown}（可选 {','.join(RECRUIT_CHOICES)}）")
    if cfg["source"] not in SOURCE_CHOICES:
        raise ValueError(f"source 非法：{cfg['source']}（可选 {','.join(SOURCE_CHOICES)}）")
    if not cfg["deliverable"]:
        raise ValueError("日常作业模板缺少 deliverable（交付物文件名）")
    return cfg


def format_profiles(profiles):
    if not profiles:
        return (f"还没有日常作业模板。把模板 JSON 放到 {profiles_dir()}/ 下，"
                "示例见 data/daily_profile.example.json")
    lines = [f"\n=== 日常作业模板（{profiles_dir()}）==="]
    for item in profiles:
        if item.get("error"):
            lines.append(f"  ✗ {item.get('file')}：{item['error']}")
            continue
        lines.append(f"  · {item['name']}")
        lines.append(f"      {item.get('city', '')}｜{item.get('keywords', 0)} 个关键词"
                     f"｜{','.join(item.get('recruit_types') or []) or '全部类型'}"
                     f"｜source={item.get('source', 'db')}")
        if item.get("deliverable"):
            lines.append(f"      交付物：{item['deliverable']}")
        if item.get("comment"):
            lines.append(f"      说明：{item['comment']}")
    lines.append("\n用法：--daily <模板名或路径>（不带参数只列模板，不跑）")
    lines.append("")
    return "\n".join(lines)


def build_scrape_argv(cfg, python=None):
    """日常作业 → 抓取子进程 argv（列表+详情；口径在列表阶段就过滤）。"""
    python = python or sys.executable
    argv = [python, SCRAPE_SCRIPT,
            "--keyword", ",".join(cfg["keywords"]),
            "--city", cfg["city"],
            "--pages", str(cfg["pages"])]
    if cfg["recruit_types"]:
        argv += ["--recruit-type", ",".join(cfg["recruit_types"])]
    if not cfg["detail"]:
        argv.append("--no-detail")
    if cfg.get("max_details"):
        argv += ["--max-details", str(cfg["max_details"])]
    if cfg.get("max_jobs"):
        argv += ["--max-jobs", str(cfg["max_jobs"])]
    for key in FILTER_KEYS:
        value = (cfg.get("filters") or {}).get(key)
        if value:
            argv += [f"--{key}", str(value)]
    return argv


def build_deliverable_argv(cfg, python=None, result_dir=None, db_path=None):
    """日常作业 → 交付物构建 argv（库/产物并集两种来源，走生产合并路径）。"""
    python = python or sys.executable
    argv = [python, DELIVERABLE_SCRIPT]
    if cfg.get("source") == "artifacts":
        argv.append("--from-artifacts")
    else:
        argv += ["--from-db", db_path or cfg.get("db") or _db_store.DEFAULT_DB_PATH]
    argv += ["--city", cfg["city"]]
    if cfg["recruit_types"]:
        argv += ["--recruit-type", ",".join(cfg["recruit_types"])]
    if result_dir:
        argv += ["--result-dir", result_dir]
    argv += ["--target", cfg["deliverable"]]
    if cfg.get("scope"):
        argv += ["--scope", cfg["scope"]]
    if cfg.get("scope_key"):
        argv += ["--scope-key", cfg["scope_key"]]
    if cfg.get("exclude_job_ids_file"):
        argv += ["--exclude-job-ids-file",
                 os.path.abspath(os.path.expanduser(cfg["exclude_job_ids_file"]))]
    return argv


def _log_path():
    return os.path.abspath(os.path.expanduser(
        os.environ.get(LOG_PATH_ENV) or DEFAULT_LOG_PATH))


def _write_log(text):
    path = _log_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text or "")
    except OSError:
        return None
    return path


def _run_step(argv, as_json, label):
    """跑一个子进程：非 JSON 模式继承 stdout（终端实时看）；JSON 模式捕获落日志。"""
    started = time.time()
    try:
        if as_json:
            completed = subprocess.run(argv, capture_output=True, text=True,
                                       encoding="utf-8", errors="replace")
            output = (completed.stdout or "") + (completed.stderr or "")
        else:
            completed = subprocess.run(argv)
            output = ""
    except OSError as exc:
        return {"label": label, "returncode": 127, "seconds": 0,
                "output": f"启动失败：{exc}"}, False, f"启动失败：{exc}"
    return ({
        "label": label,
        "returncode": int(completed.returncode),
        "seconds": int(time.time() - started),
        "output": output,
    }, completed.returncode == 0, output)


def run_daily(config_ref=None, result_dir=None, as_json=False):
    """``--daily`` 实现：跑完整流水线并输出今日简报。

    ``config_ref`` 为空时**只列模板不执行**（多套模板并存，用哪套必须明说）。
    """
    try:
        resolved = resolve_config(config_ref)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if resolved is None:
        profiles = list_profiles()
        if as_json:
            print(json.dumps({"profiles": profiles,
                              "profiles_dir": profiles_dir()},
                             ensure_ascii=False, indent=2))
        else:
            print(format_profiles(profiles))
        return 0
    try:
        cfg = load_config(resolved)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    result_dir = os.path.abspath(os.path.expanduser(
        result_dir or _brief.DEFAULT_RESULT_DIR))
    db_path = cfg.get("db") or _db_store.DEFAULT_DB_PATH
    _run_state.start(phase="daily",
                     message=f"日常作业：{cfg['name']}（{cfg['source']}）",
                     keyword=",".join(cfg["keywords"]), city=cfg["city"])
    started = time.time()
    steps = []

    scrape_argv = build_scrape_argv(cfg)
    _run_state.update(phase="scrape", message="抓取列表+详情")
    scrape, ok, output = _run_step(scrape_argv, as_json, "scrape")
    steps.append(scrape)
    log = _write_log(output) if as_json else None
    if not ok:
        _run_state.finish(phase="failed", message="抓取失败",
                          message_detail=f"returncode={scrape['returncode']}")
        if as_json:
            print(json.dumps({
                "ok": False,
                "stage": "scrape",
                "config": cfg,
                "steps": steps,
                "log": log,
            }, ensure_ascii=False, indent=2))
        else:
            print(f"❌ 抓取失败（rc={scrape['returncode']}）；日志：{log or '无'}",
                  file=sys.stderr)
            if output:
                print(output[-3000:], file=sys.stderr)
        return 1

    deliver_argv = build_deliverable_argv(cfg, result_dir=result_dir,
                                          db_path=db_path)
    _run_state.update(phase="deliverable", message="从库重建交付物")
    deliver, deliver_ok, deliver_output = _run_step(deliver_argv, as_json,
                                                    "deliverable")
    steps.append(deliver)
    if as_json:
        log = _write_log((output or "") + "\n" + (deliver_output or ""))
    if not deliver_ok:
        _run_state.finish(phase="failed", message="交付物构建失败")
        if as_json:
            print(json.dumps({"ok": False, "stage": "deliverable",
                              "config": cfg, "steps": steps, "log": log},
                             ensure_ascii=False, indent=2))
        else:
            print(f"❌ 交付物构建失败（rc={deliver['returncode']}）", file=sys.stderr)
        return 1

    _run_state.update(phase="deliverable", message="刷新产物清单 + 今日简报")
    _artifacts.refresh_index(result_dir)
    try:
        report = _brief.collect(result_dir=result_dir, db_path=db_path)
    except (OSError, TypeError, ValueError) as exc:
        report = {"error": str(exc)}

    total_seconds = int(time.time() - started)
    _run_state.finish(
        phase="done", deliverable=cfg["deliverable"],
        jobs_kept=next((item.get("jobs") for item in report.get("deliverables", [])
                        if item.get("path") == cfg["deliverable"]), None),
        message=f"日常作业完成（{total_seconds}s）")
    payload = {
        "ok": True,
        "config": cfg,
        "steps": steps,
        "deliverable": {
            "target": cfg["deliverable"],
            "path": os.path.join(result_dir, cfg["deliverable"]),
            "scope_key": cfg.get("scope_key", ""),
        },
        "log": log,
        "seconds": total_seconds,
        "brief": report,
    }
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"\n✅ 日常作业完成：{cfg['deliverable']}（{total_seconds}s）")
        print(_brief.format_brief(report) if "error" not in report
              else f"简报生成失败：{report['error']}")
    return 0


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(
        description="日常作业：列表+详情 → 重建交付物 → 今日简报"
                    "（不带模板名只列模板）")
    parser.add_argument("--config", default=None,
                        help="模板名或路径（如 杭州11词实习-日常）；"
                             f"模板目录 {profiles_dir()}")
    parser.add_argument("--result-dir", default=None)
    parser.add_argument("--json", action="store_true", help="输出最终 JSON")
    args = parser.parse_args(argv)
    return run_daily(args.config, result_dir=args.result_dir, as_json=args.json)


if __name__ == "__main__":
    sys.exit(main())
