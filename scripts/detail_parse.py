# -*- coding: utf-8 -*-
"""详情解析纯逻辑（从 boss_cdp_raw.py 增量拆分）。

含：列表条目映射（map_api_job/map_api_jobs）、详情异常体系、
JD 噪声剥离/招聘者卡片截断/技能标签清理、详情 API 响应解析、
extract_detail_fields / extract_job_description、build_detail_record。

约束：全部运行时全局经 _core() 动态取自主模块（tests 对 boss_cdp_raw
的直接调用与 patch 保持有效）；`re` 仅用于模块级常量预编译，
函数体内的运行时引用一律走 _core()。
"""

import logging
import re as _re_for_consts

log = logging.getLogger("boss_cdp")


def _core():
    import sys as _sys
    # 1) 调用栈优先：编排链路经某 boss 副本进入时，栈上必有它的帧 → 用那一份。
    #    （多测试文件以不同名字加载 boss_cdp_raw.py 时 sys.modules 里有多个
    #    副本，candidates[-1] 会命中别人的模块，导致调用方的直接赋值/patch 失效。）
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
    # 2) 直接调用（tests 直接调重导出函数，栈上无 boss 帧）：
    #    取最近一次 exec 的 boss 副本（序号由 boss_cdp_raw 底部登记）。
    _candidates = [m for m in _sys.modules.values()
                   if getattr(m, "__file__", None)
                   and str(m.__file__).endswith("boss_cdp_raw.py")]
    if _candidates:
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
    # 3) 兜底：按路径加载 boss（子模块被独立 import 时）。
    try:
        from . import boss_cdp_raw as _m
        return _m
    except Exception:
        if "boss_cdp_raw" in _sys.modules:
            return _sys.modules["boss_cdp_raw"]
        import boss_cdp_raw as _m
        return _m


DETAIL_LOGIN_MARKER = "登录查看完整内容"

DETAIL_DESCRIPTION_MARKER = "职位描述"
DETAIL_COMPETITIVENESS_MARKER = "竞争力分析"
DETAIL_SAFETY_MARKER = "BOSS 安全提示"
MIN_DETAIL_TEXT_LENGTH = 50  # 用户 2026-10-24 拍板：120 误伤短 JD，从 120 降到 50

DETAIL_API_PATH = "/wapi/zpgeek/job/detail.json"


def map_api_job(raw):
    """joblist.json 原始条目 → 统一 job dict（**由岗位模型产生**）。

    字段与 ``FETCH_API_JS_TEMPLATE`` 一一对应（注入 JS 模板的 Python 等价版），
    保证被动捕获与 XHR 两条通道产出**同形状**的 job，下游契约/security_map 不受影响。

    字段的含义、来源与消费方在 ``scripts/job_model.py`` 里声明（唯一真相来源）。
    """
    if not isinstance(raw, dict):
        return None
    return _core()._job_model.JobRecord.from_list_payload(raw).to_dict()


def map_api_jobs(data):
    """从捕获的 joblist 响应 JSON 提取映射后的职位列表。"""
    if not isinstance(data, dict):
        return []
    job_list = (data.get("zpData") or {}).get("jobList")
    if not isinstance(job_list, list):
        return []
    return [j for j in (map_api_job(item) for item in job_list) if j]


class DetailExtractionError(ValueError):
    """详情提取失败（页面结构漂移/内容过短），不进 pending。"""


class DetailLoginRequiredError(DetailExtractionError):
    """The detail page is truncated because the BOSS session is not logged in."""


class DetailRiskError(DetailExtractionError):
    """详情 API 返回风控码（code!=0）。

    category 用于**二分处置**（2026-09-22 同行研究）：
      token_expired  → 会话/令牌过期：刷新会话（轮换 tab）后重试一次
      env_risk / account_risk / security_block → 换 tab 无用：停手 + 冷却
    """
    def __init__(self, message, category="env_risk"):
        super().__init__(message)
        self.category = category


def _normalize_detail_whitespace(text):
    core = _core()
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").splitlines()]
    normalized = "\n".join(lines).strip()
    normalized = core.re.sub(r"\n{3,}", "\n\n", normalized)
    return core.re.sub(r"[ \t]{2,}", " ", normalized)


# JD 噪声行：详情页 UI 文案混入正文（举报/扫码分享/去APP 等），逐行剥离。
# 仅当**整行**由噪声 token 组成才删（保守），避免误删正文里出现的同名片段。
_JD_NOISE_TOKENS = frozenset((
    "举报", "微信扫码分享", "去APP", "去 APP", "小程序", "分享",
    "收藏", "立即沟通", "投诉", "下载APP", "下载 APP",
))

# 页面模块行（薪资详情 / 社保类型 / 活跃标签 / 招聘职位 …）的判定口径
# **只有一处**：scripts/semantics.py 的 check_invariants（验收器
# scripts/acceptance.py 已改为委托它）。本模块**不再抄第二份标记表**——
# 2026-10-22 独立审计实测两份副本已经漂移（验收器多认 6 个标记，实现侧一个不认）。
_JD_PAGE_MODULE_MAX_CHARS = 24


def _semantic_junk_hit(text):
    """语义层认为这段文字是「页面模块垃圾」吗（问唯一实现，不自己判）。"""
    snippet = str(text or "")
    if not snippet:
        return False
    invariants = _core()._semantics.check_invariants([{"jd": snippet}])
    info = invariants.get("no_junk_tail") or {}
    return not info.get("ok", True)


def _is_jd_page_module_line(line):
    """这一行是详情页的**页面模块**（而不是招聘方写的正文）吗？

    两个条件缺一不可：
    1. 语义层（唯一实现）判定它含页面模块标记；
    2. 标记落在**行首**且整行很短——正文里顺口提到「提成方式」这种句子
       不能被当成页面模块删掉（子串命中 ≠ 页面模块）。
    """
    text = str(line or "").strip()
    if not text or len(text) > _JD_PAGE_MODULE_MAX_CHARS:
        return False
    if not _semantic_junk_hit(text):
        return False
    if len(text) > 6 and not _semantic_junk_hit(text[:6]):
        return False
    return True


def _is_jd_noise_line(line):
    """整行仅由 UI 噪声 token 组成（空白 / · / | 分隔）→ True。"""
    core = _core()
    parts = [p for p in core.re.split(r"[\s·|/]+", line.strip()) if p]
    return bool(parts) and all(p in _JD_NOISE_TOKENS for p in parts)


def strip_jd_noise(text):
    """逐行剥离 JD 噪声行（详情页 UI 文案 + 页面模块文案）；空行交由归一化处理。"""
    lines = str(text or "").replace("\r\n", "\n").split("\n")
    return "\n".join(line for line in lines
                     if not _is_jd_noise_line(line)
                     and not _is_jd_page_module_line(line))


def _detail_encrypt_job_id(job):
    """详情 API 的 jobId 参数：encrypt_job_id 优先，缺省从 job_link 提取
    （列表 job_link 自带 encryptJobId，与详情 DOM 导航同源）。"""
    core = _core()
    job_id = str(job.get("encrypt_job_id") or "")
    if not job_id:
        m = core.re.search(r"/job_detail/([^./]+)\.html", str(job.get("job_link") or ""))
        if m:
            job_id = m.group(1)
    return job_id


def build_detail_api_url(job, security_id, city_code="", id_mode="security"):
    """构造详情 API URL。

    id_mode="security"：jobId+securityId+city（我方实证需带）；
    id_mode="encrypt"：仅 jobId——encryptJobId 长期有效，boss-agent-cli 低风险
    通道，不依赖 securityId 一次性有效期（--input 老文件无 sidecar 也可走 API）。
    """
    job_id = _detail_encrypt_job_id(job)
    if id_mode == "encrypt":
        params = {"jobId": job_id}
    else:
        params = {
            "jobId": job_id,
            "securityId": str(security_id or ""),
            "city": str(city_code or ""),
        }
    return f"{DETAIL_API_PATH}?{_core().urlencode(params)}"


def _parse_detail_api_value(val, job):
    """解析详情 API 返回（DETAIL_API_JS 输出）。

    Returns:
        dict: {jd, boss_active_status, page_update_date(''), address, ...}——失败抛异常：
        error → DetailExtractionError；code!=0 → DetailRiskError；jd 过短 → DetailExtractionError。
    """
    core = _core()
    try:
        payload = core.json.loads(val)
    except (core.json.JSONDecodeError, TypeError, ValueError) as exc:
        raise DetailExtractionError(f"详情 API 返回不是有效 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise DetailExtractionError("详情 API 返回结构异常")
    if payload.get("error"):
        raise DetailExtractionError(f"详情 API 请求失败: {payload['error']}")
    if payload.get("code", 0) != 0:
        _code = payload.get("code")
        _msg = str(payload.get("msg", ""))
        _cat, _detail = core.classify_boss_code(_code, _msg)
        raise DetailRiskError(
            f"详情 API 风控码: code={_code} {_msg}（{_detail}）", category=_cat)
    jd = str(payload.get("jd") or "")
    jd = jd.replace("\u3000", " ")  # 全角空格清理（探测实证 postDescription 含全角空格）
    jd = _normalize_detail_whitespace(jd)
    if len(jd) < MIN_DETAIL_TEXT_LENGTH:
        raise DetailExtractionError(
            f"job description too short after validation: {len(jd)} < {MIN_DETAIL_TEXT_LENGTH}"
        )
    return {
        "jd": jd,
        "boss_active_status": str(payload.get("boss_active_status") or ""),
        # 公司级活跃时间（详情 API 同一次响应内的 brandComInfo.activeTime；零额外请求）
        "brand_active_time": str(payload.get("brand_active_time") or ""),
        "page_update_date": "",  # API 通道无详情页"页面更新时间"（可选字段，退化点已记录）
        "job_status_desc": str(payload.get("job_status_desc") or ""),
        "brand_introduce": str(payload.get("brand_introduce") or ""),
        "brand_stage_name": str(payload.get("brand_stage_name") or ""),
        "brand_scale_name": str(payload.get("brand_scale_name") or ""),
        "brand_industry_name": str(payload.get("brand_industry_name") or ""),
    }


def _looks_like_navigation_page(text):
    return (
        DETAIL_DESCRIPTION_MARKER not in text
        and "无障碍专区" in text
        and "首页" in text
        and "职位" in text
        and "公司" in text
    )


def _is_boss_activity_line(text):
    """True for recruiter activity labels like「在线」「今日活跃」."""
    return text == "在线" or text.endswith("活跃")


# HR 活跃度全系文案（专题 #8，zhipin-zhuaqu ACT_RE；不含裸"在线"以免页面误命中）
_HR_ACTIVE_RE = _re_for_consts.compile(
    r"(刚刚活跃|今日活跃|\d+日内活跃|本周活跃|\d+周内活跃|"
    r"\d+个月内活跃|\d+月内活跃|半年前活跃)")


def _recruiter_footer_info(lines):
    """Locate recruiter card footer and optional activity status.

    Returns ``(footer_start, boss_active_status)``. ``footer_start`` is the
    line index where the recruiter card begins (to truncate JD), or ``None``.
    ``boss_active_status`` is e.g. ``今日活跃`` / ``在线``, or ``""``.
    """
    stripped_lines = [line.strip() for line in lines]
    end = len(stripped_lines)
    while end and not stripped_lines[end - 1]:
        end -= 1

    def card_info(card_end):
        while card_end and not stripped_lines[card_end - 1]:
            card_end -= 1
        if card_end < 4 or stripped_lines[card_end - 2] != "·":
            return None, ""
        activity_or_name = stripped_lines[card_end - 4]
        has_activity_line = _is_boss_activity_line(activity_or_name)
        if has_activity_line:
            start = card_end - 5
            status = activity_or_name
        else:
            start = card_end - 4
            status = ""
        if start < 0:
            return None, ""
        return start, status

    for marker in (DETAIL_COMPETITIVENESS_MARKER, DETAIL_SAFETY_MARKER):
        try:
            marker_index = stripped_lines.index(marker)
        except ValueError:
            continue
        start, status = card_info(marker_index)
        if start is not None:
            return start, status

    # 结构特征没命中 → 按**内容特征**再找一次（卡片常在「BOSS 安全提示」之前；
    # 尾巴上还有页面模块时，不能因为「最后一行不是卡片」就放弃）。
    limit = end
    try:
        limit = min(limit, stripped_lines.index(DETAIL_SAFETY_MARKER))
    except ValueError:
        pass
    start, status = _recruiter_card_tail(lines, limit)
    if start is not None:
        return start, status

    return card_info(end)


# 招聘者卡片的**内容特征**（2026-10-22 审计补）。
# 卡片是「姓名 → 活跃度 → 公司(·职位)」顺序的尾块；新版页面把它压成一行，
# 或者只剩姓名行——旧判据（倒数第 2 行是孤立的「·」）对这些形态全部失配。
_RECRUITER_NAME_RE = _re_for_consts.compile(
    r"^[一-鿿]{1,3}(?:先生|女士)(?:[（(][^）)]{0,6}[）)])?$")
_RECRUITER_POSITION_RE = _re_for_consts.compile(r"^[^·\n]{2,30}·[^·\n]{1,12}$")
_RECRUITER_ACTIVITY_WORDS = ("已离职", "离职中", "在职", "可联系", "暂不可联系")


def _is_recruiter_activity_line(text):
    """招聘者活跃行：「在线」「今日活跃」「离职中」…"""
    line = str(text or "").strip()
    if not line:
        return False
    if _is_boss_activity_line(line):
        return True
    return line in _RECRUITER_ACTIVITY_WORDS


def _is_recruiter_company_line(text):
    """卡片里的公司行：短、不带句读；「公司·职位」也算。"""
    line = str(text or "").strip()
    if not (2 <= len(line) <= 30):
        return False
    if _RECRUITER_POSITION_RE.match(line):
        return True
    return not any(ch in line for ch in "。！？；，、:：!?,;")


def _blank_line_before(lines, index):
    """该行前面是否隔着一个空行（独立成块的信号）。"""
    if index <= 0:
        return False
    return not lines[index - 1].strip()


def _recruiter_card_tail(lines, limit=None):
    """按**内容特征**识别 JD 尾部的招聘者卡片。

    返回 ``(起始行号, 活跃度文案)``；没有卡片返回 ``(None, "")``。

    判据是**组合特征**而不是关键词黑名单——正文里真的会出现「HR」「在线」
    字样，所以只有「姓名 + 活跃度 + 公司」这种结构才算招聘者卡片。
    """
    region = lines[:limit] if limit is not None else lines
    filled = [i for i, line in enumerate(region) if line.strip()]
    if not filled:
        return None, ""

    def text_at(index):
        return region[index].strip()

    # 三行形态：姓名 + 活跃度 + 公司（公司·职位 或 纯公司名）
    if len(filled) >= 3:
        name, activity, company = (text_at(i) for i in filled[-3:])
        if (_RECRUITER_NAME_RE.match(name)
                and _is_recruiter_activity_line(activity)
                and _is_recruiter_company_line(company)):
            return filled[-3], activity
    # 两行形态：姓名 + 公司·职位（职位明确，不会误伤正文）
    if len(filled) >= 2:
        name, second = text_at(filled[-2]), text_at(filled[-1])
        if _RECRUITER_NAME_RE.match(name) and _RECRUITER_POSITION_RE.match(second):
            return filled[-2], ""
    # 单行形态：只剩姓名——要求它前面隔着一个空行（独立成块，不是正文句子）
    if _RECRUITER_NAME_RE.match(text_at(filled[-1])):
        if _blank_line_before(region, filled[-1]):
            return filled[-1], ""
    return None, ""


def _recruiter_footer_start(lines):
    start, _status = _recruiter_footer_info(lines)
    return start


def _cut_leading_description_marker(lines):
    """切掉正文**首行**的「职位描述」小标题（含紧随其后的分隔符）。

    只有标记位于**首行**时才切。正文中段再出现「职位描述」是招聘方自己写的
    小标题（如「【职位描述】」或「职位描述：」），切了会把标记之前的正文整段
    丢掉——2026-10-22 独立审计实测 10 条 JD 头部被截（最严重 81a080c6c9a9187e
    交付 400 字 / 线上 619 字，丢了开头 219 字 / 35%），交付的 JD 以孤立的
    「：」或「】」开头。
    """
    core = _core()
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        if DETAIL_DESCRIPTION_MARKER not in line:
            return lines
        _, _, tail = line.partition(DETAIL_DESCRIPTION_MARKER)
        tail = core.re.sub(r"^[\s:：\]】]+", "", tail)
        return ([tail] if tail else []) + lines[index + 1:]
    return lines


def _is_skill_tag_line(line, tag_set):
    """这一行是不是平台技能标签（整行都由标签拼成）。"""
    parts = [part for part in str(line or "").split() if part]
    if not parts:
        return False
    return all(part in tag_set for part in parts)


def _strip_leading_skill_tags(lines, tags):
    """去掉 JD 开头粘着的**平台技能标签**行。

    页面容器兜底取正文时，技能标签（.job-keyword-list）和正文在同一块里 →
    标签行粘在 JD 最前面（2026-10-22 审计实测 4 条：ab241aa3a8fc1f8f /
    45da13a91f687539 / 0cf93290fe03340d / 6bcff8742cf1d857）。判定靠**内容**：
    该行与详情侧抓到的技能标签完全一致 → 是平台标签，不是招聘方写的正文。
    """
    tag_set = {str(tag).strip() for tag in (tags or []) if str(tag).strip()}
    if not tag_set:
        return lines
    out = list(lines)
    while out:
        index = next((i for i, line in enumerate(out) if line.strip()), None)
        if index is None:
            break
        if not _is_skill_tag_line(out[index], tag_set):
            break
        del out[index]
    return out


def extract_detail_fields(extracted, min_length=MIN_DETAIL_TEXT_LENGTH):
    """Return validated JD and boss activity status as separate fields.

    ``jd`` never includes the recruiter card or activity label.
    ``boss_active_status`` is extracted from that card when present.

    ``page_text`` is diagnostic input only. It is never persisted unless it has
    an explicit job-description section that passes all checks.
    """
    core = _core()
    if not isinstance(extracted, dict):
        raise DetailExtractionError("detail extractor returned non-dict")

    raw_jd = str(extracted.get("jd") or "")
    page_text = str(extracted.get("page_text") or "")
    diagnostic_text = "\n".join((raw_jd, page_text))

    if DETAIL_LOGIN_MARKER in diagnostic_text:
        raise DetailLoginRequiredError(
            "detail page is truncated at the login wall; refresh the BOSS login session"
        )
    if _looks_like_navigation_page(diagnostic_text):
        raise DetailExtractionError("detail page rendered navigation chrome without a JD")

    text = raw_jd
    from_page_text = False
    if not text and DETAIL_DESCRIPTION_MARKER in page_text:
        # 页面正文兜底：标记之前全是页面框架（岗位名/导航/公司卡）→ 在标记处切
        text = page_text.split(DETAIL_DESCRIPTION_MARKER, 1)[1]
        from_page_text = True

    lines = text.replace("\r\n", "\n").split("\n")
    # 1) 先剥 UI 文案行——否则「职位描述」前面隔着噪声行时判不出它在首行
    lines = [line for line in lines if not _is_jd_noise_line(line)]
    # 2) 「职位描述」小标题：只在**首行**切（正文中段出现的是招聘方自己写的）
    if not from_page_text:
        lines = _cut_leading_description_marker(lines)
    # 3) 平台技能标签行（容器兜底取正文时粘在 JD 最前面）
    lines = _strip_leading_skill_tags(lines, extracted.get("tags"))
    # 4) 尾部：招聘者卡片（结构 + 内容两种特征）/ BOSS 安全提示
    footer_start, boss_active_status = _recruiter_footer_info(lines)
    if footer_start is not None:
        lines = lines[:footer_start]
    else:
        for index, line in enumerate(lines):
            if line.strip() == DETAIL_SAFETY_MARKER:
                lines = lines[:index]
                break
    # 5) 页面模块行（语义层唯一判定的垃圾尾标记）
    lines = [line for line in lines if not _is_jd_page_module_line(line)]

    jd = _normalize_detail_whitespace("\n".join(lines))
    if len(jd) < min_length:
        raise DetailExtractionError(
            f"job description too short after validation: {len(jd)} < {min_length}"
        )
    # 页面更新时间：BOSS 详情页唯一岗位侧日期（招聘方最后编辑岗位时间，
    # 非发布日期——平台不公开发布时间）；列表 API 无任何时间字段。
    # 非契约可选字段，缺失留空；用于区分"岗位侧更新时间"与"我方抓取时间"。
    update_m = core.re.search(r"页面更新时间[：:]\s*(\d{4}-\d{2}-\d{2})", page_text)
    page_update_date = update_m.group(1) if update_m else ""
    # HR 活跃度 DOM 兜底：recruiter 卡未取到活跃行时，用 .boss-active-time
    hr_active_time = str(extracted.get("hr_active_time") or "").strip()
    if not boss_active_status and _is_boss_activity_line(hr_active_time):
        boss_active_status = hr_active_time
    # 再兜底：页面全文按 ACT_RE 全系文案匹配（专题 #8；不含裸"在线"防误命中）
    if not boss_active_status:
        act_m = _HR_ACTIVE_RE.search(page_text)
        if act_m:
            boss_active_status = act_m.group(1)
    # 注：DOM 的 `div.info-publis>p`（相对发布时间）已实测失效——新 SPA 无此节点
    # （2026-09-23 实机复核）；故 publish_time 字段已移除，岗位侧日期仅保留 page_update_date。
    return {"jd": jd, "boss_active_status": boss_active_status,
            "page_update_date": page_update_date}


def extract_job_description(extracted, min_length=MIN_DETAIL_TEXT_LENGTH):
    """Return validated JD text without BOSS page chrome."""
    return extract_detail_fields(extracted, min_length=min_length)["jd"]


def build_detail_record(job, extracted):
    """列表 job + 详情抽取 → 详情记录。

    ``extracted`` 里的 ``_raw_text``（清洗前的页面原文）是**详情侧保真核对的依据**：
    它只进 raw 落盘文件，导出前会被脱敏层剥掉（见 export_contract 的内部字段规则）。
    """
    """详情记录 = **岗位模型记录**（列表侧 + 详情侧合一）。

    2026-10-22 修：此前详情记录是**另一套手写 schema**（只 23 个字段，缺 company_name
    等 16 个列表侧字段），导致
    - `--detail-output` 的记录不满足导出契约（契约要求 company_name，详情只有 company）；
    - CSV 是从详情记录写的 → 46 列里 **32 列整列全空**（用户拿到的表三分之二是空的）；
    - 详情输出与导出两套字段集（双轨）。

    现在由模型统一产生：列表侧 + 详情侧 + 派生语义 + 内部字段剔除（红线），
    详情输出与导出**同一套 schema**。
    """
    core = _core()
    boss_active_status = core.resolve_boss_active_status(
        list_status=job.get("boss_active_status", ""),
        detail_status=extracted.get("boss_active_status", ""),
    )
    payload = dict(job)
    payload["boss_active_status"] = boss_active_status
    # 详情 API 通道的可选字段（2026-08-14；DOM 路径缺省空，兼容）
    detail_payload = dict(extracted or {})
    for key in ("job_status_desc", "brand_introduce", "brand_stage_name",
                "brand_scale_name", "brand_industry_name", "brand_active_time"):
        detail_payload.setdefault(key, "")

    record = core._job_model.JobRecord.from_detail_payload(
        payload, detail_payload,
        link_verified_at=core.datetime.now().astimezone().isoformat(timespec="seconds"))
    row = record.to_public_dict()
    # 历史别名（保持详情输出的向后兼容；规范名仍是 model 的 company_name/tags/job_link）
    row["company"] = row.get("company_name", "")
    row["tags_list"] = row.get("tags", "")
    row["link"] = row.get("job_link", "")
    return row
