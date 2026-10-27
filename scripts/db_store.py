#!/usr/bin/env python3
"""SQLite 增量存储层 + 资产层（schema v3）。

从 ``scripts/boss_cdp_raw.py`` 抽出的稳定纯逻辑（2026-09-22 P4c-2）：
仅用标准库 ``sqlite3``，无 CDP / 网络依赖，可独立测试；主文件对同名符号做
re-export，保持 ``scripts.boss_cdp_raw.X`` 导入面不变（同 ratelimit/export_contract）。

设计要点
--------
- **WAL 模式**：``journal_mode=WAL`` + ``synchronous=NORMAL`` + ``busy_timeout``，
  多进程读写不互相锁表（与 ``--max-concurrent`` / ``--batch`` 共存）。
- **增量 upsert**：``job_id`` 唯一键；``first_seen_at`` 首次写入后不再变，
  ``updated_at`` 每次覆盖 → 支撑跨 run 增量与后续趋势分析（P3）。
- **payload 存 JSON**：抓取字段随版本演进，无需跟随建列；只把关键查询列
  （``job_id`` / ``updated_at``）单独抽出并建索引。
- **合规**：入库前统一走 ``_sanitize_job``（cookie / token / securityId / BOSS
  内部标识一律不落库）；DB 默认放 ``~/.boss-zhipin-scraper/``（仓库外，不进 git）。

资产层（schema v3，2026-10-28 P0）
----------------------------------
v2 的库是「payload JSON 堆」：能存不能用——查一个城市/薪资/招聘类型都要把
JSON 反序列化再跑一遍 Python。v3 在**不推翻旧表**的前提下把可查询的东西落成列：

- ``jobs`` 增加规范化列（title/company/city/district/salary_min/salary_max/
  salary_desc/experience/education/recruit_type/industry/company_size/url/
  first_seen_at/last_seen_at/state/content_hash/canonical_id）：写入时从 payload
  抽取，旧行由迁移脚本回填；
- ``job_versions``：JD 版本历史（jd_hash 变化才追加，涨薪/改名可查）；
- ``job_flags``：从 payload 派生的标记（risk_flags / recruit:intern / post:agency…），
  口径/规则可版本化（``JOB_FLAG_RULE_VERSION``）；
- ``runs`` 增加 scope_key/template/ruleset_version/list_pages/detail_ok/
  detail_failed/budget_used/artifact_paths（批次健康度与口径血缘）；
- 迁移是**幂等**的（ALTER/CREATE IF NOT EXISTS + 只补差异）：连跑第二次
  ``backfilled`` 应为 0；旧读取路径（``load_details`` / ``stats`` /
  ``cross_run_delta`` / ``record_run``）行为不变；
- 只读打开（``open_store_readonly``）**不做迁移**：新查询接口用
  ``asset_schema_ready`` 显式探测，旧库上 ``query_jobs`` 抛 ``DbSchemaError``，
  ``stats``/``load_details`` 仍可用（优雅降级）。

规范化列的口径（写下来，避免"这一列到底啥意思"）：
- ``city`` 保持 v2 语义：**搜索口径城市**（调用方显式传的 city 优先），旧行回填
  时取 payload ``location`` 的城市段——与岗位实际城市在绝大多数情况下一致；
- ``district`` 取 ``location`` 的第二段（如 ``杭州·余杭区·仓前`` → ``余杭区``）；
- ``salary_min``/``salary_max`` 来自 ``job_model.parse_salary_parts``，单位随
  salary 原文（元/月 或 元/天），与交付物 CSV 的派生列同源；
- ``content_hash`` = 规范化 title/company/city + （若有）JD 内容指纹 的 sha256
  ——同内容稳定、JD 变化即变；``canonical_id`` = title/company/city 的 sha256
  前 16 位（同岗多 job_id 的归组基础，P1 再做 JD 感知的精确去重）；
- ``state``：写入时首次入库 ``new``、再次出现 ``active``；回填/刷新按
  ``last_seen_at`` 超过 ``STATE_STALE_DAYS`` 天标 ``stale``；``closed`` 只由
  显式证据写入（迁移不猜）。
"""

import hashlib
import json
import os
import pathlib
import re
import sqlite3
import unicodedata
from datetime import UTC, datetime, timedelta

try:
    from scripts import export_contract as _export_contract
    from scripts import job_model as _job_model
except ImportError:  # pragma: no cover - 直接运行脚本时走此分支
    import export_contract as _export_contract
    import job_model as _job_model

_sanitize_job = _export_contract._sanitize_job

# 默认库路径：仓库外（用户状态目录），与 job-result/、.session/ 同源约定
DEFAULT_DB_PATH = os.path.expanduser("~/.boss-zhipin-scraper/boss.db")
# v3：资产层（jobs 规范化列 + job_versions/job_flags + runs 元数据，2026-10-28 P0）
SCHEMA_VERSION = 3

# 岗位状态机（设计稿 §3.4）：new → active → stale → closed（closed 只由显式证据写入）
STATE_VALUES = ("new", "active", "stale", "closed")
# 超过 N 天没在列表里再出现 → stale（设计稿说"超过 N 天"，P0 取 14 天）
STATE_STALE_DAYS = 14
# 标记规则的版本：job_flags.rule_version（口径变更时换版本，旧行可查）
JOB_FLAG_RULE_VERSION = "2026-10-28"


class DbSchemaError(RuntimeError):
    """数据库 schema 高于当前代码可安全处理的版本。"""


_FINISHED_AT_UNSET = object()

# jobs 规范化列（v2 已有的 job_id/payload/first_seen_at/updated_at/run_id/keyword/city
# 之外新增的列）。``city`` 复用 v2 列，不重复建。
_ASSET_JOB_COLUMNS = (
    ("title", "TEXT"),
    ("company", "TEXT"),
    ("district", "TEXT"),
    ("salary_min", "INTEGER"),
    ("salary_max", "INTEGER"),
    ("salary_desc", "TEXT"),
    ("experience", "TEXT"),
    ("education", "TEXT"),
    ("recruit_type", "TEXT"),
    ("industry", "TEXT"),
    ("company_size", "TEXT"),
    ("url", "TEXT"),
    ("last_seen_at", "TEXT"),
    ("state", "TEXT"),
    ("content_hash", "TEXT"),
    ("canonical_id", "TEXT"),
)
_ASSET_JOB_COLUMN_NAMES = tuple(name for name, _decl in _ASSET_JOB_COLUMNS)

# runs 新增列（批次健康度 / 口径血缘，设计稿 §3.2）
_ASSET_RUN_COLUMNS = (
    ("scope_key", "TEXT"),
    ("template", "TEXT"),
    ("ruleset_version", "TEXT"),
    ("list_pages", "INTEGER"),
    ("detail_ok", "INTEGER"),
    ("detail_failed", "INTEGER"),
    ("budget_used", "INTEGER"),
    ("artifact_paths", "TEXT"),
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id          TEXT PRIMARY KEY,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    mode            TEXT,
    keyword         TEXT,
    city            TEXT,
    job_count       INTEGER NOT NULL DEFAULT 0,
    detail_count    INTEGER NOT NULL DEFAULT 0,
    scope_key       TEXT,
    template        TEXT,
    ruleset_version TEXT,
    list_pages      INTEGER,
    detail_ok       INTEGER,
    detail_failed   INTEGER,
    budget_used     INTEGER,
    artifact_paths  TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
    job_id        TEXT PRIMARY KEY,
    payload       TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    run_id        TEXT,
    keyword       TEXT,
    city          TEXT,
    title         TEXT,
    company       TEXT,
    district      TEXT,
    salary_min    INTEGER,
    salary_max    INTEGER,
    salary_desc   TEXT,
    experience    TEXT,
    education     TEXT,
    recruit_type  TEXT,
    industry      TEXT,
    company_size  TEXT,
    url           TEXT,
    last_seen_at  TEXT,
    state         TEXT,
    content_hash  TEXT,
    canonical_id  TEXT
);
CREATE TABLE IF NOT EXISTS details (
    job_id        TEXT PRIMARY KEY,
    jd            TEXT,
    payload       TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    run_id        TEXT
);
CREATE TABLE IF NOT EXISTS job_versions (
    job_id      TEXT NOT NULL,
    version     INTEGER NOT NULL,
    jd          TEXT,
    jd_hash     TEXT NOT NULL DEFAULT '',
    title       TEXT,
    salary_desc TEXT,
    skill_tags  TEXT,
    captured_at TEXT NOT NULL,
    run_id      TEXT,
    source      TEXT,
    PRIMARY KEY (job_id, version)
);
CREATE TABLE IF NOT EXISTS job_flags (
    job_id       TEXT NOT NULL,
    flag         TEXT NOT NULL,
    value        TEXT,
    rule_version TEXT,
    run_id       TEXT,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (job_id, flag)
);
CREATE INDEX IF NOT EXISTS idx_jobs_updated_at ON jobs(updated_at);
CREATE INDEX IF NOT EXISTS idx_details_updated_at ON details(updated_at);
CREATE INDEX IF NOT EXISTS idx_job_versions_jd_hash ON job_versions(jd_hash);
CREATE INDEX IF NOT EXISTS idx_job_flags_flag ON job_flags(flag);
"""

# 依赖 v3 新列（title/state/... ）的索引不能写进 _SCHEMA：老库在 ALTER 之前建索引
# 会直接报 "no such column"。迁移函数补完列之后再建（IF NOT EXISTS，幂等）。
_ASSET_JOB_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_jobs_last_seen_at ON jobs(last_seen_at)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_canonical_id ON jobs(canonical_id)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_recruit_type ON jobs(recruit_type)",
)

_SQLITE_MAX_VARS = 900  # 单条 SQL 变量上限（默认 999）内留余量，分批 IN 查询


# ---------------------------------------------------------------------------
# 0) 基础工具
# ---------------------------------------------------------------------------

def _utcnow():
    """UTC 秒级 ISO 时间戳（跨时区可比较；本地展示由消费端决定）。"""
    return datetime.now(UTC).isoformat(timespec="seconds")


def _chunked(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _payload_json(record):
    """脱敏后序列化：凭据 / securityId / BOSS 内部标识不落库（红线）。"""
    return json.dumps(_sanitize_job(record), ensure_ascii=False, sort_keys=True)


def _text(value):
    """payload 值 → 规范化文本列；缺失/空白返回 None（NULL，不写空串）。"""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_iso(value):
    """ISO 时间戳 → 带时区 datetime；解析失败返回 None。"""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _normalize_jd_text(jd):
    """JD 归一化（与 job_model 内容指纹的归一化规则一致，供 jd_hash 用）。"""
    text = unicodedata.normalize("NFKC", str(jd or "")).lower()
    return re.sub(r"\s+", "", text)


def _jd_hash(jd):
    """JD 内容指纹（sha256 前 32 位）；空 JD → 空串。"""
    text = _normalize_jd_text(jd)
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def compute_canonical_id(title=None, company=None, city=None):
    """岗位身份（同岗多 job_id 的归组键）：title/company/city 归一化后 sha256 前 16 位。

    刻意**不含 JD**：列表行通常没有 JD，含 JD 会导致同一岗位在不同阶段算出不同
    身份。P1 再做「JD 感知」的精确去重（见设计稿 §3.4）。
    """
    parts = [_job_model.normalize_title(value) for value in (title, company, city)]
    if not any(parts):
        return ""
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return "cn-" + digest[:16]


def compute_content_hash(job, canonical_id=""):
    """内容指纹：规范化身份 + （若有）JD 内容指纹 → sha256 前 32 位。

    同内容稳定（迁移/重复抓取不改哈希）；JD 一旦变化哈希即变（job_versions
    另行记录逐版历史）。
    """
    fingerprint = ""
    if isinstance(job, dict):
        try:
            fingerprint = _job_model.content_fingerprint(job) or ""
        except (TypeError, ValueError):
            fingerprint = ""
    basis = f"{canonical_id or ''}|{fingerprint}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


def _salary_bounds(salary):
    """薪资原文 → (min, max) 数值；解析不出返回 (None, None)。

    与交付物 CSV 的 salary_min/salary_max 同源（job_model.parse_salary_parts），
    单位随原文（元/月 或 元/天），不在这里做量纲换算。
    """
    try:
        low, high, _unit, _months = _job_model.parse_salary_parts(salary)
    except (TypeError, ValueError):
        return None, None

    def _number(value):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None

    return _number(low), _number(high)


def extract_job_columns(job, *, fallback_city=None):
    """payload → ``jobs`` 规范化列（缺失返回 None，不编造值）。

    ``fallback_city`` 用于回填：旧行 payload 没有 location 时保留 v2 的
    搜索口径城市，绝不把已有口径洗成 NULL。
    """
    if not isinstance(job, dict):
        return {}
    location = str(job.get("location") or "")
    parts = [part.strip() for part in location.split("·") if part.strip()]
    payload_city = parts[0] if parts else None
    district = parts[1] if len(parts) > 1 else None
    salary_min, salary_max = _salary_bounds(job.get("salary"))
    title = _text(job.get("title"))
    company = _text(job.get("company_name")) or _text(job.get("company"))
    city = _text(fallback_city) or payload_city
    recruit_type = _text(job.get("recruit_type"))
    if recruit_type is None:
        try:
            recruit_type = _job_model.classify_recruit_type(job)
        except (TypeError, ValueError):
            recruit_type = None
    canonical = compute_canonical_id(title, company, city)
    return {
        "title": title,
        "company": company,
        "city": city,
        "district": district,
        "salary_min": salary_min,
        "salary_max": salary_max,
        "salary_desc": _text(job.get("salary")),
        "experience": _text(job.get("experience")),
        "education": _text(job.get("education")),
        "recruit_type": recruit_type,
        "industry": (_text(job.get("company_industry"))
                     or _text(job.get("brand_industry_name"))),
        "company_size": (_text(job.get("company_scale"))
                         or _text(job.get("brand_scale_name"))),
        "url": _text(job.get("job_link")) or _text(job.get("link")),
        "canonical_id": canonical or None,
        "content_hash": compute_content_hash(job, canonical) if canonical else None,
    }


def _explicit_post_type(job):
    """发布主体类型的**平台证据**（不给公司名启发式打旗，避免和详情侧冲突）。

    列表 payload 没有 model 算好的 ``post_type``：只有 ``proxy_job``/``proxy_type``
    （平台标注代发）或 HR 职位=猎头顾问 才算证据；公司名"某…"这类匿名启发式
    交给 ``risk_flags.agency_placeholder``，两边不重复、不互相打架。
    """
    post = _text(job.get("post_type"))
    if post in ("agency", "anonymous"):
        return post
    if _text(job.get("proxy_job")) or _text(job.get("proxy_type")):
        return "agency"
    if _text(job.get("boss_title")) == "猎头顾问":
        return "agency"
    return ""


def extract_job_flags(job):
    """payload → ``job_flags`` 候选 ``[(flag, value)]``（去重保序）。

    只从 payload 里**已有**的事实派生，不在这里重跑删库/口径规则（那是规则层
    P1 的事，规则变更走 ``rule_version``）：
    - ``risk_flags``（详情侧写入的风险标记）→ 每个标记一行；
    - 招聘类型非社招 → ``recruit:intern`` / ``recruit:campus``；
    - 平台标注代发（proxy_job/proxy_type/猎头顾问/详情侧 post_type）→ ``post:agency``；
    - 详情侧判定的 AI 方向产品岗 → ``ai_pm``。
    """
    flags = []
    seen = set()

    def add(name):
        text = str(name or "").strip()
        if not text or text in seen:
            return
        seen.add(text)
        flags.append((text, "1"))

    if not isinstance(job, dict):
        return flags
    raw = job.get("risk_flags")
    if isinstance(raw, (list, tuple, set)):
        for item in raw:
            add(item)
    recruit = _text(job.get("recruit_type"))
    if recruit is None:
        try:
            recruit = _job_model.classify_recruit_type(job)
        except (TypeError, ValueError):
            recruit = ""
    if recruit and recruit != "social":
        add(f"recruit:{recruit}")
    post = _explicit_post_type(job)
    if post:
        add(f"post:{post}")
    if job.get("is_ai_pm") is True or _text(job.get("direction")) == "ai_pm":
        add("ai_pm")
    return flags


def _skill_tags_json(value):
    """技能标签 → JSON 文本（job_versions.skill_tags）；无值返回 None。"""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return json.dumps([text], ensure_ascii=False) if text else None
    if isinstance(value, (list, tuple, set)):
        items = [str(item).strip() for item in value if str(item).strip()]
        return json.dumps(items, ensure_ascii=False) if items else None
    return None


def _state_for(last_seen_at, *, now=None, stale_days=None):
    """按"最后出现时间"算状态：超过 stale_days 天 → stale，否则 active。

    解析不出时间时返回 ``active``（不拿缺失数据当"下架"证据）。
    """
    moment = _parse_iso(last_seen_at)
    if moment is None:
        return "active"
    now_dt = _parse_iso(now) or datetime.now(UTC)
    try:
        days = max(int(stale_days if stale_days is not None else STATE_STALE_DAYS), 0)
    except (TypeError, ValueError):
        days = STATE_STALE_DAYS
    return "stale" if (now_dt - moment) > timedelta(days=days) else "active"


# ---------------------------------------------------------------------------
# 1) schema / 迁移（幂等）
# ---------------------------------------------------------------------------

def stored_schema_version(conn):
    """读取已持久化 schema 版本；无表/无值返回 None，非法值 fail-closed。"""
    table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_meta'"
    ).fetchone()
    if table is None:
        return None
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()
    if row is None:
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError) as exc:
        raise DbSchemaError(f"schema_version 非法: {row[0]!r}") from exc


# 兼容旧内部名（同模块内历史调用/测试可能引用）
_stored_schema_version = stored_schema_version


def schema_version_at(path):
    """不打开写连接，读某个库文件的 schema 版本（迁移前快照用）。"""
    db_path = os.path.expanduser(str(path))
    if not os.path.exists(db_path):
        return None
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        return stored_schema_version(conn)
    finally:
        conn.close()


def _table_exists(conn, table):
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _table_columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _scalar(conn, sql, default=0):
    """只读标量查询；表/列缺失（旧库）时返回默认值，不抛异常。"""
    try:
        row = conn.execute(sql).fetchone()
    except sqlite3.Error:
        return default
    if row is None or row[0] is None:
        return default
    return row[0]


def _ensure_columns(conn, table, columns):
    """幂等补列：缺哪列 ALTER 哪列，已有列不动（迁移可反复执行）。"""
    existing = _table_columns(conn, table)
    added = []
    for name, decl in columns:
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
            added.append(name)
    return added


def _migrate_v1_to_v2(conn):
    """v1 → v2：jobs 表补 keyword/city 列（跨 run 按城市口径对账，#175）。

    旧行两列为 NULL → 不参与任何城市口径比对，记忆从此刻开始积累。幂等。
    """
    _ensure_columns(conn, "jobs", (("keyword", "TEXT"), ("city", "TEXT")))


def _migrate_to_asset_schema(conn):
    """v2 → v3：补 jobs 规范化列 / runs 元数据列（新表由 ``_SCHEMA`` 建）。幂等。"""
    _migrate_v1_to_v2(conn)
    _ensure_columns(conn, "jobs", _ASSET_JOB_COLUMNS)
    _ensure_columns(conn, "runs", _ASSET_RUN_COLUMNS)
    for statement in _ASSET_JOB_INDEXES:
        conn.execute(statement)


def open_store(path=None):
    """打开（必要时创建）SQLite 库并确保 schema，返回连接。

    已存在且版本高于当前代码时立即拒绝，不执行 WAL/DDL/版本覆盖等写操作。
    低版本库就地迁移到当前版本（v1→v2 补列、v2→v3 资产层），数据不动；
    列补齐用 ``_ensure_columns`` 幂等执行，重复打开不会重复 ALTER。
    """
    db_path = os.path.expanduser(path or DEFAULT_DB_PATH)
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        stored = stored_schema_version(conn)
        if stored is not None and stored > SCHEMA_VERSION:
            raise DbSchemaError(
                f"数据库 schema v{stored} 高于当前支持版本 v{SCHEMA_VERSION}")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript(_SCHEMA)
        # 老库的 CREATE TABLE IF NOT EXISTS 是空操作 → 必须显式补列（幂等）
        _migrate_to_asset_schema(conn)
        if stored is None:
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) "
                "VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
        elif stored < SCHEMA_VERSION:
            conn.execute(
                "UPDATE schema_meta SET value=? WHERE key='schema_version'",
                (str(SCHEMA_VERSION),),
            )
        conn.commit()
        return conn
    except (OSError, sqlite3.Error, DbSchemaError):
        conn.close()
        raise


def open_store_readonly(path):
    """以 SQLite read-only URI 打开已有库；绝不创建或修改文件。

    注意：只读打开**不迁移** schema。资产层新查询请先用 ``asset_schema_ready``
    探测，旧库上退回到 payload/统计类读取（``stats``/``load_details`` 均兼容）。
    """
    db_path = os.path.expanduser(path)
    if not os.path.exists(db_path):
        raise FileNotFoundError(db_path)
    uri = pathlib.Path(db_path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def asset_schema_ready(conn):
    """资产层（v3 列 + 新表）是否已就绪；旧库返回 False（调用方应优雅降级）。"""
    try:
        if not (_table_exists(conn, "job_versions")
                and _table_exists(conn, "job_flags")):
            return False
        columns = _table_columns(conn, "jobs")
    except sqlite3.Error:
        return False
    return bool({"canonical_id", "state", "content_hash"} <= columns)


# ---------------------------------------------------------------------------
# 2) 写入（payload 抽取 + 版本 + 标记）
# ---------------------------------------------------------------------------

def _upsert_rows(conn, table, rows, *, with_jd=False):
    """按 ``job_id`` 唯一键 upsert；返回 (inserted, updated)。

    ON CONFLICT 只更新 payload/updated_at/run_id（+jd），**不碰 first_seen_at**，
    从而保留"首次入库时间"语义。
    """
    if not rows:
        return 0, 0
    ids = [row[0] for row in rows]
    existing = set()
    for chunk in _chunked(ids, _SQLITE_MAX_VARS):
        placeholders = ",".join("?" for _ in chunk)
        existing.update(
            str(row[0]) for row in conn.execute(
                f"SELECT job_id FROM {table} WHERE job_id IN ({placeholders})", chunk)
        )
    inserted = sum(1 for jid in ids if jid not in existing)
    if with_jd:
        sql = (
            f"INSERT INTO {table} (job_id, jd, payload, first_seen_at, updated_at, run_id) "
            f"VALUES (?, ?, ?, ?, ?, ?) "
            f"ON CONFLICT(job_id) DO UPDATE SET jd=excluded.jd, payload=excluded.payload, "
            f"updated_at=excluded.updated_at, run_id=excluded.run_id"
        )
    else:
        sql = (
            f"INSERT INTO {table} (job_id, payload, first_seen_at, updated_at, run_id) "
            f"VALUES (?, ?, ?, ?, ?) "
            f"ON CONFLICT(job_id) DO UPDATE SET payload=excluded.payload, "
            f"updated_at=excluded.updated_at, run_id=excluded.run_id"
        )
    conn.executemany(sql, rows)
    conn.commit()
    return inserted, len(ids) - inserted


_JOBS_UPSERT_SQL = (
    "INSERT INTO jobs ("
    " job_id, payload, first_seen_at, updated_at, run_id, keyword, city,"
    " title, company, district, salary_min, salary_max, salary_desc, experience,"
    " education, recruit_type, industry, company_size, url, last_seen_at, state,"
    " content_hash, canonical_id)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    " ON CONFLICT(job_id) DO UPDATE SET"
    " payload=excluded.payload, updated_at=excluded.updated_at,"
    " run_id=excluded.run_id,"
    " keyword=COALESCE(excluded.keyword, jobs.keyword),"
    " city=COALESCE(excluded.city, jobs.city),"
    " title=COALESCE(excluded.title, jobs.title),"
    " company=COALESCE(excluded.company, jobs.company),"
    " district=COALESCE(excluded.district, jobs.district),"
    " salary_min=COALESCE(excluded.salary_min, jobs.salary_min),"
    " salary_max=COALESCE(excluded.salary_max, jobs.salary_max),"
    " salary_desc=COALESCE(excluded.salary_desc, jobs.salary_desc),"
    " experience=COALESCE(excluded.experience, jobs.experience),"
    " education=COALESCE(excluded.education, jobs.education),"
    " recruit_type=COALESCE(excluded.recruit_type, jobs.recruit_type),"
    " industry=COALESCE(excluded.industry, jobs.industry),"
    " company_size=COALESCE(excluded.company_size, jobs.company_size),"
    " url=COALESCE(excluded.url, jobs.url),"
    " last_seen_at=excluded.last_seen_at,"
    # 再次出现 = active；closed 的岗重新出现也视为重新上架（P1 再引入显式状态变更）
    " state='active',"
    " content_hash=COALESCE(excluded.content_hash, jobs.content_hash),"
    " canonical_id=COALESCE(excluded.canonical_id, jobs.canonical_id)"
)


def upsert_jobs(conn, jobs, *, run_id=None, keyword=None, city=None, now=None):
    """列表记录增量入库（唯一键 upsert，与 JSON/CSV 并存）。

    ``keyword``/``city`` 记录该行**最近所属口径**（跨 run 按城市对账用，#175）；
    ON CONFLICT 用 COALESCE——调用方没传口径时**绝不把已有口径洗成 NULL**。
    v3：同时把 payload 里的可查询字段抽成规范化列，并写 job_flags；payload 若已
    并入 JD（交付物/旧交付回填行），额外追加 JD 版本。
    """
    ts = now or _utcnow()
    rows = []
    sources = []
    for job in jobs or []:
        if not isinstance(job, dict):
            continue
        job_id = str(job.get("job_id") or "").strip()
        if not job_id:
            continue
        safe = _sanitize_job(job)
        columns = extract_job_columns(safe)
        scope_city = _text(city)
        if scope_city:
            # 显式搜索口径优先（保持 v2 的 cross_run 语义）；否则用岗位所在城市
            columns["city"] = scope_city
        rows.append((
            job_id, json.dumps(safe, ensure_ascii=False, sort_keys=True), ts, ts,
            run_id, keyword, columns["city"],
            columns["title"], columns["company"], columns["district"],
            columns["salary_min"], columns["salary_max"], columns["salary_desc"],
            columns["experience"], columns["education"], columns["recruit_type"],
            columns["industry"], columns["company_size"], columns["url"],
            ts, "new", columns["content_hash"], columns["canonical_id"],
        ))
        sources.append((job_id, safe))
    if not rows:
        return 0, 0
    ids = [row[0] for row in rows]
    existing = set()
    for chunk in _chunked(ids, _SQLITE_MAX_VARS):
        placeholders = ",".join("?" for _ in chunk)
        existing.update(
            str(row[0]) for row in conn.execute(
                "SELECT job_id FROM jobs WHERE job_id IN (" + placeholders + ")", chunk)
        )
    inserted = sum(1 for jid in ids if jid not in existing)
    conn.executemany(_JOBS_UPSERT_SQL, rows)
    # 资产层附加账：只增不改的 JD 版本 + 从 payload 派生的标记
    for job_id, safe in sources:
        if str(safe.get("jd") or "").strip():
            append_version_from_payload(conn, safe, captured_at=ts, run_id=run_id,
                                        source="list")
        _write_job_flags(conn, job_id, extract_job_flags(safe), run_id=run_id, now=ts)
    conn.commit()
    return inserted, len(ids) - inserted


def upsert_details(conn, details, *, run_id=None, now=None):
    """详情记录增量入库（抽出 jd 列便于 SQL 过滤/统计）。

    v3：同一 job 的 JD/标题/薪资变化追加 ``job_versions``（内容没变不重复追加），
    并写 job_flags（详情侧才有 risk_flags / direction）。
    """
    ts = now or _utcnow()
    rows = []
    sources = []
    for detail in details or []:
        if not isinstance(detail, dict):
            continue
        job_id = str(detail.get("job_id") or "").strip()
        if not job_id:
            continue
        safe = _sanitize_job(detail)
        jd = str(safe.get("jd") or "").strip()
        rows.append((job_id, jd, json.dumps(safe, ensure_ascii=False, sort_keys=True),
                     ts, ts, run_id))
        sources.append((job_id, safe))
    result = _upsert_rows(conn, "details", rows, with_jd=True)
    if not rows:
        return result
    for job_id, safe in sources:
        append_version_from_payload(conn, safe, captured_at=ts, run_id=run_id,
                                    source="detail")
        _write_job_flags(conn, job_id, extract_job_flags(safe), run_id=run_id, now=ts)
    conn.commit()
    return result


def _latest_version(conn, job_id):
    if not _table_exists(conn, "job_versions"):
        return None
    return conn.execute(
        "SELECT version, jd_hash, title, salary_desc FROM job_versions "
        "WHERE job_id=? ORDER BY version DESC LIMIT 1", (str(job_id),)
    ).fetchone()


def _version_changed(latest, jd_hash, title, salary_desc):
    """版本是否变化：JD 指纹不同 → 变；标题/薪资**只有本次带了值**才参与比较。

    为什么不能直接比 None：详情快照/交付物行常常只有 ``job_id+jd``（没有 title/
    salary），若把"本次没给"当成"变了"，每跑一次迁移就会追一条内容相同、字段为空
    的版本（实测 2026-10-28 抖动）。"没给"= 不改，不是"清空"。
    """
    if latest is None:
        return bool(jd_hash or title or salary_desc)
    if (latest["jd_hash"] or "") != jd_hash:
        return True
    if title is not None and _text(latest["title"]) != title:
        return True
    return salary_desc is not None and _text(latest["salary_desc"]) != salary_desc


def _recorded_version(conn, job_id, jd_hash, memo):
    """同一 JD 指纹是否已记录（DB 或 dry-run 影子表）。

    版本身份 = JD 内容指纹：**同一指纹只留一个版本**。产物目录回填会按文件
    时间顺序重放历史 JD，若只看"最新版本"，重跑时会因为顺序不同把旧 JD 再
    追一遍（实测 2026-10-28 第二次迁移多出 38 条重复版本）。
    """
    if not jd_hash:
        return None
    if memo is not None:
        entry = memo.get(job_id)
        if entry is not None and jd_hash in entry.get("hashes", ()):
            return entry
    if _table_exists(conn, "job_versions"):
        return conn.execute(
            "SELECT version, title, salary_desc FROM job_versions "
            "WHERE job_id=? AND jd_hash=? ORDER BY version ASC LIMIT 1",
            (job_id, jd_hash)).fetchone()
    return None


def _fill_version_fields(conn, job_id, jd_hash, title, salary_desc):
    """同一 JD 已有版本时：只补该行缺失的 title/salary（不新增版本、不覆盖）。"""
    if not (title or salary_desc) or not _table_exists(conn, "job_versions"):
        return
    conn.execute(
        "UPDATE job_versions SET title=COALESCE(title, ?),"
        " salary_desc=COALESCE(salary_desc, ?) WHERE job_id=? AND jd_hash=?",
        (title, salary_desc, job_id, jd_hash))


def _append_version(conn, job_id, *, jd="", title=None, salary_desc=None,
                    skill_tags=None, captured_at=None, run_id=None, source="detail",
                    dry_run=False, memo=None):
    """变化才追加一个 JD 版本；返回新版本号，未变化返回 None。

    dry_run=True 时只算"会不会追加"，不写库（assets_migrate --dry-run 用）。
    ``memo`` 是 dry-run 的**影子版本表**（``{job_id: {...}}``，含已记录 hash 集合）：
    dry-run 不写库，若不记在内存里，同一 job 的 N 条相同 JD 会被算成 N 次追加
    （实测真实库 dry-run 因此虚报 8181 条，实际只新增 ~430 条）。
    """
    job_id = str(job_id or "").strip()
    if not job_id:
        return None
    jd_hash = _jd_hash(jd)
    title = _text(title)
    salary_desc = _text(salary_desc)
    if not (jd_hash or title or salary_desc):
        return None
    ready = _table_exists(conn, "job_versions")
    latest = memo.get(job_id) if memo is not None else None
    if latest is None and ready:
        latest = _latest_version(conn, job_id)
    recorded = _recorded_version(conn, job_id, jd_hash, memo)
    if recorded is not None:
        # 同一 JD 已记录：只补缺失元数据，不新增版本（重跑幂等的关键）
        if not dry_run and ready:
            _fill_version_fields(conn, job_id, jd_hash, title, salary_desc)
        return None
    if not _version_changed(latest, jd_hash, title, salary_desc):
        return None
    version = (int(latest["version"]) + 1) if latest is not None else 1
    if dry_run or not ready:
        if memo is not None:
            hashes = set(latest.get("hashes", ())) if isinstance(latest, dict) else set()
            hashes.add(jd_hash)
            memo[job_id] = {"version": version, "jd_hash": jd_hash,
                            "title": title, "salary_desc": salary_desc,
                            "hashes": hashes}
        return version
    conn.execute(
        "INSERT INTO job_versions (job_id, version, jd, jd_hash, title, salary_desc,"
        " skill_tags, captured_at, run_id, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (job_id, version, str(jd or ""), jd_hash, title, salary_desc,
         _skill_tags_json(skill_tags), captured_at or _utcnow(), run_id, source),
    )
    return version


def append_job_version(conn, job_id, *, jd="", title=None, salary_desc=None,
                       skill_tags=None, captured_at=None, run_id=None,
                       source="detail", dry_run=False, memo=None):
    """追加 JD 版本（内容未变不追加）；提交事务后返回版本号/None。"""
    version = _append_version(
        conn, job_id, jd=jd, title=title, salary_desc=salary_desc,
        skill_tags=skill_tags, captured_at=captured_at, run_id=run_id,
        source=source, dry_run=dry_run, memo=memo)
    if version is not None and not dry_run:
        conn.commit()
    return version


def append_version_from_payload(conn, job, *, captured_at=None, run_id=None,
                                source="detail", dry_run=False, memo=None):
    """从一条 payload（列表/详情/交付物行）追加 JD 版本。"""
    if not isinstance(job, dict):
        return None
    job_id = str(job.get("job_id") or "").strip()
    if not job_id:
        return None
    return append_job_version(
        conn, job_id, jd=str(job.get("jd") or ""),
        title=job.get("title"), salary_desc=job.get("salary"),
        skill_tags=job.get("skill_tags") or job.get("skills"),
        captured_at=captured_at, run_id=run_id, source=source, dry_run=dry_run,
        memo=memo)


def _write_job_flags(conn, job_id, flags, *, rule_version=None, run_id=None,
                     now=None, dry_run=False, memo=None):
    """写 job_flags（按 job_id+flag upsert）；返回内容真正变化/会变化的行数。

    幂等关键（2026-10-28）：变更判定只看 ``value + rule_version``；``run_id``
    是血缘不是内容——本次没带 run_id（产物回填/迁移）时绝不覆盖已有血缘，
    带了新 run_id 时静默刷新但不计入 backfilled（否则迁移每跑一次都在
    "r1 ↔ NULL" 之间抖动）。``memo`` 是 dry-run 的影子标记表（未迁移的旧库上
    没有 job_flags 表，不记内存就会把同一条标记重复计数）。
    """
    flags = list(flags or [])
    if not flags:
        return 0
    rule = rule_version or JOB_FLAG_RULE_VERSION
    job_id = str(job_id)
    ready = _table_exists(conn, "job_flags")
    if not ready:
        if not dry_run:
            return 0
        if memo is None:
            return len(flags)
        counted = 0
        for name, value in flags:
            name = str(name or "").strip()
            if not name:
                continue
            key = (job_id, name)
            content = (None if value is None else str(value), rule)
            if memo.get(key) == content:
                continue
            memo[key] = content
            counted += 1
        return counted
    ts = now or _utcnow()
    changed = 0
    for name, value in flags:
        name = str(name or "").strip()
        if not name:
            continue
        text_value = None if value is None else str(value)
        key = (job_id, name)
        content = (text_value, rule)
        if memo is not None and key in memo:
            same_content = memo[key] == content
            refresh_run = False
        else:
            current = conn.execute(
                "SELECT value, rule_version, run_id FROM job_flags "
                "WHERE job_id=? AND flag=?", (job_id, name)
            ).fetchone()
            same_content = current is not None and (
                (current["value"] or None) == text_value
                and (current["rule_version"] or None) == rule)
            refresh_run = (current is not None and same_content
                           and run_id is not None
                           and (current["run_id"] or None) != run_id)
        if same_content and not refresh_run:
            if memo is not None:
                memo[key] = content
            continue
        if not same_content:
            changed += 1
        if memo is not None:
            memo[key] = content
        if dry_run:
            continue
        conn.execute(
            "INSERT INTO job_flags (job_id, flag, value, rule_version, run_id,"
            " updated_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(job_id, flag) DO UPDATE SET value=excluded.value,"
            " rule_version=excluded.rule_version,"
            " run_id=COALESCE(excluded.run_id, job_flags.run_id),"
            " updated_at=excluded.updated_at",
            (job_id, name, text_value, rule, run_id, ts),
        )
    return changed


def write_job_flags(conn, job_id, flags, *, rule_version=None, run_id=None,
                    now=None, dry_run=False, memo=None):
    """公开的标记写入（job_id + [(flag, value)]）；返回写入行数。"""
    changed = _write_job_flags(conn, job_id, flags, rule_version=rule_version,
                               run_id=run_id, now=now, dry_run=dry_run,
                               memo=memo)
    if changed and not dry_run:
        conn.commit()
    return changed


def record_job_flag(conn, job_id, flag, value=None, *, rule_version=None,
                    run_id=None, now=None):
    """记录单个标记；返回是否写入（值/版本/run 都没变返回 False）。"""
    return bool(write_job_flags(
        conn, job_id, [(flag, value)], rule_version=rule_version, run_id=run_id,
        now=now))


# ---------------------------------------------------------------------------
# 3) 只读读取（v2 兼容面 + 资产层查询）
# ---------------------------------------------------------------------------

def load_job_ids(conn):
    """库内全部列表 job_id 集合。"""
    return {str(row[0]) for row in conn.execute("SELECT job_id FROM jobs")}


def load_city_job_ids(conn, city):
    """同城市口径的历史 job_id 集合（city 列 NULL 的旧行不参与，#175）。"""
    city = str(city or "").strip()
    if not city:
        return set()
    return {str(row[0]) for row in conn.execute(
        "SELECT job_id FROM jobs WHERE city = ?", (city,))}


def cross_run_delta(conn, city, current_job_ids):
    """同城市口径的跨 run 对账（#175）：新增 / 本次没搜到 / 仍在。

    - added：本次列表里、库里该城市从没见过的（= 今天新增候选）
    - removed：库里该城市见过、本次列表没有的（下架/改口候选，**只是信号**——
      平台模糊召回本来就会漏，不等于真下架）
    - 首次运行（库内该城市还没有任何记录）→ baseline=True，全部算已知
    - 城市为空 → None（无口径可比）
    """
    city = str(city or "").strip()
    if not city:
        return None
    current = sorted({str(j).strip() for j in (current_job_ids or [])
                      if str(j).strip()})
    known = load_city_job_ids(conn, city)
    if not known:
        return {"baseline": True, "scope_city": city,
                "added": current, "added_count": len(current),
                "removed": [], "removed_count": 0, "kept_count": 0}
    added = [jid for jid in current if jid not in known]
    removed = sorted(known - set(current))
    return {"baseline": False, "scope_city": city,
            "added": added, "added_count": len(added),
            "removed": removed, "removed_count": len(removed),
            "kept_count": len(current) - len(added)}


def load_detail_ids(conn):
    """库内**含 JD** 的详情 job_id 集合（断点续抓种子）。"""
    return {str(row[0]) for row in conn.execute(
        "SELECT job_id FROM details WHERE jd IS NOT NULL AND TRIM(jd) != ''")}


def load_details(conn, job_ids=None):
    """读回详情记录（payload 反序列化为 dict），可按 job_id 子集过滤。

    仅返回含 JD 的行；损坏 payload 静默跳过（不阻塞续抓）。
    """
    results = []

    def collect(sql, params):
        for row in conn.execute(sql, params):
            try:
                payload = json.loads(row[0])
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            if isinstance(payload, dict) and payload.get("job_id"):
                results.append(payload)

    if job_ids is None:
        collect("SELECT payload FROM details WHERE jd IS NOT NULL AND TRIM(jd) != ''", [])
        return results
    ids = [str(j).strip() for j in job_ids if str(j).strip()]
    for chunk in _chunked(ids, _SQLITE_MAX_VARS):
        placeholders = ",".join("?" for _ in chunk)
        collect(
            f"SELECT payload FROM details WHERE jd IS NOT NULL AND TRIM(jd) != '' "
            f"AND job_id IN ({placeholders})", chunk)
    return results


def load_job_versions(conn, job_id):
    """某个岗位的 JD 版本历史（按 version 升序）；旧库/无记录返回 []。"""
    if not _table_exists(conn, "job_versions"):
        return []
    try:
        rows = conn.execute(
            "SELECT job_id, version, jd, jd_hash, title, salary_desc, skill_tags,"
            " captured_at, run_id, source FROM job_versions "
            "WHERE job_id=? ORDER BY version ASC", (str(job_id),)
        ).fetchall()
    except sqlite3.Error:
        return []
    return [dict(row) for row in rows]


def load_job_flags(conn, job_id=None, flag=None):
    """读 job_flags：可按 job_id / flag 过滤；旧库返回 []。"""
    if not _table_exists(conn, "job_flags"):
        return []
    sql = ("SELECT job_id, flag, value, rule_version, run_id, updated_at "
           "FROM job_flags")
    params = []
    where = []
    if job_id is not None:
        where.append("job_id=?")
        params.append(str(job_id))
    if flag is not None:
        where.append("flag=?")
        params.append(str(flag))
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY job_id, flag"
    try:
        return [dict(row) for row in conn.execute(sql, params)]
    except sqlite3.Error:
        return []


def load_job_asset(conn, job_id):
    """取单个岗位的资产视图（规范化列 + payload + 版本 + 标记）。

    旧库（未迁移）抛 ``DbSchemaError``——调用方应先跑 ``assets_migrate`` 或
    用 ``asset_schema_ready`` 探测后降级。
    """
    if not asset_schema_ready(conn):
        raise DbSchemaError(
            f"assets schema 未就绪（需要迁移到 v{SCHEMA_VERSION}）；请先运行 "
            "scripts/assets_migrate.py")
    columns = list(_ASSET_JOB_COLUMN_NAMES)
    sql = ("SELECT job_id, payload, first_seen_at, updated_at, run_id, keyword,"
           " city, " + ", ".join(columns) + " FROM jobs WHERE job_id=?")
    row = conn.execute(sql, (str(job_id),)).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row["payload"])
    except (json.JSONDecodeError, TypeError, ValueError):
        payload = None
    asset = {
        "job_id": row["job_id"],
        "payload": payload,
        "first_seen_at": row["first_seen_at"],
        "updated_at": row["updated_at"],
        "run_id": row["run_id"],
        "keyword": row["keyword"],
    }
    for name in columns:
        asset[name] = row[name]
    asset["versions"] = load_job_versions(conn, asset["job_id"])
    asset["flags"] = load_job_flags(conn, asset["job_id"])
    return asset


def query_jobs(conn, *, city=None, recruit_type=None, state=None, since=None,
               limit=None):
    """按规范化列查岗位（asset 视图，不含 payload 大字段）。

    - ``city``：精确匹配 ``jobs.city``（搜索口径城市）；
    - ``recruit_type``：``social`` / ``intern`` / ``campus``；
    - ``state``：``new`` / ``active`` / ``stale`` / ``closed``；
    - ``since``：``last_seen_at >= since``（ISO 文本，UTC 同格式比较）；
    - ``limit``：最多返回条数。

    旧库（未迁移）抛 ``DbSchemaError``，不做静默空结果（空结果会被误读成
    "没有岗位"）。
    """
    if not asset_schema_ready(conn):
        raise DbSchemaError(
            f"assets schema 未就绪（需要迁移到 v{SCHEMA_VERSION}）；请先运行 "
            "scripts/assets_migrate.py")
    sql = ("SELECT job_id, title, company, city, district, salary_min, salary_max,"
           " salary_desc, experience, education, recruit_type, industry,"
           " company_size, url, first_seen_at, last_seen_at, state,"
           " content_hash, canonical_id FROM jobs")
    where = []
    params = []
    if city:
        where.append("city = ?")
        params.append(str(city).strip())
    if recruit_type:
        where.append("recruit_type = ?")
        params.append(str(recruit_type).strip())
    if state:
        where.append("state = ?")
        params.append(str(state).strip())
    if since:
        where.append("last_seen_at IS NOT NULL AND last_seen_at >= ?")
        params.append(str(since).strip())
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY last_seen_at DESC, job_id ASC"
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    return [dict(row) for row in conn.execute(sql, params)]


def recent_runs(conn, limit=10):
    """最近 N 次 run（含 v3 的批次健康度/口径列；旧库自动少掉缺的列）。"""
    try:
        rows = conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?",
            (max(int(limit or 0), 0),)
        ).fetchall()
    except (sqlite3.Error, TypeError, ValueError):
        return []
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# 4) run lineage（批次健康度 / 口径血缘）
# ---------------------------------------------------------------------------

def _artifact_paths_json(value):
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return None


def record_run(conn, run_id, *, mode=None, keyword=None, city=None,
               started_at=None, finished_at=_FINISHED_AT_UNSET, job_count=0,
               detail_count=0, scope_key=None, template=None,
               ruleset_version=None, list_pages=None, detail_ok=None,
               detail_failed=None, budget_used=None, artifact_paths=None,
               now=None):
    """记录/更新一次 run；显式 ``finished_at=None`` 保持未完成状态。

    v3 增补批次健康度与口径血缘（scope_key/template/ruleset_version/list_pages/
    detail_ok/detail_failed/budget_used/artifact_paths）；ON CONFLICT 用 COALESCE，
    局部更新（如先建 open 行、收尾再补数字）不会把已有值洗成 NULL。
    """
    ts = now or _utcnow()
    finish = ts if finished_at is _FINISHED_AT_UNSET else finished_at
    conn.execute(
        "INSERT INTO runs (run_id, started_at, finished_at, mode, keyword, city,"
        " job_count, detail_count, scope_key, template, ruleset_version, list_pages,"
        " detail_ok, detail_failed, budget_used, artifact_paths)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(run_id) DO UPDATE SET mode=excluded.mode,"
        " keyword=excluded.keyword, city=excluded.city,"
        " finished_at=excluded.finished_at, job_count=excluded.job_count,"
        " detail_count=excluded.detail_count,"
        " scope_key=COALESCE(excluded.scope_key, runs.scope_key),"
        " template=COALESCE(excluded.template, runs.template),"
        " ruleset_version=COALESCE(excluded.ruleset_version, runs.ruleset_version),"
        " list_pages=COALESCE(excluded.list_pages, runs.list_pages),"
        " detail_ok=COALESCE(excluded.detail_ok, runs.detail_ok),"
        " detail_failed=COALESCE(excluded.detail_failed, runs.detail_failed),"
        " budget_used=COALESCE(excluded.budget_used, runs.budget_used),"
        " artifact_paths=COALESCE(excluded.artifact_paths, runs.artifact_paths)",
        (run_id, started_at or ts, finish, mode, keyword, city,
         int(job_count or 0), int(detail_count or 0),
         _text(scope_key), _text(template), _text(ruleset_version),
         None if list_pages is None else int(list_pages),
         None if detail_ok is None else int(detail_ok),
         None if detail_failed is None else int(detail_failed),
         None if budget_used is None else int(budget_used),
         _artifact_paths_json(artifact_paths)),
    )
    conn.commit()
    return run_id


# ---------------------------------------------------------------------------
# 5) 状态机 / 统计
# ---------------------------------------------------------------------------

def refresh_job_states(conn, *, now=None, stale_days=None):
    """按 ``last_seen_at`` 重算 active/stale（不碰 ``closed``）；返回更新行数。"""
    if not asset_schema_ready(conn):
        return 0
    rows = conn.execute(
        "SELECT job_id, state, COALESCE(last_seen_at, updated_at) AS seen_at FROM jobs"
    ).fetchall()
    updated = 0
    for row in rows:
        if str(row["state"] or "") == "closed":
            continue
        desired = _state_for(row["seen_at"], now=now, stale_days=stale_days)
        if (row["state"] or None) == desired:
            continue
        conn.execute("UPDATE jobs SET state=? WHERE job_id=?",
                     (desired, row["job_id"]))
        updated += 1
    if updated:
        conn.commit()
    return updated


def asset_stats(conn):
    """资产账统计（旧库自动降级为 0，不抛异常）。"""
    states = {}
    try:
        for row in conn.execute(
                "SELECT state, COUNT(*) FROM jobs GROUP BY state"):
            states[str(row[0] or "unknown")] = int(row[1])
    except sqlite3.Error:
        states = {}
    return {
        "jobs_with_canonical": _scalar(
            conn, "SELECT COUNT(*) FROM jobs WHERE canonical_id IS NOT NULL", 0),
        "jobs_with_content_hash": _scalar(
            conn, "SELECT COUNT(*) FROM jobs WHERE content_hash IS NOT NULL", 0),
        "canonical_groups": _scalar(
            conn, "SELECT COUNT(DISTINCT canonical_id) FROM jobs "
                  "WHERE canonical_id IS NOT NULL", 0),
        "job_versions": _scalar(conn, "SELECT COUNT(*) FROM job_versions", 0),
        "jobs_with_versions": _scalar(
            conn, "SELECT COUNT(DISTINCT job_id) FROM job_versions", 0),
        "job_flags": _scalar(conn, "SELECT COUNT(*) FROM job_flags", 0),
        "states": states,
    }


def stats(conn):
    """只读统计：列表/详情/含 JD/runs 计数与最近更新时间（+ 资产层计数）。"""
    db_path = None
    for row in conn.execute("PRAGMA database_list"):
        if row[1] == "main":
            db_path = row[2]
    stored_schema = None
    try:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        if row is not None:
            stored_schema = int(row[0])
    except (sqlite3.Error, TypeError, ValueError):
        stored_schema = None
    info = {
        "db_path": db_path,
        "schema_version": stored_schema,
        "jobs": conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
        "details": conn.execute("SELECT COUNT(*) FROM details").fetchone()[0],
        "details_with_jd": conn.execute(
            "SELECT COUNT(*) FROM details WHERE jd IS NOT NULL AND TRIM(jd) != ''"
        ).fetchone()[0],
        "runs": conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0],
        "last_job_update": conn.execute("SELECT MAX(updated_at) FROM jobs").fetchone()[0],
        "job_versions": _scalar(conn, "SELECT COUNT(*) FROM job_versions", 0),
        "job_flags": _scalar(conn, "SELECT COUNT(*) FROM job_flags", 0),
        "states": asset_stats(conn)["states"],
    }
    return info


# ---------------------------------------------------------------------------
# 6) 回填（迁移用；幂等：只补差异，连跑第二次 backfilled=0）
# ---------------------------------------------------------------------------

def _same_value(current, desired):
    """回填比较：NULL 与空串等价（不因空串/NULL 差异反复写库）。"""
    if current is None or current == "":
        return desired is None or desired == ""
    return current == desired


def backfill_jobs(conn, *, dry_run=False, stale_days=None, now=None, memo=None):
    """从 payload 回填 ``jobs`` 规范化列（只补/改差异，幂等）。

    返回 ``{scanned, backfilled, skipped, failed, columns_filled}``。
    dry_run=True 时只统计"会改多少行/哪些列"，不写库；``memo`` 是影子列集合
    （``{job_id: {column}}``），供同一轮 dry-run 的产物回填参考，避免重复计数。
    """
    existing = _table_columns(conn, "jobs")
    select = ["job_id", "payload", "updated_at", "city"]
    select += [name for name in _ASSET_JOB_COLUMN_NAMES if name in existing]
    stats = {
        "scanned": 0,
        "backfilled": 0,
        "skipped": 0,
        "failed": 0,
        "columns_filled": {name: 0 for name in _ASSET_JOB_COLUMN_NAMES},
    }
    for row in conn.execute("SELECT " + ", ".join(select) + " FROM jobs"):
        stats["scanned"] += 1
        try:
            payload = json.loads(row["payload"])
        except (json.JSONDecodeError, TypeError, ValueError):
            stats["failed"] += 1
            continue
        if not isinstance(payload, dict):
            stats["failed"] += 1
            continue
        columns = extract_job_columns(payload, fallback_city=row["city"])
        desired = {name: columns.get(name) for name in _ASSET_JOB_COLUMN_NAMES}
        last_seen = _text(row["updated_at"])
        desired["last_seen_at"] = last_seen
        desired["state"] = _state_for(last_seen, now=now, stale_days=stale_days)
        updates = {}
        for name, value in desired.items():
            if value is None:
                continue  # 回填只补齐，不抹掉已有值
            current = row[name] if name in existing else None
            if _same_value(current, value):
                continue
            updates[name] = value
        if not updates:
            stats["skipped"] += 1
            continue
        stats["backfilled"] += 1
        for name in updates:
            stats["columns_filled"][name] += 1
        if dry_run:
            if memo is not None:
                memo.setdefault(row["job_id"], set()).update(updates)
            continue
        assignments = ", ".join(f"{name}=?" for name in updates)
        conn.execute(f"UPDATE jobs SET {assignments} WHERE job_id=?",
                     (*updates.values(), row["job_id"]))
    if not dry_run:
        conn.commit()
    return stats


def backfill_job_versions(conn, *, dry_run=False, memo=None):
    """从 ``details`` 回填 ``job_versions``（同 JD 不重复追加）。

    ``memo``：dry-run 的影子版本表（见 ``_append_version``），与产物目录回填
    共用可让 dry-run 的"将追加版本数"不被重复记录虚报。
    """
    stats = {"scanned": 0, "backfilled": 0, "skipped": 0, "failed": 0}
    for row in conn.execute(
            "SELECT job_id, jd, payload, run_id, updated_at FROM details "
            "WHERE jd IS NOT NULL AND TRIM(jd) != ''"):
        stats["scanned"] += 1
        try:
            payload = json.loads(row["payload"])
        except (json.JSONDecodeError, TypeError, ValueError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        version = _append_version(
            conn, row["job_id"], jd=row["jd"],
            title=payload.get("title"), salary_desc=payload.get("salary"),
            skill_tags=payload.get("skill_tags") or payload.get("skills"),
            captured_at=_text(row["updated_at"]), run_id=row["run_id"],
            source="migrate:details", dry_run=dry_run, memo=memo,
        )
        if version is None:
            stats["skipped"] += 1
        else:
            stats["backfilled"] += 1
    if not dry_run:
        conn.commit()
    return stats


def backfill_job_flags(conn, *, dry_run=False, now=None, memo=None):
    """从 ``jobs`` / ``details`` payload 回填 ``job_flags``（按 flag 去重）。

    ``memo``：dry-run 的影子标记表（见 ``_write_job_flags``），与产物目录回填共用
    避免同一条标记被重复计数。
    """
    stats = {"scanned": 0, "backfilled": 0, "skipped": 0, "failed": 0}
    candidates = {}
    for row in conn.execute("SELECT job_id, payload, run_id FROM jobs"):
        stats["scanned"] += 1
        try:
            payload = json.loads(row["payload"])
        except (json.JSONDecodeError, TypeError, ValueError):
            stats["failed"] += 1
            continue
        if not isinstance(payload, dict):
            stats["failed"] += 1
            continue
        candidates[row["job_id"]] = (extract_job_flags(payload), row["run_id"])
    # 详情侧标记更全（risk_flags/direction 只在详情 payload 里）：覆盖同名 flag
    for row in conn.execute("SELECT job_id, payload, run_id FROM details"):
        try:
            payload = json.loads(row["payload"])
        except (json.JSONDecodeError, TypeError, ValueError):
            stats["failed"] += 1
            continue
        if not isinstance(payload, dict):
            continue
        detail_flags = extract_job_flags(payload)
        if not detail_flags:
            continue
        merged = dict(candidates.get(row["job_id"], ([], None))[0])
        for name, value in detail_flags:
            # 同一命名空间（post:/recruit:）以详情侧为准：避免一个岗位同时挂
            # post:anonymous（列表侧按公司名猜）和 post:agency（详情侧平台标注）
            if ":" in name:
                prefix = name.split(":", 1)[0] + ":"
                merged = {key: val for key, val in merged.items()
                          if not key.startswith(prefix)}
            merged[name] = value
        candidates[row["job_id"]] = (list(merged.items()), row["run_id"])
    for job_id, (flags, run_id) in candidates.items():
        if not flags:
            stats["skipped"] += 1
            continue
        changed = _write_job_flags(conn, job_id, flags, run_id=run_id,
                                   now=now, dry_run=dry_run, memo=memo)
        stats["backfilled"] += changed
        stats["skipped"] += len(flags) - changed
    if not dry_run:
        conn.commit()
    return stats


def backfill_runs(conn, *, dry_run=False):
    """``runs`` 元数据回填占位：旧 run 没有可推导的 scope/template 证据。

    刻意不猜（历史 run 的 scope_key 推错会污染口径账）；返回全 0 统计，
    新 run 由 ``record_run`` 写入这 8 个字段。
    """
    scanned = _scalar(conn, "SELECT COUNT(*) FROM runs", 0)
    return {"scanned": int(scanned), "backfilled": 0,
            "skipped": int(scanned), "failed": 0}


def merge_job_columns(conn, job, *, dry_run=False, memo=None):
    """用产物文件里的 payload **只补 NULL 列**（产物账 → 资产账，不改已有值）。

    返回 ``{job_id, matched, updated}``；``matched=False`` 表示库里没有这个岗位
    （本函数不插入新岗位——产物回填只补资产，不扩资产）。``dry_run=True`` 时
    在未迁移的旧库上也能统计"会补多少列"（按缺失列当 NULL 算）；``memo`` 是
    dry-run 的影子列集合（``{job_id: {column}}``），与 DB 阶段模拟共用，
    避免把同一行在两步里重复计成"回填"。
    """
    job_id = str((job or {}).get("job_id") or "").strip()
    result = {"job_id": job_id, "matched": False, "updated": 0}
    if not job_id:
        return result
    existing = _table_columns(conn, "jobs")
    select_columns = [name for name in _ASSET_JOB_COLUMN_NAMES if name in existing]
    sql = "SELECT job_id, city"
    if select_columns:
        sql += ", " + ", ".join(select_columns)
    sql += " FROM jobs WHERE job_id=?"
    row = conn.execute(sql, (job_id,)).fetchone()
    if row is None:
        return result
    result["matched"] = True
    row_keys = set(row.keys())
    filled = memo.get(job_id, set()) if memo is not None else set()
    extracted = extract_job_columns(job, fallback_city=row["city"])
    updates = {}
    for name in _ASSET_JOB_COLUMN_NAMES:
        value = extracted.get(name)
        if value is None:
            continue
        if name in filled:
            continue
        # 产物回填只补缺失列，绝不覆盖资产层已有值（payload 才是原始账）
        if name in row_keys and row[name] is not None:
            continue
        updates[name] = value
    result["updated"] = len(updates)
    if memo is not None and updates:
        memo.setdefault(job_id, set()).update(updates)
    if dry_run or not updates:
        return result
    writable = {name: value for name, value in updates.items() if name in existing}
    if not writable:
        return result
    assignments = ", ".join(f"{name}=?" for name in writable)
    conn.execute(f"UPDATE jobs SET {assignments} WHERE job_id=?",
                 (*writable.values(), job_id))
    conn.commit()
    return result


# ---------------------------------------------------------------------------
# 7) 独立只读入口
# ---------------------------------------------------------------------------

def _main(argv=None):
    """独立只读入口：``python -m scripts.db_store [DB_PATH]`` 打印统计。"""
    import argparse  # 局部导入：主流程无 CLI 依赖

    parser = argparse.ArgumentParser(
        prog="db_store",
        description=f"SQLite 增量层只读统计（默认库 {DEFAULT_DB_PATH}）")
    parser.add_argument("db", nargs="?", default=DEFAULT_DB_PATH,
                        help="SQLite 库路径（默认 ~/.boss-zhipin-scraper/boss.db）")
    args = parser.parse_args(argv)
    if not os.path.exists(args.db):
        print(f"数据库不存在: {args.db}")
        return 1
    try:
        conn = open_store_readonly(args.db)
    except (OSError, sqlite3.Error) as exc:
        print(f"数据库只读打开失败: {exc}")
        return 1
    try:
        info = stats(conn)
    except sqlite3.Error as exc:
        print(f"数据库统计失败: {exc}")
        return 1
    finally:
        conn.close()
    print(f"库: {info['db_path']}（schema v{info['schema_version']}）")
    print(f"列表: {info['jobs']} 条｜详情: {info['details']} 条"
          f"（含 JD {info['details_with_jd']}）｜runs: {info['runs']}")
    if info.get("job_versions") or info.get("job_flags"):
        print(f"资产: JD 版本 {info.get('job_versions', 0)}｜标记 "
              f"{info.get('job_flags', 0)}｜状态 {info.get('states') or {}}")
    if info["last_job_update"]:
        print(f"最近更新: {info['last_job_update']}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
