"""数据验收检查器：按**规格**（data/acceptance_spec.json）判定产物是否可用。

为什么重做（2026-10-22 独立审计结论落到代码）
------------------------------------------------
原版验收器能查的，只有"写它的人那一刻想到的那几件事"。审计构造的 **17 条单缺陷记录
全部放行**——不是漏看一条，是验收器与被验收物出自同一个心智模型，盲区结构性地重复。

四条根因，逐条落成本次改造：

1. **恒假的性质**：P8（"标了 api 但薪资为空"）在模型里恒不成立（模型把
   ``salary_source`` 写死成 ``api if salary else api_empty``）→ 性质写了但永远不会红，
   等于没有。→ **删掉 P8**，改成能红的规则（薪资必须可解析或明确标面议）。
2. **抄了一份的实现**：P9/P10 在本文件里自己写了一份 JD 长度/垃圾尾正则，而
   ``scripts/semantics.py`` 的 ``check_invariants`` 已经有一份 → 两份副本在漂移。
   → P9/P10 **委托** ``semantics.check_invariants``（单一实现），本文件不再持有正则。
3. **与实现同源**：P2 的登记字段表直接取 ``job_model.FIELD_INDEX``、P3 直接复用打标器
   的指纹函数 → 结构上不可能发现实现自身的错误。→ P2 的登记表搬进**规格**（消费者侧），
   P3 的内容指纹在本文件**独立实现**（同定义、异实现）。
4. **warn 不进 ok**："检测到"不等于"判不合格"。→ 新增性质里凡是"矛盾"一律 error，
   只有"信息不足/信号"才 warn，并在规格里逐条标明。

新增三条性质（审计给的判定逻辑）：

- **P11 交付物与请求一致**：meta.keyword 的标题命中率、meta.city 的一致率、
  meta.job_count 与实际条数；关键词不可机器解析时明说"无法核对是不是这次要找的岗"。
- **P12 语义交叉一致性**：派生态（post_type/recruit_type/active_state/title_key/salary）
  必须与平台原始字段自洽；矛盾即 error。
- **P13 身份 + 时效 + 凭据**：job_id 唯一且由链接推出、link_verified_at 可解析且不超 7 天
  （岗位挂多天是常态，超过只做 warn）、递归扫描凭据参数（复用消费端 contract_check 的实现）、
  重复组计数与主条一致。

**产物形态分级**：列表产物（本来就没有 JD）不该被判"内容相同/JD 太短"——
空指纹跳过 P3、没有 JD 的记录跳过 P9/P10；结论里说明用的是哪一档。

用法::

    python scripts/acceptance.py <导出.json> [...]     # 检查一个或多个产物
    from acceptance import check_export                 # 供测试/脚本调用
"""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import os
import re
import unicodedata
from datetime import UTC, datetime, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
SPEC_PATH = os.path.join(_REPO_ROOT, "data", "acceptance_spec.json")

# 消费端契约校验器（vendor 副本）：**凭据扫描的唯一实现**，不再另写一套。
_CONTRACT_CHECK_PATH = os.path.join(
    _REPO_ROOT, "tests", "fixtures", "consumer_validator", "src", "python",
    "contract_check.py",
)

# ---------------------------------------------------------------------------
# 产物形态
# ---------------------------------------------------------------------------
SHAPE_DETAIL = "detail"      # 详情/导出档：有 JD
SHAPE_LIST = "list"          # 列表档：本来就没有 JD

_SHAPE_LABEL = {
    SHAPE_DETAIL: "详情档（含 JD）",
    SHAPE_LIST: "列表档（无 JD）",
}
_SHAPE_NOTE = {
    SHAPE_DETAIL: "内容级性质 P3/P5/P9/P10 全部生效",
    SHAPE_LIST: "内容级性质降级：P3 只比较有 JD 的记录，P5/P9/P10 跳过"
                "（列表产物本来就没有 JD，拿它比内容是假阳性）",
}

# ---------------------------------------------------------------------------
# 判定用词表（判定依据来自"消费者会怎么读"，不是"实现里有哪些字段"）
# ---------------------------------------------------------------------------

# 岗位名里的**通用词**：命中它们不算"JD 与岗位名对得上"。
# 原版判定用 2 字滑窗，「产品」这种通用词就能过关（审计实测 466 条里 351 条
# ＝75% 是这样过关的）——这里把它们全部剔除。
_GENERIC_TITLE_TERMS = frozenset({
    "产品经理", "产品负责人", "产品总监", "产品运营", "产品规划", "产品专家",
    "产品企划", "产品专员", "产品助理", "产品设计", "产品策划", "产品线",
    "产品", "经理", "主管", "总监", "专员", "助理", "顾问", "组长", "负责人",
    "工程师", "开发", "研发", "设计", "运营", "销售", "商务", "人事", "人力",
    "财务", "客服", "策划", "分析", "市场", "品牌", "增长", "采购", "供应链",
    "工作", "岗位", "职位", "职责", "要求", "经验", "能力", "学历", "本科",
    "硕士", "大专", "学历不限", "应届", "在校", "社招", "校招", "实习", "兼职",
    "团队", "公司", "业务", "项目", "客户", "用户", "内容", "相关", "熟悉",
    "优先", "具备", "以上", "其他", "各类", "服务", "管理", "支持", "负责",
    "参与", "完成", "平台", "系统", "工具", "数据", "技术", "方向", "高级",
    "资深", "初级", "中级", "前端", "后端", "全栈", "架构", "测试", "策略",
    "法务", "行政", "bd", "hr", "ui", "ux", "pm",
})

# 平台标注：猎头/代发（平台自己给的，不靠猜）
_ANON_PREFIXES = ("某", "知名", "大型", "头部", "顶级", "互联网大厂", "上市", "一线")
_FRAGILE_TYPES = ("工作室", "个体", "自由职业", "兼职")
_AGENCY_BOSS_TITLES = frozenset({"猎头顾问"})
_FALSY_FLAGS = frozenset({"", "0", "false", "no", "none", "null"})

# HR 活跃度："X 个月前活跃"＝僵尸岗（平台自己的说法）
_ZOMBIE_ACTIVITY_RE = re.compile(r"(?:\d+\s*)?(?:个?月|周|年)\s*前活跃")

# 招聘类型：平台 jobType（4=实习 / 5=校招）
_RECRUIT_TYPE_BY_JOB_TYPE = {"4": "intern", "5": "campus"}
_ALLOWED_JOB_TYPES = frozenset({"", "4", "5"})

# 薪资
_NEGOTIABLE_SALARY = frozenset({"面议", "面谈", "不限", "negotiable", "价格面议"})
_SALARY_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
_SALARY_UNIT_RE = re.compile(r"(?:k|千|万|元|块|薪|天|时|月|年)", re.IGNORECASE)

# 标题归一化用的标点集合（比平台那套更全：只用于"两者是不是同一个标题"的比对）
_PUNCT_CHARS = set(
    "·・-—–_、,，。;；:：!！?？()（）[]【】{}「」『』《》〈〉“”‘’\"'/\\|~`@#$%^&*+=<>"
)

# 平台生成形态的 job_id：16 位小写十六进制（md5 前 16 位）
_JOB_ID_RE = re.compile(r"^[0-9a-f]{16}$")

_SEMANTICS_MODULE = None
_CONTRACT_CHECK_MODULE = None


# ---------------------------------------------------------------------------
# 依赖加载（延迟 + 可降级）
# ---------------------------------------------------------------------------

def _semantics_module():
    """语义层（JD 长度/垃圾尾的**唯一实现**）。"""
    global _SEMANTICS_MODULE
    if _SEMANTICS_MODULE is None:
        module = None
        try:
            from scripts import semantics as module  # noqa: PLC0415
        except ImportError:  # pragma: no cover - 直接运行脚本时走此分支
            try:
                import semantics as module  # noqa: PLC0415
            except ImportError:
                module = None
        _SEMANTICS_MODULE = module or False
    return _SEMANTICS_MODULE or None


def _contract_check_module():
    """消费端契约校验器（凭据扫描的**唯一实现**，vendor 副本）。"""
    global _CONTRACT_CHECK_MODULE
    if _CONTRACT_CHECK_MODULE is None:
        module = None
        if os.path.exists(_CONTRACT_CHECK_PATH):
            spec = importlib.util.spec_from_file_location(
                "boss_consumer_contract_check", _CONTRACT_CHECK_PATH)
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
        _CONTRACT_CHECK_MODULE = module or False
    return _CONTRACT_CHECK_MODULE or None


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def load_spec(path=None):
    """读验收规格。"""
    with open(path or SPEC_PATH, encoding="utf-8") as handle:
        return json.load(handle)


def _records(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("jobs", "details"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def _meta_of(payload):
    """产物的元信息（导出文件里 meta 与 jobs 平级）。"""
    return payload if isinstance(payload, dict) else {}


def _norm(text):
    return unicodedata.normalize("NFKC", str(text or "")).strip().lower()


def _norm_body(text):
    """正文归一化：NFKC → 小写 → 去全部空白。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(text or "")).lower())


def _canon_key(text):
    """标题归一化键（独立实现）：NFKC → 小写 → 去空白 → 去标点。"""
    text = unicodedata.normalize("NFKC", str(text or "")).lower()
    text = re.sub(r"\s+", "", text)
    return "".join(ch for ch in text if ch not in _PUNCT_CHARS)


def _title_terms(title):
    """岗位名里的候选词（**与消费端 job_summary.jd_matches_title 同口径**）。

    判定"JD 与岗位名对得上"时，标题的任一片段（含 2 字滑窗）出现在 JD 里
    就算命中。**不**剔除"通用词"——剔除通用词会把「产品经理」这种标题削成
    一个滑窗造出来的伪词「品经」，导致大量真岗位被误判成 mismatch（实测
    466 条里 70 条假阳性）。消费端的权威口径是"任一标题片段命中"，验收器
    必须与之同口径，否则验收结论与实际过滤结果矛盾。
    """
    text = _norm(title)
    terms = set()
    for chunk in re.split(r"[\s\-—_|/·、,，()（）【】\[\]]+", text):
        chunk = chunk.strip()
        if len(chunk) < 2:
            continue
        terms.add(chunk)
        # 中文长词再切 2 字滑窗
        if re.search(r"[一-鿿]", chunk) and len(chunk) > 3:
            for i in range(len(chunk) - 1):
                terms.add(chunk[i:i + 2])
    # 注意：**不要**在这里剔除"通用词"。
    #
    # 这是 2026-10-22 独立审计给 P5 加的"通用词过滤"导致的回归：把
    # 「产品」「经理」这类词全部剔除后，「产品经理」这种标题只剩一个滑窗
    # 造出来的伪词「品经」，而真实 JD 里根本不会有「品经」——于是 466 条里
    # 70 条**真正对得上**的岗位被误判成"JD 与岗位名对不上"（实测 acceptance
    # 报 72 条、job_summary.jd_matches_title 只报 2 条，两者口径打架）。
    #
    # 消费端（job_summary.jd_matches_title）的定义才是"JD 是否与岗位名对得上"
    # 的权威口径：任一标题片段（含 2 字滑窗）出现在 JD 里就算对得上。验收器
    # 必须与消费端同口径，否则验收结论与实际过滤结果矛盾。通用词**不**剔除：
    # 「产品」出现在 JD 里本身就是岗位相关的证据，剔除它反而把信号削没了。
    return {t for t in terms if len(t) >= 2}


def _content_lens(record, head=180):
    """内容指纹（**独立实现**：与打标器同定义、异实现）。

    定义（写在规格里）：同内容 ＝ 归一化 JD 前 180 字 ＋ 归一化城市前 12 字。
    打标器在 ``semantics.mark_duplicate_groups`` → ``job_model.content_fingerprint``。

    为什么不用打标器那个函数：验收器与被验收物同源时，**结构上不可能发现实现自身
    的错误**（2026-10-22 独立审计结论）。这里各写各的：定义一致，实现漂移会被抓出来。
    """
    jd = _norm_body(record.get("jd"))[:head]
    if not jd:
        return ""
    city = _norm_body(record.get("location"))[:12]
    return f"{jd}|{city}"


def _content_core(record, head=180):
    """重复组的内容核心（归一化 JD 前 180 字）。"""
    return _norm_body(record.get("jd"))[:head]


def _truthy(value):
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in ("1", "true", "yes", "y", "t")


def _as_int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _city_of(location):
    """location（城市·区域·商圈）里的城市。"""
    return str(location or "").split("·")[0].strip()


# ---------------------------------------------------------------------------
# 产物形态
# ---------------------------------------------------------------------------

def detect_product_shape(payload, records=None):
    """产物形态：列表档（本来就没有 JD）还是详情档。

    判定依据按可靠性排序：meta.jd_coverage 存在 → 详情档；记录里出现非空 jd →
    详情档；否则列表档。
    """
    records = records if records is not None else _records(payload)
    meta = _meta_of(payload)
    if isinstance(meta.get("jd_coverage"), dict):
        return SHAPE_DETAIL
    for record in records:
        if isinstance(record, dict) and str(record.get("jd") or "").strip():
            return SHAPE_DETAIL
    return SHAPE_LIST


# ---------------------------------------------------------------------------
# 登记字段（**只来自规格**，不取实现的字段表）
# ---------------------------------------------------------------------------

def _registered_fields(spec):
    """登记字段集合：**只来自规格**（消费者问题 + 规格登记表）。

    2026-10-22 独立审计结论：原实现直接取 ``job_model.FIELD_INDEX``——验收器与被
    验收物同源，模型多一个没人问的字段，验收器照样放行。登记表搬进规格后，
    "模型加了字段但消费者契约没登记"会被抓出来。
    """
    fields = set()
    for name in (spec.get("field_registry") or []):
        fields.add(str(name))
    for question in spec.get("questions") or []:
        fields.update(str(x) for x in (question.get("requires") or []))
    fields.update(str(x) for x in (spec.get("required_always") or []))
    fields.update(str(x) for x in (spec.get("aliases") or {}))
    return fields


def _type_matches(value, declared):
    """取值类型是否符合规格声明。"""
    if declared == "list[str]":
        return isinstance(value, list) and all(isinstance(x, str) for x in value)
    if declared == "str":
        return isinstance(value, str)
    return True


# ---------------------------------------------------------------------------
# 语义交叉推导（**独立于 job_model.classify_* 的判定规则**）
# ---------------------------------------------------------------------------

def _expected_post_type(record):
    """按平台原始字段推 post_type：proxy_job/proxy_type → agency；猎头顾问 → agency；
    匿名公司名 → anonymous；其余 direct。"""
    proxy = str(record.get("proxy_job") or "").strip().lower()
    proxy_type = str(record.get("proxy_type") or "").strip().lower()
    if proxy not in _FALSY_FLAGS or proxy_type not in _FALSY_FLAGS:
        return "agency"
    if str(record.get("boss_title") or "").strip() in _AGENCY_BOSS_TITLES:
        return "agency"
    company = str(record.get("company_name") or record.get("company") or "").strip()
    if company:
        if company.startswith(_ANON_PREFIXES) or any(t in company for t in _FRAGILE_TYPES):
            return "anonymous"
    return "direct"


def _expected_recruit_type(record):
    """按 job_type 推 recruit_type（4=实习 / 5=校招 / 其它=社招）。

    空标记 + 标题自称实习 → 实习（与 job_model.classify_recruit_type
    同口径，issue #159；独立实现，角度交叉验证）。
    """
    raw = str(record.get("job_type") or "").strip()
    if not raw and "实习" in str(record.get("title") or ""):
        return "intern"
    return _RECRUIT_TYPE_BY_JOB_TYPE.get(raw, "social")


def _expected_active_state(record):
    """按 boss_active_status 推 active_state（active/inactive/unknown）。"""
    text = str(record.get("boss_active_status") or "").strip()
    if not text:
        return "unknown"
    if _ZOMBIE_ACTIVITY_RE.search(text):
        return "inactive"
    if text == "在线" or text.endswith("活跃"):
        return "active"
    return "unknown"


def _is_negotiable_salary(text):
    return str(text or "").strip().lower() in _NEGOTIABLE_SALARY


def _salary_parses(text):
    """薪资原文能否解析成区间（面议族单独判）。"""
    value = str(text or "").strip()
    if not value:
        return False
    if _is_negotiable_salary(value):
        return True
    if not _SALARY_NUMBER_RE.search(value):
        return False
    return bool(_SALARY_UNIT_RE.search(value))


def _is_daily_salary(record):
    text = str(record.get("salary") or "")
    if "/天" in text or "元/天" in text:
        return True
    return str(record.get("salary_unit") or "").strip().lower() == "day"


# ---------------------------------------------------------------------------
# 性质实现
# ---------------------------------------------------------------------------

def _finding(fid, level, detail, count=None):
    return {"id": fid, "level": level, "detail": detail, "count": count}


def _check_p1(records, spec, shape):
    """P1 必填字段非空 + 公司信息不得整块为空。

    必填清单**按产物形态分级**：jd 只在详情档必填（列表产物本来就没有 JD）。
    """
    out = []
    required = list(spec.get("required_always") or [])
    if shape == SHAPE_DETAIL:
        required += list(spec.get("required_detail_only") or [])
    empty_counts = {}
    for field in required:
        count = sum(1 for r in records if not str(r.get(field) or "").strip())
        if count:
            empty_counts[field] = count
    if empty_counts:
        out.append(_finding(
            "P1_required_non_empty", "error",
            "必填字段为空：" + "，".join(f"{k}({v} 条)" for k, v in empty_counts.items()),
            sum(empty_counts.values())))
    company_fields = ("company_name", "company_industry", "company_scale",
                      "company_stage", "company_link")
    blank = 0
    for record in records:
        if not any(str(record.get(f) or "").strip() for f in company_fields):
            blank += 1
    if blank:
        out.append(_finding(
            "P1_required_non_empty", "error",
            f"有 {blank} 条记录的公司信息整块为空"
            f"（{'/'.join(company_fields)} 全空）——消费者问「这家公司是谁」得不到答案",
            blank))
    return out


def _check_p2(records, spec):
    """P2 字段必须在规格里登记（warn）+ 取值类型必须符合规格声明（error）。"""
    out = []
    registered = _registered_fields(spec)
    unknown = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        for key in record:
            if key not in registered:
                unknown.add(key)
    if unknown:
        out.append(_finding(
            "P2_no_unregistered_field", "warn",
            "出现规格未登记的字段：" + "，".join(sorted(unknown)[:12]),
            len(unknown)))
    field_types = spec.get("field_types") or {}
    drifted = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        for field, declared in field_types.items():
            if field not in record:
                continue
            value = record.get(field)
            if value is None:
                continue
            if not _type_matches(value, declared):
                drifted.setdefault(field, []).append(record)
    if drifted:
        detail = "；".join(
            f"{field}(期望 {field_types.get(field)}，实际 "
            f"{type(items[0].get(field)).__name__}，{len(items)} 条)"
            for field, items in drifted.items()
        )
        out.append(_finding(
            "P2_type_stable", "error",
            "字段取值类型漂移：" + detail,
            sum(len(items) for items in drifted.values())))
    return out


def _check_p3(records):
    """P3 内容相同必须被标重复（独立实现的内容指纹）。"""
    groups = {}
    for record in records:
        lens = _content_lens(record)
        if not lens:
            continue          # 没有 JD 的记录没有内容指纹（列表产物）——空指纹会把
            # 所有无 JD 记录归成一组 → 假阳性（独立审计 F5）
        groups.setdefault(lens, []).append(record)
    unflagged = 0
    samples = []
    for members in groups.values():
        if len(members) < 2:
            continue
        keys = {str(m.get("dup_group") or "") for m in members}
        if len(keys) != 1 or "" in keys:
            unflagged += len(members)
            if len(samples) < 5:
                samples.append(
                    f"{str(members[0].get('title') or '')[:16]}@"
                    f"{_city_of(members[0].get('location'))}")
    if unflagged:
        out = _finding(
            "P3_duplicates_are_flagged", "error",
            f"有 {unflagged} 条内容相同（归一化 JD 前 180 字 + 城市前 12 字）"
            f"但未归入同一 dup_group", unflagged)
        if samples:
            out["detail"] += "：" + "；".join(samples)
        return [out]
    return []


def _check_p4(records):
    """P4 身份判定：链接与 job_id 一一对应。"""
    link_owner = {}
    id_links = {}
    for record in records:
        link = str(record.get("job_link") or "").strip()
        jid = str(record.get("job_id") or "").strip()
        if link:
            link_owner.setdefault(link, set()).add(jid)
        if jid:
            id_links.setdefault(jid, set()).add(link)
    out = []
    bad_link = sum(len(ids) - 1 for ids in link_owner.values() if len(ids) > 1)
    if bad_link:
        out.append(_finding(
            "P4_link_belongs_to_record", "error",
            f"有 {bad_link} 条记录的 job_link 指向别的 job_id（同一链接属于多个身份）",
            bad_link))
    bad_id = sum(len(links) - 1 for links in id_links.values() if len(links) > 1)
    if bad_id:
        out.append(_finding(
            "P4_link_belongs_to_record", "error",
            f"有 {bad_id} 条 job_id 对应多个 job_link（同一身份挂在多条链接上）",
            bad_id))
    return out


def _check_p5(records):
    """P5（信号，warn）：JD 必须出现岗位名里的词（与 job_summary.jd_matches_title
    同口径：任一标题片段/2 字滑窗命中即算对得上）。"""
    mismatch = []
    for record in records:
        title = str(record.get("title") or "")
        jd = str(record.get("jd") or "")
        terms = _title_terms(title)
        if not terms or not jd:
            continue
        jd_norm = _norm_body(jd)
        if not any(term in jd_norm for term in terms):
            mismatch.append(record)
    if not mismatch:
        return []
    samples = "；".join(str(r.get("title") or "")[:20] for r in mismatch[:5])
    return [_finding(
        "P5_jd_matches_its_title", "warn",
        f"有 {len(mismatch)} 条 JD 与岗位名对不上（标题片段/滑窗均未出现在 JD，"
        f"疑似张冠李戴）：{samples}", len(mismatch))]


def _check_p6(records):
    """P6 link_verified_at 必须标注。"""
    missing = sum(1 for r in records if not str(r.get("link_verified_at") or "").strip())
    if not missing:
        return []
    return [_finding(
        "P6_link_verified", "warn",
        f"有 {missing} 条缺 link_verified_at（链接是短时效凭证）", missing)]


def _check_p7(records, spec):
    """P7 语义判定齐全且取值合法。"""
    out = []
    value_rules = spec.get("value_rules") or {}
    presence = list(spec.get("presence_fields") or [])
    for field in presence:
        missing = sum(1 for r in records if not str(r.get(field) or "").strip())
        if missing:
            out.append(_finding(
                "P7_semantic_labels_present", "warn",
                f"{field} 缺失 {missing} 条", missing))
    for field, allowed in value_rules.items():
        if field in presence:
            continue          # 缺失已在 presence 段报过，别重复报
        missing = sum(1 for r in records if not str(r.get(field) or "").strip())
        if missing:
            out.append(_finding(
                "P7_semantic_labels_present", "warn",
                f"{field} 缺失 {missing} 条", missing))
            continue
        illegal = sum(1 for r in records if str(r.get(field)) not in allowed)
        if illegal:
            out.append(_finding(
                "P7_semantic_labels_present", "error",
                f"{field} 有 {illegal} 条取值不在 {allowed}", illegal))
    return out


def _check_p9_p10(records, spec):
    """P9/P10：委托 semantics.check_invariants（**单一实现**，本文件不持有正则）。"""
    out = []
    semantics = _semantics_module()
    if semantics is None:
        return [_finding(
            "P9_jd_not_too_short", "warn",
            "语义层不可用，JD 长度/垃圾尾检查未执行（唯一实现在 scripts/semantics.py）")]
    jd_records = [r for r in records if str(r.get("jd") or "").strip()]
    if not jd_records:
        return out          # 没有 JD 的记录跳过 P9/P10（列表产物本来就没有 JD）
    invariants = semantics.check_invariants(jd_records)
    length = invariants.get("jd_length") or {}
    if not length.get("ok", True):
        out.append(_finding(
            "P9_jd_not_too_short", "error",
            f"{length.get('detail')}（JD 短于语义层约定的下限，抽取失败不是真 JD）"))
    junk = invariants.get("no_junk_tail") or {}
    if not junk.get("ok", True):
        out.append(_finding(
            "P10_no_junk_tail", "error",
            f"{junk.get('detail')}（判定来自 semantics.check_invariants，单一实现）"))
    # 规格补充的同族标记：语义层那一份没覆盖到的页面模块文案（避免能力倒退）。
    extra = [str(m) for m in (spec.get("junk_tail_extra_markers") or [])]
    if extra:
        hits = [r for r in jd_records
                if any(m in str(r.get("jd") or "") for m in extra)]
        if hits:
            out.append(_finding(
                "P10_no_junk_tail", "error",
                f"{len(hits)} 条 JD 尾部混进页面模块文案"
                f"（规格补充标记：{'、'.join(extra)}）", len(hits)))
    return out


def _check_p11(payload, records, spec):
    """P11 交付物与请求一致：关键词命中率 / 城市一致率 / job_count。"""
    out = []
    meta = _meta_of(payload)
    thresholds = spec.get("thresholds") or {}
    hit_min = float(thresholds.get("keyword_hit_rate_min", 0.6))
    city_min = float(thresholds.get("city_match_rate_min", 0.95))
    total = len(records)

    # 目标词根：优先 meta.target_roots（机器可用的词根列表），退回 meta.keyword。
    # 为什么需要单独字段：关键词是**搜索词**（如 "AI产品经理"），而平台对它是模糊召回
    # → 搜 "AI产品经理" 会回来 "AI出海产品经理"、"AIGC产品运营" 这类**命中目标但标题
    # 不含完整关键词串**的岗位；用完整串匹配会把它们误判成"不是这次要找的岗"。
    # 词根列表让"命中"按词根算，而不是按整串。
    raw_roots = meta.get("target_roots")
    if isinstance(raw_roots, (list, tuple)):
        roots = [str(r).strip() for r in raw_roots if str(r).strip()]
    else:
        keyword = str(meta.get("keyword") or "").strip()
        roots = [r.strip() for r in re.split(r"[,，、;；/]+", keyword) if r.strip()]
    if not roots:
        out.append(_finding(
            "P11_delivery_matches_request", "warn",
            "meta.keyword 缺失或不可机器解析——**无法核对是不是这次要找的岗**"))
    else:
        matched = 0
        unmatched = []
        for record in records:
            title = _norm_body(record.get("title"))
            if any(_norm_body(root) in title for root in roots):
                matched += 1
            else:
                unmatched.append(str(record.get("title") or "")[:24])
        rate = matched / total if total else 0.0
        if rate < hit_min:
            detail = (f"标题与关键词（{'、'.join(roots)}）命中率仅 {rate:.0%}"
                      f"（{matched}/{total}）——这批岗位大多不是这次要找的岗")
            if unmatched:
                shown = "、".join(unmatched[:8])
                detail += f"；未命中标题：{shown}"
                if len(unmatched) > 8:
                    detail += f" 等 {len(unmatched)} 条"
            out.append(_finding(
                "P11_delivery_matches_request", "error", detail, total - matched))

    city = str(meta.get("city") or "").strip()
    if not city:
        out.append(_finding(
            "P11_delivery_matches_request", "warn",
            "meta.city 缺失——无法核对岗位是不是在要找的城市"))
    elif re.fullmatch(r"\d{6,9}", city):
        out.append(_finding(
            "P11_delivery_matches_request", "warn",
            f"meta.city={city} 是城市码，无法与记录 location 比对"))
    else:
        wanted = _norm(city)
        hit = sum(1 for r in records if _norm(_city_of(r.get("location"))) == wanted)
        rate = hit / total if total else 0.0
        if rate < city_min:
            cities = sorted({_city_of(r.get("location")) for r in records
                             if _norm(_city_of(r.get("location"))) != wanted})
            out.append(_finding(
                "P11_delivery_matches_request", "error",
                f"记录城市与请求城市 {city} 的一致率仅 {rate:.0%}（{hit}/{total}）"
                f"；实际城市：{'、'.join(c for c in cities if c)[:120] or '（空）'}",
                total - hit))

    if "job_count" in meta:
        declared = _as_int(meta.get("job_count"))
        if declared is None:
            out.append(_finding(
                "P11_delivery_matches_request", "error",
                f"meta.job_count={meta.get('job_count')!r} 不是数字"))
        elif declared != total:
            out.append(_finding(
                "P11_delivery_matches_request", "error",
                f"meta.job_count={declared} 与实际 {total} 条不符", abs(declared - total)))
    else:
        out.append(_finding(
            "P11_delivery_matches_request", "warn",
            "meta 缺 job_count——无法核对交付条数"))
    return out


def _check_p12(records):
    """P12 语义交叉一致性：派生态必须与平台原始字段自洽。"""
    buckets = {
        "post_type": [], "recruit_type": [], "active_state": [],
        "title_key": [], "salary": [], "job_type": [], "social_daily": [],
    }
    for record in records:
        post_type = str(record.get("post_type") or "").strip()
        if post_type:
            expected = _expected_post_type(record)
            if post_type != expected:
                buckets["post_type"].append(
                    f"{str(record.get('title') or '')[:16]}: post_type={post_type}"
                    f" 但平台字段推出 {expected}"
                    f"（proxy_job={record.get('proxy_job')!r}/"
                    f"proxy_type={record.get('proxy_type')!r}/"
                    f"boss_title={record.get('boss_title')!r}）")

        recruit_type = str(record.get("recruit_type") or "").strip()
        if recruit_type:
            expected = _expected_recruit_type(record)
            if recruit_type != expected:
                buckets["recruit_type"].append(
                    f"{str(record.get('title') or '')[:16]}: recruit_type={recruit_type}"
                    f" 但 job_type={record.get('job_type')!r} 推出 {expected}")
            if recruit_type == "intern" and str(record.get("job_type") or "").strip() != "4" \
                    and not (not str(record.get("job_type") or "").strip()
                             and "实习" in str(record.get("title") or "")):
                buckets["recruit_type"].append(
                    f"{str(record.get('title') or '')[:16]}: 标了实习但 job_type="
                    f"{record.get('job_type')!r}（实习要求 job_type=4，"
                    f"无标记但标题自称实习的除外）")
            if recruit_type == "campus" and str(record.get("job_type") or "").strip() != "5":
                buckets["recruit_type"].append(
                    f"{str(record.get('title') or '')[:16]}: 标了校招但 job_type="
                    f"{record.get('job_type')!r}（校招必须 job_type=5）")

        active_state = str(record.get("active_state") or "").strip()
        if active_state:
            expected = _expected_active_state(record)
            if active_state != expected:
                buckets["active_state"].append(
                    f"{str(record.get('title') or '')[:16]}: active_state={active_state}"
                    f" 但 boss_active_status={record.get('boss_active_status')!r}"
                    f" 推出 {expected}")
            valid = str(record.get("job_valid_status") or "").strip()
            if active_state == "active" and valid not in ("", "1"):
                buckets["active_state"].append(
                    f"{str(record.get('title') or '')[:16]}: 标了活跃但"
                    f" job_valid_status={valid}（非 1 的岗位不算活着）")

        title = str(record.get("title") or "").strip()
        title_key = str(record.get("title_key") or "").strip()
        if title and title_key and _canon_key(title) != _canon_key(title_key):
            buckets["title_key"].append(
                f"{title[:16]}: title_key={title_key!r} 与岗位名对不上")

        salary = str(record.get("salary") or "").strip()
        if salary:
            if not _salary_parses(salary):
                buckets["salary"].append(
                    f"{str(record.get('title') or '')[:16]}: 薪资 {salary!r}"
                    f" 无法解析，也不是明确的面议")
            elif _is_negotiable_salary(salary):
                for field in ("salary_min", "salary_max", "salary_unit",
                              "months_per_year"):
                    if str(record.get(field) or "").strip():
                        buckets["salary"].append(
                            f"{str(record.get('title') or '')[:16]}: 薪资面议"
                            f"却有结构化列 {field}={record.get(field)!r}")
        source = str(record.get("salary_source") or "").strip()
        if source:
            if source == "api" and not salary:
                buckets["salary"].append(
                    f"{str(record.get('title') or '')[:16]}: salary_source=api 但薪资为空")
            if source == "api_empty" and salary:
                buckets["salary"].append(
                    f"{str(record.get('title') or '')[:16]}: salary_source=api_empty"
                    f" 但薪资非空（{salary!r}）")

        job_type = str(record.get("job_type") or "").strip()
        if job_type not in _ALLOWED_JOB_TYPES:
            buckets["job_type"].append(
                f"{str(record.get('title') or '')[:16]}: job_type={job_type!r}"
                f" 不是合法取值（只能是 ''/4/5）")

        if recruit_type == "social" and _is_daily_salary(record):
            buckets["social_daily"].append(
                f"{str(record.get('title') or '')[:16]}: 标了社招却用日薪口径"
                f"（{record.get('salary')!r}）")

    out = []
    labels = {
        "post_type": "post_type 与平台原始字段矛盾",
        "recruit_type": "recruit_type 与 job_type 矛盾",
        "active_state": "active_state 与平台活跃/有效性字段矛盾",
        "title_key": "title_key 与岗位名矛盾",
        "salary": "薪资口径矛盾（无法解析 / 面议与结构化列打架 / 来源自相矛盾）",
        "job_type": "job_type 取值非法",
        "social_daily": "社招用了日薪口径",
    }
    for key, items in buckets.items():
        if not items:
            continue
        detail = f"{labels[key]}：{len(items)} 条"
        detail += "；" + "；".join(items[:5])
        out.append(_finding("P12_semantic_cross_consistency", "error", detail,
                             len(items)))
    return out


def _check_p13(payload, records, spec):
    """P13 身份 + 时效 + 凭据 + 重复组一致性。"""
    out = []
    thresholds = spec.get("thresholds") or {}
    max_age = timedelta(hours=float(thresholds.get("link_max_age_hours", 24)))
    now = datetime.now(UTC)

    # ── 身份：job_id 唯一 ────────────────────────────────────────────
    seen = {}
    for record in records:
        jid = str(record.get("job_id") or "").strip()
        if jid:
            seen.setdefault(jid, []).append(record)
    duplicated = {jid: len(items) for jid, items in seen.items() if len(items) > 1}
    if duplicated:
        out.append(_finding(
            "P13_identity_freshness_credentials", "error",
            "有重复 job_id（同一身份出现多次）："
            + "、".join(f"{jid}×{n}" for jid, n in list(duplicated.items())[:8]),
            sum(n - 1 for n in duplicated.values())))

    # ── 身份：job_id 必须由自己的 job_link 推出（模型里的真身定义）────
    bad_identity = []
    for record in records:
        jid = str(record.get("job_id") or "").strip()
        link = str(record.get("job_link") or "").strip()
        if not jid or not link:
            continue
        if not _JOB_ID_RE.match(jid):
            continue      # 非平台生成形态的 id 无从用 md5 定义核对，跳过（不误判）
        expect = hashlib.md5(link.encode()).hexdigest()[:16]
        if jid != expect:
            bad_identity.append(f"{jid}≠md5(job_link)[:16]={expect}")
    if bad_identity:
        out.append(_finding(
            "P13_identity_freshness_credentials", "error",
            f"有 {len(bad_identity)} 条记录的 job_id 不是由自己的 job_link 推出的"
            f"（模型真身定义 job_id=md5(job_link)[:16]）："
            + "；".join(bad_identity[:5]), len(bad_identity)))

    # ── 时效：link_verified_at ────────────────────────────────────────
    garbage = []
    stale = []
    naive = []
    for record in records:
        stamp = str(record.get("link_verified_at") or "").strip()
        if not stamp:
            continue
        try:
            moment = datetime.fromisoformat(stamp)
        except ValueError:
            garbage.append(stamp)
            continue
        if moment.tzinfo is None:
            naive.append(stamp)
            continue
        if now - moment > max_age:
            stale.append(stamp)
    if garbage:
        out.append(_finding(
            "P13_identity_freshness_credentials", "error",
            f"有 {len(garbage)} 条 link_verified_at 不是合法 ISO 时间"
            f"（例：{'、'.join(garbage[:3])}）", len(garbage)))
    if stale:
        out.append(_finding(
            "P13_identity_freshness_credentials", "warn",
            f"有 {len(stale)} 条 link_verified_at 超过 "
            f"{thresholds.get('link_max_age_hours', 168)} 小时（7 天）"
            f"——岗位挂多天是常态，仅提示可择机刷新：{'、'.join(stale[:3])}",
            len(stale)))
    if naive:
        out.append(_finding(
            "P13_identity_freshness_credentials", "warn",
            f"有 {len(naive)} 条 link_verified_at 没有时区——无法核对时效"
            f"（生产端写的是本地时间，建议改带时区的 ISO 时间戳）", len(naive)))

    # ── 凭据：递归扫所有字符串值（复用消费端 contract_check 的实现）──
    module = _contract_check_module()
    if module is None:
        out.append(_finding(
            "P13_identity_freshness_credentials", "warn",
            "找不到消费端 contract_check，凭据扫描未执行"
            "（唯一实现：tests/fixtures/consumer_validator/src/python/contract_check.py）"))
    else:
        hits = set(module._structured_sensitive_keys(payload))
        if hits:
            out.append(_finding(
                "P13_identity_freshness_credentials", "error",
                "检出敏感字段或 URL 参数：" + "、".join(sorted(hits)[:10]),
                len(hits)))

    # ── 重复组：双向一致 + 计数 + 主条 ───────────────────────────────
    groups = {}
    for record in records:
        gid = str(record.get("dup_group") or "").strip()
        if gid:
            groups.setdefault(gid, []).append(record)
    bad_size = []
    bad_primary = []
    mixed_content = []
    for gid, members in groups.items():
        size = len(members)
        for member in members:
            declared = _as_int(member.get("dup_size"))
            if declared is not None and declared != size:
                bad_size.append(f"{gid}: dup_size={declared} 但组内实际 {size} 条")
        primaries = [m for m in members if _truthy(m.get("is_primary"))]
        if len(primaries) != 1:
            bad_primary.append(f"{gid}: is_primary 的记录有 {len(primaries)} 条（应恰好 1 条）")
        if len({_content_core(m) for m in members}) > 1:
            mixed_content.append(f"{gid}: 组内记录的 JD 内容不一致")
    if bad_size:
        out.append(_finding(
            "P13_identity_freshness_credentials", "error",
            "重复组的 dup_size 与组内实际条数不符：" + "；".join(bad_size[:5]),
            len(bad_size)))
    if bad_primary:
        out.append(_finding(
            "P13_identity_freshness_credentials", "error",
            "重复组的主条标记不对：" + "；".join(bad_primary[:5]), len(bad_primary)))
    if mixed_content:
        out.append(_finding(
            "P13_identity_freshness_credentials", "error",
            "被标为重复的记录内容其实不同：" + "；".join(mixed_content[:5]),
            len(mixed_content)))
    loose = []
    for record in records:
        if str(record.get("dup_group") or "").strip():
            continue
        declared = _as_int(record.get("dup_size"))
        if declared not in (None, 1):
            loose.append(f"dup_size={declared} 但未标重复组")
        if record.get("is_primary") is False:
            loose.append("未标重复组却标了非主条")
    if loose:
        out.append(_finding(
            "P13_identity_freshness_credentials", "error",
            "重复组标记自相矛盾：" + "；".join(loose[:5]), len(loose)))
    return out


def _check_forbidden(records, spec):
    """禁列（红线）：平台内部字段不得出现在对外产物里。"""
    forbidden = set(spec.get("forbidden_fields") or [])
    leaked = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        for key in record:
            if key in forbidden:
                leaked.add(key)
    if not leaked:
        return []
    return [_finding(
        "P0_forbidden_fields", "error",
        "产物里出现平台内部字段（红线）：" + "、".join(sorted(leaked)), len(leaked))]


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------

def check_export(payload, spec=None, sample_title_check=True):
    """按规格检查一个产物（列表 / 详情记录 / 导出皆可）。

    Returns:
        ``(ok, findings)``；findings 是 ``[{"id","level","detail","count"}]``。
        level: ``error``（产物不可用）/ ``warn``（信号，需人判断）/ ``info``（形态说明）。
    """
    spec = spec or load_spec()
    records = _records(payload)
    findings = []

    if not records:
        findings.append(_finding("P0_empty", "error", "产物里没有记录"))
        return False, findings

    shape = detect_product_shape(payload, records)
    findings.append(_finding(
        "artifact_shape", "info",
        f"产物形态：{_SHAPE_LABEL[shape]}；{_SHAPE_NOTE[shape]}"))

    findings.extend(_check_p1(records, spec, shape))
    findings.extend(_check_p2(records, spec))
    findings.extend(_check_p3(records))
    findings.extend(_check_p4(records))
    if sample_title_check:
        findings.extend(_check_p5(records))
    findings.extend(_check_p6(records))
    findings.extend(_check_p7(records, spec))
    findings.extend(_check_p9_p10(records, spec))
    findings.extend(_check_p11(payload, records, spec))
    findings.extend(_check_p12(records))
    findings.extend(_check_p13(payload, records, spec))
    findings.extend(_check_forbidden(records, spec))

    ok = not any(f["level"] == "error" for f in findings)
    return ok, findings


# ---------------------------------------------------------------------------
# CSV 随附文件验收（issue #171，链路地图「还没立住」#6）
# ---------------------------------------------------------------------------

# 列表档 CSV 必需列（消费者按 job_id 对账、按这些列使用）
CSV_REQUIRED_COLUMNS = ("job_id", "title", "salary", "company_name",
                        "location", "job_link")
# 详情档 CSV 必需列
DETAIL_CSV_REQUIRED_COLUMNS = ("job_id", "title", "jd", "job_link")


def csv_companion_path(payload_path):
    """产物 json 的随附 CSV 路径（同目录同名 .csv）；非 .json 路径返回 None。"""
    if not str(payload_path).lower().endswith(".json"):
        return None
    return str(payload_path)[:-5] + ".csv"


def check_csv_companion(payload_path, payload=None):
    """随附 CSV 验收：行数/job_id 与 JSON 对账 + 必需列齐全 + 可解析。

    CSV 不存在 → 返回 []（CSV 是可选产物，缺失不算缺陷）；存在则任何
    对不上都是 error（CSV 与 JSON 口径不一致 = 交付物自相矛盾）。
    """
    csv_path = csv_companion_path(payload_path)
    if not csv_path or not os.path.exists(csv_path):
        return []
    if payload is None:
        with open(payload_path, encoding="utf-8") as handle:
            payload = json.load(handle)
    if isinstance(payload, dict):
        records = payload.get("jobs") or []
        required = CSV_REQUIRED_COLUMNS
    elif isinstance(payload, list):
        records = payload
        required = DETAIL_CSV_REQUIRED_COLUMNS
    else:
        return []
    records = [r for r in records if isinstance(r, dict)]
    try:
        with open(csv_path, encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            header = list(reader.fieldnames or [])
            rows = list(reader)
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        return [_finding("csv_unreadable", "error",
                         f"随附 CSV 无法解析（utf-8 BOM）：{exc}")]
    findings = []
    missing_cols = [c for c in required if c not in header]
    if missing_cols:
        findings.append(_finding(
            "csv_missing_columns", "error",
            f"随附 CSV 缺必需列：{', '.join(missing_cols)}"))
    json_ids = [str(r.get("job_id") or "") for r in records]
    csv_ids = [str((r or {}).get("job_id") or "").strip() for r in rows]
    if len(rows) != len(records):
        findings.append(_finding(
            "csv_row_count", "error",
            f"随附 CSV 行数 {len(rows)} ≠ JSON 岗位数 {len(records)}"))
    dupes = sorted({i for i in csv_ids if i and csv_ids.count(i) > 1})
    if dupes:
        findings.append(_finding("csv_duplicate_ids", "error",
                                 f"随附 CSV job_id 重复：{', '.join(dupes[:5])}"))
    only_csv = sorted(set(csv_ids) - set(json_ids) - {""})
    if only_csv:
        findings.append(_finding(
            "csv_extra_rows", "error",
            f"随附 CSV 有 JSON 里没有的岗位 {len(only_csv)} 条"
            f"（如 {', '.join(only_csv[:3])}）"))
    only_json = sorted(set(json_ids) - set(csv_ids))
    if only_json:
        findings.append(_finding(
            "csv_missing_rows", "error",
            f"随附 CSV 缺了 JSON 里的岗位 {len(only_json)} 条"
            f"（如 {', '.join(only_json[:3])}）"))
    empty_ids = sum(1 for i in csv_ids if not i)
    if empty_ids:
        findings.append(_finding("csv_empty_job_id", "error",
                                 f"随附 CSV 有 {empty_ids} 行 job_id 为空"))
    return findings


def format_findings(findings, limit=20):
    """把 findings 渲染成人看的几行。"""
    problems = [f for f in findings if f.get("level") in ("error", "warn")]
    lines = []
    for finding in findings:
        if finding.get("level") != "info":
            continue
        lines.append(f"ℹ️ {finding['id']}: {finding['detail']}")
    for finding in problems[:limit]:
        mark = "❌" if finding["level"] == "error" else "⚠️"
        lines.append(f"{mark} {finding['id']}: {finding['detail']}")
    if len(problems) > limit:
        lines.append(f"… 另有 {len(problems) - limit} 项")
    if not problems:
        lines.append("✅ 全部通过")
    return "\n".join(lines)


def main(argv=None):
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__)
        return 2
    spec = load_spec()
    all_ok = True
    for path in argv:
        try:
            with open(path, encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError) as exc:
            print(f"❌ {os.path.basename(path)}: 读取失败 {exc}")
            all_ok = False
            continue
        ok, findings = check_export(payload, spec)
        records = _records(payload)
        shape = detect_product_shape(payload, records)
        print(f"\n=== {os.path.basename(path)}（{len(records)} 条 · "
              f"{_SHAPE_LABEL[shape]}）===")
        print(format_findings(findings))
        all_ok = all_ok and ok
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
