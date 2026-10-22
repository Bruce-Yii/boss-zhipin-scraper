#!/usr/bin/env python3
"""岗位语义层：发布主体类型、内容指纹去重、标题归一化、供给编排、导出不变量。

为什么需要这一层（2026-10-22 逐条读 294 条导出后定位）：

1. **发布主体类型被丢弃**。平台在列表侧用 `proxy_job` / `proxy_type` 标注了"这条岗是
   猎头/代发还是企业直招"，我们把字段搬进导出却**从不语义化使用**：于是猎头代发岗
   （公司名匿名、薪资区间虚高——实测 25 条代理岗里 11 条薪资上限 ≥50K，而企业直招岗
   **0 条**达到 50K）混在结果里，求职端无法分辨。同一个根因还造成"同一份 JD 被多家
   猎头重复发布"（1 组 ×2）与"同公司同岗重复发布"。

2. **标题被当成标识**。平台原样回传，列表侧与详情侧大小写/空格不一致
   （AI产品经理 67 / ai产品经理 55 / AI 产品经理 6），按字符串分组/匹配会漏。

3. **链接的时效性被高估了**。BOSS 的 `job_detail` URL 含 `securityId` 段，只用
   `job_id` 拼 URL 会 404（`job_id` 只是 securityId 的前 16 位）→ **链接不能由 job_id
   重建**。但链接本身**是稳的**（2026-10-22 实测：昨天存的链接今天用真导航打开仍是同一
   岗位，3/3 内容指纹一致；今天新拉的 683 条列表里链接字符串与昨天完全相同，0 条需要刷新）。
   真正会"取到别的岗位"的是**用 fetch(XHR) 取页面**——平台会间歇性返回诱饵内容（见
   HTML 直取通道的封存说明）；真导航（document 导航）不受影响。

4. **供给与额度之间没有协调**。平台单个「关键词×城市」组合的搜索深度上限约 10 页
   （实测第 11 页起内容重复），单组合最多约 240-300 个岗位；日额度 700 装不下，
   而工具一次运行只吃一个关键词 → "额度没花完但没岗可抓"是结构性的。

5. **导出没有不变量自检**。上面这些问题都要靠"人肉读数据"才能发现，工具自己不报。

本模块是**纯逻辑、无 CDP 依赖、无网络**，按项目模块约定单独成文件。
"""

import hashlib
import re
import unicodedata
from collections import Counter

try:
    from scripts import job_model as _model
except ImportError:  # pragma: no cover - 直接运行脚本时走此分支
    import job_model as _model

# ---------------------------------------------------------------------------
# 1) 发布主体类型
# ---------------------------------------------------------------------------

# 猎头/代发岗常见的匿名公司名写法（"某"字头、模糊描述）
_ANON_PATTERNS = (
    re.compile(r"^某"),
    re.compile(r"^(知名|大型|头部|顶级|互联网大厂|上市|一线)"),
    re.compile(r"(公司|集团|科技|网络|信息|技术)?$"),
)

# 明显是"个人/工作室"形态的发布主体（不一定是代发，但同样不可信）
_FRAGILE_TYPES = ("工作室", "个体", "自由职业", "兼职")


# 记录级语义的**唯一定义点在 scripts/job_model.py**（岗位数据模型）。
# 这里只保留集合级逻辑（指纹、重复分组、供给编排、导出不变量）。
classify_post_type = _model.classify_post_type
classify_recruit_type = _model.classify_recruit_type
is_agency_like = _model.is_agency_like


# ---------------------------------------------------------------------------
# 2) 标题归一化
# ---------------------------------------------------------------------------

normalize_title = _model.normalize_title


# ---------------------------------------------------------------------------
# 3) 内容指纹（跨猎头/跨发布的同源识别）
# ---------------------------------------------------------------------------

def _normalize_jd(jd):
    text = unicodedata.normalize("NFKC", str(jd or "")).lower()
    text = re.sub(r"\s+", "", text)
    return text


def content_fingerprint(job, head=180):
    """岗位内容指纹（**转发到岗位模型——唯一定义**）。

    用途：识别"同一份 JD 被多家猎头/多个发布重复投放"（`job_id` 不同但内容同源）。
    只取 JD 开头（正文前 180 字）——足够区分不同岗位，又不会被尾部差异干扰。

    2026-10-22：指纹定义迁到 models（验收器与打标器必须同源，否则会出现
    "检查器说重复、打标器说不重复"——实测 4 组真重复因此没被标）。
    """
    return _model.content_fingerprint(job, head=head)


def group_duplicates(jobs):
    """按内容指纹分组，返回 ``(fingerprint_by_id, group_sizes)``。

    ``group_sizes[fp] > 1`` 表示该组是重复投放（保留组内第一条，其余应标记）。
    """
    fingerprint_by_id = {}
    sizes = {}
    for job in jobs or []:
        if not isinstance(job, dict):
            continue
        jid = job.get("job_id")
        if not jid:
            continue
        fp = content_fingerprint(job)
        if not fp:
            continue
        fingerprint_by_id[jid] = fp
        sizes[fp] = sizes.get(fp, 0) + 1
    return fingerprint_by_id, sizes


# ---------------------------------------------------------------------------
# 4) 供给编排（详情额度 ↔ 列表供给）
# ---------------------------------------------------------------------------

# 平台单个「关键词×城市」组合的可翻深度上限约 10 页，且第 11 页起内容重复
# （2026-10-22 实测：请求 20 页 → "第 11 页与之前页内容重复（翻页失效）" → 239 条）。
COMBO_CAPACITY = 260


def supply_plan(remaining_budget, combo_capacity=COMBO_CAPACITY, max_combos=12):
    """按剩余额度算出需要几个「关键词×城市」组合才装得下。

    返回 ``(needed_combos, expected_supply)``；``needed_combos`` 至少为 1（只要还有额度）。
    """
    remaining = max(int(remaining_budget or 0), 0)
    if remaining <= 0:
        return 0, 0
    capacity = max(int(combo_capacity or COMBO_CAPACITY), 1)
    needed = max(1, -(-remaining // capacity))  # 向上取整
    needed = min(needed, max(int(max_combos or 1), 1))
    return needed, needed * capacity


def supply_gap(remaining_budget, available_jobs, combo_capacity=COMBO_CAPACITY):
    """供给缺口：还差多少个岗位才能把今日额度花完。

    返回 ``(gap, need_combos)``；``gap <= 0`` 表示供给充足。
    """
    remaining = max(int(remaining_budget or 0), 0)
    available = max(int(available_jobs or 0), 0)
    gap = remaining - available
    if gap <= 0:
        return 0, 0
    capacity = max(int(combo_capacity or COMBO_CAPACITY), 1)
    return gap, max(1, -(-gap // capacity))


# ---------------------------------------------------------------------------
# 5) 导出不变量自检
# ---------------------------------------------------------------------------

def check_invariants(jobs, meta=None):
    """导出前的数据完整性自检，返回 ``{name: {"ok": bool, "detail": str}}``。

    这些不变量对应 2026-10-22 逐条读数据时人工发现的问题——把它们变成机器检查，
    就不用再靠人眼发现：

    - ``jd_present``      每条记录都有 JD
    - ``jd_length``       JD 长度达标（≥120 字，非占位）
    - ``no_junk_tail``    JD 尾部没有「薪资详情 / 刚刚活跃 / 公司·招聘专员」等页面模块
    - ``post_type``       每条记录都有发布主体类型标注
    - ``dup_ratio``       重复投放占比不过高
    - ``field_coverage``  关键字段（title/company/salary/location/link）无空值
    - ``link_age``        链接是"抓取时刻有效"的临时入口（必须有 link_verified_at）
    """
    jobs = [j for j in (jobs or []) if isinstance(j, dict)]
    total = len(jobs)
    result = {}

    def add(name, ok, detail):
        result[name] = {"ok": bool(ok), "detail": detail}

    if total == 0:
        add("jd_present", True, "无记录")
        return result

    with_jd = sum(1 for j in jobs if str(j.get("jd") or "").strip())
    add("jd_present", with_jd == total, f"{with_jd}/{total} 条有 JD")

    lens = [len(str(j.get("jd") or "")) for j in jobs]
    short = sum(1 for n in lens if n < 120)
    add("jd_length", short == 0, f"{short} 条 < 120 字（中位 {sorted(lens)[total // 2]}）")

    junk = re.compile(r"薪资详情|社保类型|提成方式|刚刚活跃|今日活跃|本周活跃|·招聘专员")
    dirty = sum(1 for j in jobs if junk.search(str(j.get("jd") or "")))
    add("no_junk_tail", dirty == 0, f"{dirty} 条 JD 尾部混进页面模块")

    typed = sum(1 for j in jobs if str(j.get("post_type") or "").strip())
    add("post_type", typed == total, f"{typed}/{total} 条有发布主体类型标注")

    fingerprint_by_id, sizes = group_duplicates(jobs)
    dup_ids = sum(1 for fp, n in sizes.items() if n > 1 for _ in range(n - 1))
    dup_ratio = dup_ids / total if total else 0
    add("dup_ratio", dup_ratio <= 0.2, f"重复投放 {dup_ids}/{total}（{dup_ratio:.1%}）")

    for field in ("title", "company_name", "salary", "location", "job_link"):
        empty = sum(1 for j in jobs if not str(j.get(field) or "").strip())
        add(f"field_{field}", empty == 0, f"{empty}/{total} 条为空")

    stamped = sum(1 for j in jobs if str(j.get("link_verified_at") or "").strip())
    add("link_age", stamped == total,
        f"{stamped}/{total} 条标注了 link_verified_at（平台 URL 是短时效凭证）")

    return result


def invariant_failures(invariants):
    """把自检结果里不通过的项摘出来（给日志/告警用）。"""
    return [f"{name}: {info.get('detail')}" for name, info in (invariants or {}).items()
            if not info.get("ok")]

# ---------------------------------------------------------------------------
# 6) 失败指纹（坏页归类）+ 活跃度三态 + 重复组标记
# ---------------------------------------------------------------------------

# 详情抽取失败的指纹分类。根因不是"数据缺失"，而是"这类页面的解析路径不适用"，
# 因此要按指纹归类沉淀（而不是每天重试同样失败的页面、白花额度）。
_EXTRACT_FAILURE_PATTERNS = (
    ("login_wall", ("登录查看", "登录后可见", "请先登录", "扫码登录")),
    ("risk_page", ("访问受限", "暂时被限制访问", "账户存在异常行为", "验证码")),
    ("shell_only", ("BOSS正在加载中", "正在加载", "javascript is required")),
    ("no_section", ("职位描述",)),          # 有标题但取不到正文块 → 选择器/结构不匹配
    ("too_short", ("too short", "字，", "职位描述")),   # 抽到了但过短
)


def filter_by_relevance(jobs, recruit_types=None, industry_allow=None,
                         industry_block=None, only_relevant_title=False):
    """**零成本的相关度过滤**（在列表阶段做，丢掉的不进详情 → 也不耗额度）。

    - ``recruit_types``: 只保留这些招聘类型（social/intern/campus）；
    - ``industry_allow``: 只保留这些行业（**默认不过滤**；给了才生效）；
    - ``industry_block``: 丢掉这些行业（用户显式指定的"明显无关行业"）；
    - ``only_relevant_title``: 只保留标题是产品经理/AI 方向的岗位。

    返回 ``(kept, dropped_detail)``：dropped_detail 含每类丢了多少 + **丢掉的清单**
    （不静默丢——用户要能核对丢的是什么、为什么丢）。
    """
    allow_set = {str(x).strip() for x in (industry_allow or []) if str(x).strip()}
    block_set = {str(x).strip() for x in (industry_block or []) if str(x).strip()}
    type_set = {str(x).strip() for x in (recruit_types or []) if str(x).strip()}
    kept = []
    dropped = {"recruit_type": 0, "industry_block": 0, "industry_allow": 0,
               "title_relevance": 0, "no_industry": 0, "items": []}
    for job in jobs or []:
        if not isinstance(job, dict):
            continue
        reason = ""
        if type_set:
            rtype = _model.classify_recruit_type(job)
            if rtype not in type_set:
                reason = f"招聘类型={rtype}（不在要的类型里）"
                dropped["recruit_type"] += 1
        if not reason and block_set:
            industry = _model.industry_of(job)
            if industry and industry in block_set:
                reason = f"行业={industry}（在排除名单里）"
                dropped["industry_block"] += 1
        if not reason and allow_set:
            industry = _model.industry_of(job)
            if not industry:
                dropped["no_industry"] += 1
            elif industry not in allow_set:
                reason = f"行业={industry}（不在要的行业里）"
                dropped["industry_allow"] += 1
        if not reason and only_relevant_title and not _model.is_relevant_title(job):
            reason = f"标题={str(job.get('title') or '')[:16]}（不是产品/AI 方向）"
            dropped["title_relevance"] += 1
        if reason:
            dropped["items"].append({
                "title": str(job.get("title") or ""),
                "company": str(job.get("company_name") or ""),
                "industry": _model.industry_of(job),
                "reason": reason,
            })
            continue
        kept.append(job)
    return kept, dropped


def list_stage_audit(jobs, keywords=None, job_expectation=""):
    """**列表层体检**（在抓详情之前做——问题要在便宜的一层暴露）。

    用户 2026-10-22 的原话："当时在列表的时候，你就应该反映到这些问题，
    而不是现在才发现"。列表层 10 个请求 vs 详情层 700 个请求，且关键词失配、
    方向跑偏、平台"求职期望"污染在这一层全都看得见。

    返回一个 dict：方向分布 / 关键词命中率 / 行业分布 / 招聘类型分布 /
    期望设置 / 风险提示列表。
    """
    jobs = jobs or []
    total = len(jobs)
    if not total:
        return {"total": 0, "direction_counts": {}, "keyword_hit_rate": None,
                "industry_top": [], "recruit_type_counts": {}, "job_expectation": "",
                "warnings": []}

    directions = Counter()
    industries = Counter()
    recruit_types = Counter()
    off_samples = []
    for job in jobs:
        is_ai_pm, label, _evidence = _model.classify_direction(job)
        directions[label] += 1
        industry = _model.industry_of(job) or "(平台未提供)"
        industries[industry] += 1
        rtype = _model.classify_recruit_type(job)
        recruit_types[rtype] += 1
        if label not in ("ai_pm",) and len(off_samples) < 8:
            off_samples.append(f"{str(job.get('title') or '')[:16]}"
                               f"｜{str(job.get('company_name') or '')[:12]}")

    keywords = [k for k in (keywords or []) if str(k).strip()]
    hit_rate = None
    hits = 0
    if keywords:
        for job in jobs:
            title = str(job.get("title") or "").lower()
            if any(str(k).strip().lower() in title for k in keywords):
                hits += 1
        hit_rate = hits / total

    warnings = []
    if hit_rate is not None and hit_rate < 0.6:
        warnings.append(
            f"标题与关键词命中率仅 {hit_rate:.0%}（{hits}/{total}）——"
            f"这批岗位大多不是关键词要找的方向（平台模糊召回 + 求职期望影响）")
    non_ai_pm = total - directions.get("ai_pm", 0)
    if total and non_ai_pm / total > 0.4:
        warnings.append(
            f"只有 {directions.get('ai_pm', 0)}/{total} 条是 AI 方向产品岗，"
            f"其余 {non_ai_pm} 条是别的方向（产品/非产品）——"
            f"若目标是 AI 产品经理，应在抓详情前按方向过滤")
    if job_expectation and any(word in str(job_expectation)
                                for word in ("房地产", "中介", "置业", "房产")):
        warnings.append(
            f"平台「求职期望」当前是「{job_expectation}」——平台会据此混入该行业岗位，"
            f"建议清空或改成目标行业后再抓（设置在 BOSS 直聘 网页端）")
    return {
        "total": total,
        "direction_counts": dict(directions),
        "keyword_hit_rate": hit_rate,
        "keyword_hits": hits,
        "industry_top": industries.most_common(10),
        "recruit_type_counts": dict(recruit_types),
        "job_expectation": str(job_expectation or ""),
        "off_direction_samples": off_samples,
        "warnings": warnings,
    }


def classify_extract_failure(message, page_text=""):
    """把抽取失败归成**失败指纹**，用于沉淀"已知坏页"。

    返回形如 ``too_short`` / ``no_section`` / ``shell_only`` / ``login_wall`` /
    ``risk_page`` / ``unknown`` 的字符串。
    """
    text = f"{message or ''}\n{page_text or ''}"
    if "too short" in text or "职位描述" not in text and "no_section" not in text:
        if "too short" in text:
            return "too_short"
    if "职位描述" in text and "too short" not in text:
        # 有"职位描述"却抽不出正文 → 结构/选择器不匹配
        return "no_section"
    for name, markers in _EXTRACT_FAILURE_PATTERNS:
        for marker in markers:
            if marker and marker in text:
                return name
    if "too short" in text:
        return "too_short"
    return "unknown"


active_state = _model.active_state


def stable_group_id(fingerprint):
    """重复组的**稳定** id（同一份内容永远得到同一个组号）。

    为什么不能用内置 ``hash()``（2026-10-22 审计）：``hash(str)`` 的结果依赖
    ``PYTHONHASHSEED``（每个进程随机）→ 同一份数据三个进程跑出
    ``dup-10961898`` / ``dup-96218808`` / ``dup-52751366``。后果：跨天/跨 run
    合并时组号对不上，同一份输入两次跑出的文件**字节不同**（不可复现）。

    改用 sha256 摘要前 8 位十六进制：跨进程、跨机器、跨 Python 版本都稳定
    （sha256 而非 md5：md5 已被 ruff B324 视为不安全哈希，且这里要的是稳定性
    而不是密码学强度，但没必要用被标记的算法）。
    """
    text = str(fingerprint or "").encode("utf-8")
    return hashlib.sha256(text).hexdigest()[:8]


def mark_duplicate_groups(jobs):
    """就地给每条记录打重复组标记：``dup_group`` / ``dup_size`` / ``is_primary``。

    根因：平台允许同一岗位**多条发布**（同公司同岗、不同 job_id），job_id 去重抓不到，
    只能靠内容指纹识别。标记出来（而不是删掉——猎头岗有时是唯一渠道），让消费端
    能一眼看出重复。

    组号用**稳定哈希**（见 ``stable_group_id``）→ 同一份内容跨进程/跨 run 永远
    得到同一个组号（可复现、可跨天合并）。
    """
    fingerprint_by_id, sizes = group_duplicates(jobs)
    groups = {}
    for job in jobs or []:
        if not isinstance(job, dict):
            continue
        jid = job.get("job_id")
        fp = fingerprint_by_id.get(jid)
        if not fp:
            job["dup_group"] = ""
            job["dup_size"] = 1
            job["is_primary"] = True
            continue
        if fp not in groups:
            groups[fp] = []
        groups[fp].append(job)
    for fp, members in groups.items():
        if len(members) < 2:
            for job in members:
                job["dup_group"] = ""
                job["dup_size"] = 1
                job["is_primary"] = True
            continue
        group_id = f"dup-{stable_group_id(fp)}"
        for index, job in enumerate(members):
            job["dup_group"] = group_id
            job["dup_size"] = len(members)
            job["is_primary"] = index == 0
    return groups


def title_group_counts(jobs):
    """按归一化标题键统计（识别"同一岗位的多种写法"）。

    平台原样回传标题 → `AI产品经理` 67 / `ai产品经理` 55 / `AI 产品经理` 6，
    按字符串分组会漏；归一化键把它们并成一组。
    """
    counts = {}
    for job in jobs or []:
        if not isinstance(job, dict):
            continue
        key = job.get("title_key") or normalize_title(job.get("title"))
        if not key:
            continue
        counts[key] = counts.get(key, 0) + 1
    return counts

