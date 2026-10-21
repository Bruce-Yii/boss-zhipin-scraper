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

import re
import unicodedata

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


def classify_post_type(job):
    """判定发布主体类型：``direct``（企业直招）/ ``agency``（猎头代发）/ ``anonymous``（匿名）。

    依据（按优先级）：
    1. 平台自己标的 ``proxy_job`` / ``proxy_type`` 非空 → ``agency``（**以平台标注为准**，
       不靠猜公司名）
    2. 公司名匿名（"某…"）→ ``anonymous``（代发但公司被隐去）
    3. 其余 → ``direct``
    """
    if not isinstance(job, dict):
        return "direct"
    proxy = str(job.get("proxy_job") or "").strip()
    proxy_type = str(job.get("proxy_type") or "").strip()
    if proxy or proxy_type:
        return "agency"
    company = str(job.get("company_name") or job.get("company") or "").strip()
    if company:
        for pat in _ANON_PATTERNS[:2]:
            if pat.search(company):
                return "anonymous"
        if any(k in company for k in _FRAGILE_TYPES):
            return "anonymous"
    return "direct"


def is_agency_like(job):
    """猎头代发或匿名发布（薪资带虚高、公司不可核，求职端需区别对待）。"""
    return classify_post_type(job) in ("agency", "anonymous")


# ---------------------------------------------------------------------------
# 2) 标题归一化
# ---------------------------------------------------------------------------

def normalize_title(title):
    """标题归一化键：全角转半角、去空白、去标点、小写。

    用于**分组/匹配**，不用于展示（展示保留平台原值）。
    """
    text = unicodedata.normalize("NFKC", str(title or "")).strip().lower()
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[·・\-—_、,，。;；:：!！?？()（）\[\]【】\"'`]+", "", text)
    return text


# ---------------------------------------------------------------------------
# 3) 内容指纹（跨猎头/跨发布的同源识别）
# ---------------------------------------------------------------------------

def _normalize_jd(jd):
    text = unicodedata.normalize("NFKC", str(jd or "")).lower()
    text = re.sub(r"\s+", "", text)
    return text


def content_fingerprint(job, head=180):
    """岗位内容指纹：归一化 JD 开头 + 薪资带 + 城市。

    用途：识别"同一份 JD 被多家猎头/多个发布重复投放"（`job_id` 不同但内容同源）。
    只取 JD 开头（正文前 180 字）——足够区分不同岗位，又不会被尾部差异干扰。
    """
    if not isinstance(job, dict):
        return ""
    jd = _normalize_jd(job.get("jd"))[:head]
    if not jd:
        return ""
    salary = re.sub(r"\s+", "", str(job.get("salary") or ""))
    city = normalize_title(job.get("location"))[:12]
    return f"{jd}|{salary}|{city}"


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
