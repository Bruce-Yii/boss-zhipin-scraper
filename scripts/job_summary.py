#!/usr/bin/env python3
"""Summarize scraped BOSS jobs and generate a lightweight job-market prompt."""

import argparse
import contextlib
import glob
import io
import json
import os
import re
import unicodedata

try:
    from scripts import job_model as _model
except ImportError:  # pragma: no cover - 直接运行脚本时走此分支
    import job_model as _model
import sys
from collections import Counter

import matplotlib

matplotlib.use("Agg")  # 无头后端：仅生成 PNG 不弹窗
import matplotlib.pyplot as plt
import pandas as pd

try:
    from scripts import boss_cdp_raw as boss
except ImportError:
    import boss_cdp_raw as boss

# matplotlib 中文字体（Windows 微软雅黑/黑体；CI 无中文字体时退化为警告不失败）
plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False


DEFAULT_RESULT_DIR = boss.DEFAULT_RESULT_DIR


def find_latest_jobs_file(result_dir=DEFAULT_RESULT_DIR):
    pattern = os.path.join(os.path.expanduser(result_dir), "boss_jobs_*.json")
    files = [path for path in glob.glob(pattern) if os.path.isfile(path)]
    if not files:
        return None
    return max(files, key=lambda path: (os.path.getmtime(path), path))


def load_jobs_file(path):
    path = os.path.abspath(os.path.expanduser(path))
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)

    if isinstance(payload, dict):
        jobs = payload.get("jobs", [])
        metadata = {
            "keyword": payload.get("keyword", ""),
            "city": payload.get("city", ""),
            "source": path,
        }
        return jobs if isinstance(jobs, list) else [], metadata

    if isinstance(payload, list):
        return payload, {"keyword": "", "city": "", "source": path}

    return [], {"keyword": "", "city": "", "source": path}


def split_tags(raw):
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    return [
        part.strip()
        for part in str(raw).replace("｜", "|").split("|")
        if part.strip()
    ]


def is_experience_tag(tag):
    normalized = str(tag or "").strip()
    return (
        "年" in normalized
        or "应届" in normalized
        or "在校" in normalized
        or normalized in {"经验不限", "不限经验"}
    )


def company_label(job):
    """Return the canonical company label, preferring the export contract field."""
    if not isinstance(job, dict):
        return ""
    for key in ("company_name", "boss_name", "company"):
        value = str(job.get(key) or "").strip()
        if value:
            return value
    return ""


def is_degree_tag(tag):
    return tag in {"初中及以下", "中专/中技", "高中", "大专", "本科", "硕士", "博士", "学历不限"}


def experience_tag_for_job(job):
    """Return the first authoritative experience label for a job.

    The normalized export has an explicit ``experience`` field, while older
    files and list-only callers may only carry it in ``tags``/``job_labels``.
    """
    if not isinstance(job, dict):
        return ""
    for field in ("experience", "tags", "job_labels"):
        for tag in split_tags(job.get(field, "")):
            if is_experience_tag(tag):
                return tag
    return ""


def degree_tag_for_job(job):
    """Return the first authoritative degree label for a job.

    优先读 ``education`` 专用字段（旧文件/仅列表的调用方才回退到 tags/job_labels）——
    同一信息只留一个来源，避免两处口径打架。
    """
    if not isinstance(job, dict):
        return ""
    for field in ("education", "tags", "job_labels"):
        for tag in split_tags(job.get(field, "")):
            if is_degree_tag(tag):
                return tag
    return ""


def district_from_location(location):
    parts = [part.strip() for part in str(location or "").split("·") if part.strip()]
    if len(parts) >= 2:
        return parts[1]
    return parts[0] if parts else ""


def jd_matches_title(job, head=160):
    """JD 是否与岗位名对得上（粗但有效）。

    平台搜索是模糊召回，会带回别的岗位：实测 466 条里有 2 条 JD 与岗位名完全对不上
    ——一条是"招募小红书运营合伙人（股权/项目合作）"的**引流帖**、一条是古茗茶饮的
    **工厂信息化产品经理**（跨行业）。这类 JD 对求职毫无价值。
    """
    if not isinstance(job, dict):
        return True
    title = str(job.get("title") or "")
    jd = str(job.get("jd") or "")
    if not title or not jd:
        return True
    text = unicodedata.normalize("NFKC", jd).lower()
    for chunk in re.split(r"[\s\-—_|/·、,，()（）【】\[\]]+",
                          unicodedata.normalize("NFKC", title).strip().lower()):
        chunk = chunk.strip()
        if len(chunk) < 2:
            continue
        if chunk in text:
            return True
        if re.search(r"[\u4e00-\u9fff]", chunk) and len(chunk) > 3:
            for i in range(len(chunk) - 1):
                if chunk[i:i + 2] in text:
                    return True
    return False


def clean_skill_tag(tag):
    tag = str(tag or "").strip()
    if not tag:
        return ""
    if is_experience_tag(tag) or is_degree_tag(tag):
        return ""
    noise = {"BOSS直聘", "boss", "BOSS", "来自BOSS直聘"}
    return "" if tag in noise else tag


def _most_common(counter, top):
    return counter.most_common(max(top, 1))


_SALARY_K_RE = re.compile(r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*K", re.IGNORECASE)
_SALARY_DAY_RE = re.compile(r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*元/天")


def parse_salary_monthly(salary):
    """把薪资字符串解析为月薪范围（千元）；无法解析返回 None。

    支持 "20-40K" / "20-40K·15薪"（K 后缀月薪）与 "350-500元/天"
    （按 22 个工作日折算月薪千元）。

    **解析口径由岗位模型统一提供**（scripts/job_model.py）——同一份薪资文本在导出、
    摘要、自检里必须得到同一个数。
    """
    return _model.parse_salary_monthly(salary)


_SALARY_MONTHS_RE = re.compile(r"(\d+)\s*薪")


def parse_salary_annual(salary):
    """把薪资字符串解析为年薪范围（千元）；无法解析返回 None。

    "20-40K·15薪" → (300, 600)（月薪×月数）；无月数信息按 12 个月折算。
    15薪 vs 13薪按月度排名不公平，年薪折算供跨岗位公平比较。
    """
    monthly = parse_salary_monthly(salary)
    if monthly is None:
        return None
    low, high = monthly
    m = _SALARY_MONTHS_RE.search(str(salary or ""))
    months = int(m.group(1)) if m else 12
    return round(low * months, 1), round(high * months, 1)


def salary_stats(jobs, post_types=None):
    """汇总岗位薪资行情：中位/均值/P10-P90 区间（月薪千元）与解析覆盖率。

    口径（第四轮调研修正）：区间用 P10/P90 分位替代 min/max（极值在
    小样本不稳定）；中位数取两中位均值（偶数样本）。

    `post_types`（2026-10-22 新增）：只统计指定发布主体类型。**猎头代发岗的薪资带
    是"面议区间"不是真实区间**（实测 25 条代发岗里 11 条上限 ≥50K，而直招岗 13/243），
    混在一起算会把行情均值/虚高岗位数一起带偏（实测全量均值 28.2K vs 直招 26.1K，
    ≥50K 的岗位 24 条 vs 13 条）→ 求职端该看的是**直招口径**。

    Returns:
        dict: {"parsed", "unparsed", "median_k", "mean_k", "low_k", "high_k"}；
            无任何可解析样本时 median/mean/low/high 为 None
    """
    mids = []
    lows = []
    highs = []
    unparsed = 0
    for job in jobs:
        if not isinstance(job, dict):
            continue
        if post_types is not None and _post_type_of(job) not in post_types:
            continue
        parsed = parse_salary_monthly(job.get("salary"))
        if parsed is None:
            unparsed += 1
            continue
        low, high = parsed
        mids.append((low + high) / 2)
        lows.append(low)
        highs.append(high)

    if not mids:
        return {"parsed": 0, "unparsed": unparsed,
                "median_k": None, "mean_k": None, "low_k": None, "high_k": None}
    series = pd.Series(mids)
    low_series = pd.Series(lows)
    high_series = pd.Series(highs)
    return {
        "parsed": len(mids),
        "unparsed": unparsed,
        "median_k": float(series.median()),
        "mean_k": round(float(series.mean()), 1),
        "low_k": round(float(low_series.quantile(0.1)), 1),
        "high_k": round(float(high_series.quantile(0.9)), 1),
    }


def _recruit_type_counts(jobs):
    """招聘类型分布（social/intern/campus）→ 让人知道结果里混了多少非社招岗。"""
    counts = {}
    for job in jobs or []:
        if not isinstance(job, dict):
            continue
        key = _recruit_type_of(job)
        counts[key] = counts.get(key, 0) + 1
    return counts


def _recruit_type_of(job):
    """招聘类型（social/intern/campus）；判定由岗位模型统一提供。"""
    return _model.classify_recruit_type(job)


def _direction_counts(jobs):
    counts = Counter()
    for job in jobs or []:
        if not isinstance(job, dict):
            continue
        counts[_direction_of(job) or "other"] += 1
    return dict(counts)


def _direction_of(job):
    """方向标签（转发到岗位模型）：ai_pm / pm_only / ai_other / other。"""
    return str(job.get("direction") or "") or _model.classify_direction(job)[1]


def _post_type_of(job):
    """发布主体类型（direct/agency/anonymous）；判定由岗位模型统一提供。"""
    return _model.classify_post_type(job)


def experience_bucket(tag):
    """把经验要求标签归一为档位（用于薪资×经验交叉统计）。"""
    tag = str(tag or "").strip()
    if "应届" in tag or "在校" in tag:
        return "应届/在校"
    if tag in {"1年以内", "1-3年"}:
        return "1-3年"
    if tag in {"3-5年", "5-10年"}:
        return tag
    if tag == "10年以上":
        return "10年以上"
    if tag in {"经验不限", "不限经验"}:
        return "经验不限"
    return "未标注"


def salary_by_experience(jobs):
    """薪资×经验交叉统计：各经验档位的岗位数、薪资中位数与未标注数。

    对应求职者核心问题"我这个经验水平大概什么价位"（参考开源招聘分析
    平台 dreamhole 的 salary_median_for_comparison 实践）。

    Returns:
        dict: {经验档位: {"count", "median_k", "unparsed"}}；档位按
            应届→10年以上→经验不限→未标注 固定顺序
    """
    buckets = {b: [] for b in
               ("应届/在校", "1-3年", "3-5年", "5-10年", "10年以上",
                "经验不限", "未标注")}
    unparsed = {b: 0 for b in buckets}
    for job in jobs:
        if not isinstance(job, dict):
            continue
        bucket = experience_bucket(experience_tag_for_job(job)) or "未标注"
        parsed = parse_salary_monthly(job.get("salary"))
        if parsed is None:
            unparsed[bucket] += 1
            continue
        low, high = parsed
        buckets[bucket].append((low + high) / 2)

    result = {}
    for bucket, mids in buckets.items():
        if not mids and unparsed[bucket] == 0:
            continue
        result[bucket] = {
            "count": len(mids) + unparsed[bucket],
            "median_k": float(pd.Series(mids).median()) if mids else None,
            "unparsed": unparsed[bucket],
        }
    return result


def top_salary_jobs(jobs, n=10):
    """高薪岗位榜单：按可解析薪资上限降序取前 n 个（纯统计，可靠）。

    Returns:
        list of dict: {"title", "salary", "company", "location"}；无可解析
            薪资返回空列表
    """
    ranked = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        parsed = parse_salary_monthly(job.get("salary"))
        if parsed is None:
            continue
        ranked.append({
            "title": str(job.get("title") or "").strip(),
            "salary": str(job.get("salary") or "").strip(),
            "company": company_label(job),
            "location": str(job.get("location") or "").strip(),
            "_high": parsed[1],
        })
    ranked.sort(key=lambda item: item["_high"], reverse=True)
    return [{k: v for k, v in item.items() if k != "_high"}
            for item in ranked[:n]]


def filter_details_for_jobs(jobs, details):
    job_ids = {
        str(job.get("job_id")).strip()
        for job in jobs
        if isinstance(job, dict) and str(job.get("job_id") or "").strip()
    }
    if not job_ids:
        return [detail for detail in details if isinstance(detail, dict)]
    return [
        detail
        for detail in details
        if isinstance(detail, dict) and str(detail.get("job_id") or "").strip() in job_ids
    ]


# 疑似销售岗过滤（2026-08-13 用户拍板）：BOSS 搜索为模糊匹配，
# 搜"AI产品经理"会混入"AI产品销售经理/AI产品销售"等非目标岗，
# 拉偏薪资统计与 JD 分析。title 命中特征词 → 强判定；JD 命中强特征词 → 判定。
# 产品岗 JD 常含"销售"单字（如"提升销售转化"），JD 侧只用强特征词避免误伤。
SALES_TITLE_TERMS = ("销售", "bd", "商务拓展", "客户经理", "sdr",
                     "大客户", "渠道经理", "渠道销售", "招商", "外呼")
SALES_JD_TERMS = ("提成", "业绩指标", "完成业绩", "回款", "签单",
                  "客户资源", "销售目标", "销售任务", "佣金", "业绩考核")


def is_sales_job(job, detail=None):
    """疑似销售岗判定：title 特征词强判定；JD 强特征词辅助。

    detail 按 job_id 匹配（含 JD 正文）；缺详情时回退 title 判定与
    job 内联 jd（--merge 数据可能自带）。
    """
    if not isinstance(job, dict):
        return False
    title = str(job.get("title") or job.get("job_name") or "").lower()
    if any(t in title for t in SALES_TITLE_TERMS):
        return True
    jd = ""
    if isinstance(detail, dict):
        jd = str(detail.get("jd") or "")
    if not jd:
        jd = str(job.get("jd") or "")
    jd_lower = jd.lower()
    return any(t in jd_lower for t in SALES_JD_TERMS)


def _record_text_key(value):
    return re.sub(r"\s+", "", str(value or "")).casefold()


def _record_link_keys(record):
    return {
        str(record.get(field) or "").strip()
        for field in ("job_link", "link")
        if str(record.get(field) or "").strip()
    }


def _record_title_keys(record):
    return {
        _record_text_key(record.get(field))
        for field in ("title", "job_name")
        if _record_text_key(record.get(field))
    }


def filter_summary_inputs(jobs, details=None):
    """Filter sales jobs and their details as one retained input set.

    The list is the source of truth.  A detail is associated by ``job_id``
    first, then by a unique job link/title for legacy files without IDs.
    Ambiguous unassociated details are dropped rather than allowed to
    contaminate the retained statistics.  When no list jobs exist,
    standalone detail files remain usable (the existing summary CLI behavior).
    Returns ``(retained_jobs, retained_details, sales_filtered)``.
    """
    jobs = [job for job in (jobs or []) if isinstance(job, dict)]
    details = [detail for detail in (details or []) if isinstance(detail, dict)]
    if not jobs:
        return [], details, 0

    ids_by_job = {}
    links_by_job = {}
    titles_by_job = {}
    for index, job in enumerate(jobs):
        job_id = str(job.get("job_id") or "").strip()
        if job_id:
            ids_by_job.setdefault(job_id, set()).add(index)
        for key in _record_link_keys(job):
            links_by_job.setdefault(key, set()).add(index)
        for key in _record_title_keys(job):
            titles_by_job.setdefault(key, set()).add(index)

    detail_job_indices = []
    for detail in details:
        job_id = str(detail.get("job_id") or "").strip()
        if job_id:
            candidates = set(ids_by_job.get(job_id, ()))
        else:
            candidates = set()
            for key in _record_link_keys(detail):
                candidates.update(links_by_job.get(key, ()))
                if candidates:
                    break
            if not candidates:
                for key in _record_title_keys(detail):
                    candidates.update(titles_by_job.get(key, ()))
                    if candidates:
                        break
            if not candidates:
                # A lone legacy job is the only safe positional-free
                # association available for an unkeyed detail.
                unkeyed = [
                    index for index, job in enumerate(jobs)
                    if not str(job.get("job_id") or "").strip()
                ]
                if len(unkeyed) == 1:
                    candidates.add(unkeyed[0])
        detail_job_indices.append(candidates)

    details_by_job = {index: [] for index in range(len(jobs))}
    for detail, candidates in zip(details, detail_job_indices, strict=True):
        for index in candidates:
            details_by_job[index].append(detail)

    sales_flags = []
    for index, job in enumerate(jobs):
        is_sales = is_sales_job(job)
        if not is_sales:
            is_sales = any(
                is_sales_job(job, detail) for detail in details_by_job[index]
            )
        sales_flags.append(is_sales)

    # JD 与岗位名对不上 → 平台模糊召回带进来的别的岗（实测：招募合伙人引流帖、跨行业
    # 工厂信息化产品经理）。对求职无价值 → 与销售岗同级剔除。
    mismatch_flags = []
    for index, job in enumerate(jobs):
        mismatch = not jd_matches_title(job)
        if not mismatch:
            mismatch = any(
                not jd_matches_title(detail) for detail in details_by_job[index]
            )
        mismatch_flags.append(mismatch)

    dropped_flags = [sales_flags[i] or mismatch_flags[i] for i in range(len(jobs))]
    retained_jobs = [
        job for index, job in enumerate(jobs) if not dropped_flags[index]
    ]
    sales_filtered = sum(sales_flags)
    retained_indices = {
        index for index, dropped in enumerate(dropped_flags) if not dropped
    }
    retained_details = [
        detail
        for detail, candidates in zip(details, detail_job_indices, strict=True)
        if candidates and candidates.issubset(retained_indices)
    ]
    return retained_jobs, retained_details, sales_filtered


def count_jd_title_mismatches(jobs, details=None):
    """有多少条岗位的 **JD 与岗位名对不上**（平台模糊召回带进来的别的岗）。

    单独成一个函数：``filter_summary_inputs`` 的返回结构是既有契约，不改
    （2026-10-22 改过一次签名 → 20 个既有用例直接挂 → 说明它是有契约的）。
    """
    jobs = jobs or []
    details = details or []
    by_job = {}
    for detail in details:
        if isinstance(detail, dict):
            by_job.setdefault(str(detail.get("job_id") or ""), []).append(detail)
    count = 0
    for job in jobs:
        if not isinstance(job, dict):
            continue
        if not jd_matches_title(job):
            count += 1
            continue
        for detail in by_job.get(str(job.get("job_id") or ""), []):
            if not jd_matches_title(detail):
                count += 1
                break
    return count


def term_appears_in_jd(term, jd_text):
    normalized = str(term or "").strip()
    if not normalized:
        return False
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9._+-]*", normalized):
        pattern = rf"(?<![A-Za-z0-9._+-]){re.escape(normalized)}(?![A-Za-z0-9._+-])"
        return re.search(pattern, jd_text, flags=re.IGNORECASE) is not None
    return normalized.lower() in jd_text.lower()


# JD 正文里的页面噪音词黑名单：详情页 JD 夹带了"安全提示/竞争力分析/推荐栏/
# 工商信息/微信扫码"等非职位内容，extract_tech_terms_from_jds 的停用词没覆盖，
# 这里在 job_summary 层过滤，避免噪音污染摘要和提示词。
JD_NOISE_TERMS = {
    # 页面结构性词 / 推荐栏
    "职位描述", "查看全部", "搜索", "更多职位", "看过该职位的", "人还看了",
    "精选职位", "城市招聘", "热门职位", "推荐公司", "热门企业",
    "公司介绍", "工作地址", "点击查看地图", "工商信息", "公司名称",
    # 竞争力分析 / 评级碎句
    "竞争力", "竞争力分析", "安全提示", "包括但不限于", "查看完整个人",
    "个人综合排名", "在人中排名第", "你在", "位置", "微信扫码分享",
    "良好", "优秀", "极好", "一般",
    # BOSS 安全声明碎句（被中文分词切成 2-6 字片段）
    "直聘严禁用人", "单位和招聘者", "用户做出任何", "损害求职者合",
    "法权益的违法", "违规行为", "扣押求职者证", "收取求职者财",
    "向求职者集资", "让求职者入股", "诱导求职者异", "地入职",
    "异地参加培训", "违法违规使用", "求职者简历等", "您一旦发现此",
    "类行为", "请立即举报",
    # 工商信息字段
    "法定代表人", "成立日期", "企业类型", "经营状态", "注册资金",
    "有限责任公司", "存续", "举报",
    # 常见地名/泛词（不是技能）
    "上海", "北京", "深圳", "杭州", "广州", "成都", "南京", "苏州",
    "工程师", "开发工程师", "研发工程师",
    # JD 动态高频词里的纯功能词（动词/虚词/套话，不是技能）
    "需要", "落地", "具备", "能够", "包括", "以及", "根据", "进行",
    "提供", "负责", "熟悉", "了解", "掌握", "完成", "参与", "协助",
    "保证", "确保", "结合", "不断", "持续", "推动", "跟进", "配合",
}
JD_NOISE_TERMS_EN = {"BOSS", "boss", "PDD", "https", "http", "www", "com", "cn"}


def is_jd_noise_term(term):
    """判断 JD 抽取出的词是否是页面噪音（非技能），应从摘要中剔除。"""
    normalized = str(term or "").strip()
    if not normalized:
        return True
    # 英文词走词形 + 黑名单
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9._+-]*", normalized):
        return normalized.lower() in {w.lower() for w in JD_NOISE_TERMS_EN}
    return normalized in JD_NOISE_TERMS


def build_summary(jobs, details=None, search_keyword="", city="", top=10):
    retained_jobs, retained_details, sales_filtered = filter_summary_inputs(
        jobs, details or []
    )
    return _build_summary_from_retained(
        retained_jobs,
        retained_details,
        search_keyword=search_keyword,
        city=city,
        top=top,
        sales_filtered=sales_filtered,
    )


def _build_summary_from_retained(
    jobs, details, search_keyword="", city="", top=10, sales_filtered=0
):
    """Build aggregate statistics from an already filtered input set."""
    salary_ranges = Counter()
    experience = Counter()
    degrees = Counter()
    districts = Counter()
    companies = Counter()
    company_scales = Counter()
    company_stages = Counter()
    skill_tags = Counter()
    jd_terms = Counter()

    for job in jobs:
        if not isinstance(job, dict):
            continue

        salary = str(job.get("salary") or "").strip() or "未标注"
        salary_ranges[salary] += 1

        district = district_from_location(job.get("location", ""))
        if district:
            districts[district] += 1

        company = company_label(job)
        if company:
            companies[company] += 1

        scale = str(job.get("company_scale") or "").strip()
        if scale:
            company_scales[scale] += 1

        stage = str(job.get("company_stage") or "").strip()
        if stage:
            company_stages[stage] += 1

        experience_tag = experience_tag_for_job(job)
        if experience_tag:
            experience[experience_tag] += 1

        degree_tag = degree_tag_for_job(job)
        if degree_tag:
            degrees[degree_tag] += 1

        for tag in split_tags(job.get("tags", "")):
            if is_experience_tag(tag) or is_degree_tag(tag):
                continue
            cleaned = clean_skill_tag(tag)
            if cleaned:
                skill_tags[cleaned] += 1

        for tag in split_tags(job.get("skills", "")):
            cleaned = clean_skill_tag(tag)
            if cleaned:
                skill_tags[cleaned] += 1

        for tag in split_tags(job.get("job_labels", "")):
            cleaned = clean_skill_tag(tag)
            if cleaned:
                skill_tags[cleaned] += 1

    for detail in details:
        if not isinstance(detail, dict):
            continue
        for tag in detail.get("skill_tags") or detail.get("tags") or []:
            cleaned = clean_skill_tag(tag)
            if cleaned:
                skill_tags[cleaned] += 1

    if details:
        tech_terms = boss.extract_tech_terms_from_jds(details, search_keyword)
        for detail in details:
            jd_text = str(detail.get("jd", ""))
            seen_terms = set()
            for term in tech_terms:
                if not term:
                    continue
                normalized = str(term).strip()
                key = normalized.lower()
                # 跳过页面噪音词（安全声明/页脚/地名等），只保留真实技术词
                if is_jd_noise_term(normalized):
                    continue
                if key and term_appears_in_jd(normalized, jd_text) and key not in seen_terms:
                    jd_terms[normalized] += 1
                    seen_terms.add(key)

    # 行业 / 福利 / 角标：平台给了但此前从没人用（"抄了不用"）→ 进报告才算交付
    industries = Counter()
    welfare = Counter()
    icon_words = Counter()
    for job in jobs:
        if not isinstance(job, dict):
            continue
        industry = str(job.get("company_industry") or "").strip()
        if industry:
            industries[industry] += 1
        welfare_text = str(job.get("welfare") or "").strip()
        if welfare_text:
            for item in welfare_text.split(" | "):
                item = item.strip()
                if item:
                    welfare[item] += 1
        icon = str(job.get("icon_word") or "").strip()
        if not icon:
            flags = str(job.get("icon_flags") or "").strip()
            icon = flags.split("|")[0].strip() if flags else ""
        if icon:
            icon_words[icon] += 1

    market = salary_stats(jobs)
    # 分层行情（2026-10-22）：直招 vs 代发分开报，避免代发岗的虚高面议区间带偏行情
    direct_market = salary_stats(jobs, post_types={"direct"})
    agency_market = salary_stats(jobs, post_types={"agency", "anonymous"})
    # 再按招聘类型分层：实习（日薪制）与校招岗和社招**不是同一个薪资口径**，
    # 主口径只算"社招直招"，其余单列。
    social_direct = [j for j in jobs
                      if _post_type_of(j) == "direct" and _recruit_type_of(j) == "social"]
    social_direct_market = salary_stats(social_direct)
    non_social_count = sum(1 for j in jobs if _recruit_type_of(j) != "social")
    return {
        "keyword": search_keyword,
        "city": city,
        "total_jobs": len([job for job in jobs if isinstance(job, dict)]),
        "total_details": len([detail for detail in details if isinstance(detail, dict)]),
        "salary_ranges": _most_common(salary_ranges, top),
        "salary_market": market,
        "salary_market_direct": direct_market,
        "salary_market_agency": agency_market,
        "salary_market_social_direct": social_direct_market,
        "recruit_type_counts": _recruit_type_counts(jobs),
        "non_social_count": non_social_count,
        # 方向分布（2026-10-22）：平台对"产品经理"是模糊召回，搜 AI 产品经理会带回
        # 纯行业产品岗（实测 32.6%）→ 摘要必须说清其中多少是真目标方向。
        "direction_counts": _direction_counts(jobs),
        "ai_pm_count": sum(1 for j in jobs
                           if str(j.get("is_ai_pm") or "") == "True"
                           or _direction_of(j) == "ai_pm"),
        # 样本量警示：可解析薪资 <30 时结论仅供参考（第四轮统计口径）
        "sample_warning": (market.get("parsed", 0) > 0
                           and market.get("parsed", 0) < 30),
        "salary_by_experience": salary_by_experience(jobs),
        "top_salary": top_salary_jobs(jobs, top),
        "experience": _most_common(experience, top),
        "degrees": _most_common(degrees, top),
        "districts": _most_common(districts, top),
        "companies": _most_common(companies, top),
        "industries": _most_common(industries, top),
        "welfare": _most_common(welfare, top),
        "icon_words": _most_common(icon_words, top),
        "company_scales": _most_common(company_scales, top),
        "company_stages": _most_common(company_stages, top),
        "skill_tags": _most_common(skill_tags, top),
        "jd_terms": _most_common(jd_terms, top),
        "sales_filtered": sales_filtered,
        "mismatch_filtered": count_jd_title_mismatches(jobs, details),
    }


def _format_items(items, empty="暂无"):
    if not items:
        return empty
    return "、".join(f"{name}({count})" for name, count in items)


def _salary_market_line(summary):
    """薪资行情行：**主行用"社招直招"口径**，代发岗与实习/校招单列说明。

    - 猎头代发岗的薪资带是"面议区间"不是真实区间（偏高）
    - 实习岗是**日薪制**、校招岗是应届口径 → 与社招**不是同一个薪资口径**
    """
    direct = summary.get("salary_market_direct") or {}
    agency = summary.get("salary_market_agency") or {}
    social = summary.get("salary_market_social_direct") or {}
    if social.get("parsed"):
        market, scope = social, "社招·企业直招"
    elif direct.get("parsed"):
        market, scope = direct, "企业直招岗"
    else:
        market, scope = (summary.get("salary_market") or {}), "全部岗位"
    if not market.get("parsed"):
        return f"薪资行情: 无法解析（未标注 {market.get('unparsed', 0)} 条）"
    line = (f"薪资行情（{scope}）: {market['parsed']} 条可解析，中位 {market['median_k']}K，"
            f"均值 {market['mean_k']}K，区间 {market['low_k']}-{market['high_k']}K")
    if agency.get("parsed"):
        line += (f"；另有 {agency['parsed']} 条猎头代发岗（面议区间、偏高，"
                 f"中位 {agency['median_k']}K，未计入行情）")
    counts = summary.get("recruit_type_counts") or {}
    extra = []
    if counts.get("intern"):
        extra.append(f"{counts['intern']} 条实习岗（日薪制）")
    if counts.get("campus"):
        extra.append(f"{counts['campus']} 条校招岗")
    if extra:
        line += "；" + "、".join(extra) + "（口径不同，未计入行情）"
    return line


def _salary_by_experience_line(summary):
    by_exp = summary.get("salary_by_experience") or {}
    parts = []
    for bucket in ("应届/在校", "1-3年", "3-5年", "5-10年", "10年以上",
                   "经验不限", "未标注"):
        stat = by_exp.get(bucket)
        if not stat:
            continue
        median = f"{stat['median_k']}K" if stat["median_k"] is not None else "无"
        parts.append(f"{bucket}:{median}（{stat['count']} 条）")
    if not parts:
        return "经验薪资: 暂无"
    return "经验薪资: " + "、".join(parts)


def _top_salary_line(summary, n=5):
    top = (summary.get("top_salary") or [])[:n]
    if not top:
        return "高薪岗位: 暂无"
    items = []
    for job in top:
        company = f"｜{job['company']}" if job["company"] else ""
        location = f"｜{job['location']}" if job["location"] else ""
        items.append(f"{job['title']}({job['salary']}){company}{location}")
    return "高薪岗位: " + "；".join(items)


def format_summary(summary):
    title_parts = [summary.get("keyword") or "岗位", summary.get("city") or ""]
    title = " @ ".join(part for part in title_parts if part)
    lines = [
        f"岗位市场摘要: {title}",
        f"列表岗位: {summary['total_jobs']} 条；详情 JD: {summary['total_details']} 条",
    ]
    if summary.get("sales_filtered"):
        lines.insert(1, f"⚠️ 已剔除 {summary['sales_filtered']} 条疑似销售岗（关键词模糊匹配混入）")
    if summary.get("mismatch_filtered"):
        lines.insert(1, f"⚠️ 已剔除 {summary['mismatch_filtered']} 条 JD 与岗位名不符的岗位"
                        "（平台模糊召回带进来的别的岗）")
    if summary.get("sample_warning"):
        lines.insert(1, "⚠️ 样本量警示：可解析薪资不足 30 条，统计结论仅供参考")
    lines += [
        "",
        f"薪资区间: {_format_items(summary['salary_ranges'])}",
        _salary_market_line(summary),
        _salary_by_experience_line(summary),
        _top_salary_line(summary),
        f"经验要求: {_format_items(summary['experience'])}",
        f"学历要求: {_format_items(summary['degrees'])}",
        f"地区分布: {_format_items(summary['districts'])}",
        f"高频公司: {_format_items(summary['companies'])}",
        f"公司规模: {_format_items(summary.get('company_scales'))}",
        f"融资阶段: {_format_items(summary.get('company_stages'))}",
        f"行业分布: {_format_items(summary.get('industries'))}",
        f"福利待遇: {_format_items(summary.get('welfare'))}",
        f"岗位角标: {_format_items(summary.get('icon_words'))}",
        f"技能标签: {_format_items(summary['skill_tags'])}",
        f"JD 高频词: {_format_items(summary['jd_terms'])}",
    ]
    return "\n".join(lines)


def _names(items, limit):
    return [name for name, _ in items[:limit]]


def _dedupe(items):
    result = []
    seen = set()
    for item in items:
        key = str(item).lower()
        if key not in seen:
            result.append(item)
            seen.add(key)
    return result


def build_prompt(summary, jobs_path=None, details_path=None):
    # 语义提炼（技能/能力/画像）交给 agent 从完整 JD 中灵活分析；
    # 脚本只提供可靠的聚合统计（数字类）与结构化技能标签。
    skill_context = _dedupe(_names(summary.get("skill_tags", []), 12))
    salary_context = _format_items(summary.get("salary_ranges", [])[:5])
    exp_context = _format_items(summary.get("experience", [])[:5])
    degree_context = _format_items(summary.get("degrees", [])[:5])
    district_context = _format_items(summary.get("districts", [])[:5])

    data_line = ""
    if jobs_path or details_path:
        data_line = f"完整数据文件: 列表 {jobs_path or '未提供'}；详情 {details_path or '未提供'}（可读取文件做深度调研，如高薪岗位的技能画像、岗位类型聚类等）"

    return "\n".join([
        "请基于下面的 BOSS 直聘岗位市场摘要，帮我优化求职材料和面试准备。",
        "",
        f"岗位市场摘要: {summary.get('keyword') or '未指定关键词'} @ {summary.get('city') or '未指定城市'}",
        f"样本规模: 列表 {summary.get('total_jobs', 0)} 条，详情 JD {summary.get('total_details', 0)} 条",
        _salary_market_line(summary),
        f"高频技能标签: {', '.join(skill_context) if skill_context else '暂无'}",
        f"常见薪资区间: {salary_context}",
        f"主流经验要求: {exp_context}",
        f"主流学历要求: {degree_context}",
        f"岗位集中地区: {district_context}",
        data_line,
        "",
        "请输出：",
        "1. 简历技能关键词补齐建议",
        "2. 项目经历和工作经历的改写方向",
        "3. 面试准备清单",
        "4. 投递时需要避开的岗位特征",
        "5. 基于薪资行情的薪酬预期建议",
        "",
        "要求：不要虚构经历，只把真实经历改写得更贴近这些岗位；结论要引用上面的统计依据。",
    ])


def load_detail_file(path):
    path = os.path.abspath(os.path.expanduser(path))
    with open(path, encoding="utf-8") as f:
        details = json.load(f)
    if not isinstance(details, list):
        raise ValueError(f"详情文件必须是 JSON list: {path}")
    return details


def load_details_for_input(input_path, detail_path=None, result_dir=DEFAULT_RESULT_DIR):
    if detail_path:
        return load_detail_file(detail_path)

    result_dir = os.path.expanduser(result_dir)
    with contextlib.redirect_stdout(io.StringIO()):
        return boss.load_existing_details(
            input_path=input_path,
            detail_output=None,
            result_dir=result_dir,
        ) or []


def generate_charts(jobs, output_dir, filename_prefix="summary"):
    """生成薪资分布直方图 + 经验×薪资中位条形图（PNG，matplotlib Agg 无头后端）。

    Returns:
        list[str]: 生成的 PNG 路径；无可解析薪资样本时返回空列表。
    """
    os.makedirs(output_dir, exist_ok=True)
    rows = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        parsed = parse_salary_monthly(job.get("salary"))
        if parsed is None:
            continue
        low, high = parsed
        bucket = experience_bucket(experience_tag_for_job(job)) or "未标注"
        rows.append({"mid_k": (low + high) / 2, "bucket": bucket})
    if not rows:
        return []
    df = pd.DataFrame(rows)

    charts = []

    # 1. 薪资中位分布直方图（可解析样本）
    fig, ax = plt.subplots(figsize=(8, 4))
    bins = min(12, max(5, int(df["mid_k"].nunique())))
    ax.hist(df["mid_k"], bins=bins, edgecolor="white", color="#4C72B0")
    ax.set_title("薪资中位分布（千元/月）")
    ax.set_xlabel("月薪（千元）")
    ax.set_ylabel("岗位数")
    path1 = os.path.join(output_dir, f"{filename_prefix}_salary_hist.png")
    fig.tight_layout()
    fig.savefig(path1, dpi=120)
    plt.close(fig)
    charts.append(path1)

    # 2. 经验×薪资中位条形图（档位固定顺序）
    order = ("应届/在校", "1-3年", "3-5年", "5-10年", "10年以上",
             "经验不限", "未标注")
    grouped = (df.groupby("bucket")["mid_k"].median()
               .reindex([b for b in order if b in df["bucket"].values])
               .dropna())
    if not grouped.empty:
        fig, ax = plt.subplots(figsize=(8, 4))
        ax.bar(grouped.index.astype(str), grouped.values, color="#55A868")
        ax.set_title("经验档位 × 薪资中位（千元/月）")
        ax.set_xlabel("经验要求")
        ax.set_ylabel("月薪中位（千元）")
        ax.tick_params(axis="x", rotation=15)
        path2 = os.path.join(output_dir, f"{filename_prefix}_salary_by_exp.png")
        fig.tight_layout()
        fig.savefig(path2, dpi=120)
        plt.close(fig)
        charts.append(path2)

    return charts


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description="对已抓取的 BOSS 岗位 JSON 做聚合摘要，并生成可复制的求职材料优化提示词。"
    )
    parser.add_argument("--input", help="boss_jobs_*.json 路径；不传则读取默认结果目录下最新列表文件")
    parser.add_argument("--details", help="boss_details_*.json 路径；不传则按同时间戳或最新详情文件自动查找")
    parser.add_argument("--result-dir", default=DEFAULT_RESULT_DIR, help="默认结果目录")
    parser.add_argument("--keyword", help="覆盖列表文件里的搜索关键词")
    parser.add_argument("--city", help="覆盖列表文件里的城市")
    parser.add_argument("--top", type=positive_int, default=10, help="每个维度展示前 N 项")
    parser.add_argument("--charts-dir",
                        help="生成薪资图表（PNG）的输出目录；不传则生成到结果目录 charts/ 子目录")
    parser.add_argument("--no-charts", action="store_true", help="不生成图表")
    output_group = parser.add_mutually_exclusive_group()
    output_group.add_argument("--summary-only", action="store_true", help="只输出聚合摘要")
    output_group.add_argument("--prompt-only", action="store_true", help="只输出提示词")
    return parser


def positive_int(raw):
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("必须是正整数") from None
    if value < 1:
        raise argparse.ArgumentTypeError("必须是正整数")
    return value


def main(argv=None):
    # 与 boss_cdp_raw.py 保持一致：Windows GBK 控制台重配 UTF-8，避免 emoji 输出崩溃。
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    input_path = os.path.expanduser(args.input) if args.input else find_latest_jobs_file(args.result_dir)
    if not input_path:
        print(f"未找到列表文件，请先抓取岗位或用 --input 指定 boss_jobs_*.json。结果目录: {args.result_dir}")
        return 1

    try:
        jobs, metadata = load_jobs_file(input_path)
        details = load_details_for_input(input_path, args.details, args.result_dir)
    except (OSError, json.JSONDecodeError, ValueError) as e:
        print(f"无法加载输入文件: {e}")
        return 1
    keyword = args.keyword if args.keyword is not None else metadata.get("keyword", "")
    city = args.city if args.city is not None else metadata.get("city", "")
    jobs, details, sales_filtered = filter_summary_inputs(jobs, details)
    summary = _build_summary_from_retained(
        jobs,
        details,
        search_keyword=keyword,
        city=city,
        top=args.top,
        sales_filtered=sales_filtered,
    )

    # 详情文件路径：--details 优先，否则复用 boss 的自动查找逻辑（供提示词引用）
    details_path = args.details
    if not details_path:
        for candidate in boss.detail_candidate_paths(input_path, None, args.result_dir):
            if os.path.exists(candidate):
                details_path = candidate
                break

    if not args.prompt_only:
        print(format_summary(summary))
    if not args.summary_only:
        if not args.prompt_only:
            print("\n--- 可复制提示词 ---")
        print(build_prompt(summary, jobs_path=input_path, details_path=details_path))

    # 图表生成（分析产品化）：默认输出到结果目录 charts/，摘要尾部引用
    if not args.no_charts:
        charts_dir = (os.path.expanduser(args.charts_dir)
                      if args.charts_dir
                      else os.path.join(os.path.expanduser(args.result_dir), "charts"))
        charts = generate_charts(jobs, charts_dir)
        if charts:
            print("\n--- 图表 ---")
            for path in charts:
                print(f"![{os.path.basename(path)}]({path})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
