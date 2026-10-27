"""分析报告与冒烟测试（从 boss_cdp_raw.py 增量拆分）。

含：JD 技术术语提取、终端分析报告、冒烟测试、git 短 SHA。

约束：boss 侧全局经 _core() 取用（调用栈优先）；re/Counter/subprocess/os
直引（tests 无相关补丁）；time/datetime 走 _core()（patch 先例）。
"""

import logging
import os
import re
import subprocess
from collections import Counter

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


def extract_tech_terms_from_jds(details, search_keyword=""):
    """从 JD 文本中动态提取高频技术术语。

    策略：
    1. 保留一个小的基础术语列表用于匹配
    2. 对 JD 正文做分词频率分析，提取高频词
    3. 将搜索关键词拆分后加入

    Args:
        details: 详情列表，每个含 "jd" 字段
        search_keyword: 搜索关键词

    Returns:
        去重后的术语列表
    """
    # 基础技术术语（小列表，用于精确匹配）
    base_tech_terms = [
        "Java", "Spring", "Redis", "MySQL", "Kafka", "Flink", "Spark",
        "Go", "Python", "微服务", "分布式", "高并发",
        "AI", "LLM", "RAG", "Agent", "SQL", "Linux",
    ]

    # 从搜索关键词中提取词
    keyword_terms = []
    for word in re.split(r'[\s,，、]+', search_keyword):
        word = word.strip()
        if len(word) >= 2:
            keyword_terms.append(word)

    # 从 JD 文本中提取高频词
    word_freq = Counter()
    for d in details:
        jd_text = d.get("jd", "")
        if not jd_text:
            continue
        # 提取英文技术词（连续 2+ 字母的词）
        en_words = re.findall(r'\b[A-Za-z][A-Za-z0-9._-]+\b', jd_text)
        for w in en_words:
            if len(w) >= 2 and len(w) <= 30:
                word_freq[w] += 1
        # 提取中文技术词（简单：连续中文字符 2-6 个）
        cn_words = re.findall(r'[\u4e00-\u9fff]{2,6}', jd_text)
        # 过滤常见非技术中文词
        stop_words = {
            "任职", "要求", "岗位", "职责", "描述", "优先", "具有",
            "负责", "相关", "经验", "能力", "以上", "及其", "工作",
            "开发", "团队", "项目", "公司", "业务", "熟悉", "熟练",
            "了解", "掌握", "参与", "完成", "进行", "能够", "学历",
            "专业", "提供", "福利", "加入", "我们", "我们只", "是通过",
            "就是", "已经", "可以", "这个", "那个", "什么", "怎么",
            "欢迎", "期待", "为你", "为你提供",
        }
        for w in cn_words:
            if w not in stop_words:
                word_freq[w] += 1

    # 取频率最高的动态词（至少出现 2 次，取 top 60）
    dynamic_terms = [
        word for word, count in word_freq.most_common(60)
        if count >= 2
    ]

    # 合并去重：基础 + 关键词 + 动态提取
    all_terms = list(dict.fromkeys(
        base_tech_terms + keyword_terms + dynamic_terms
    ))
    return all_terms


def analyze(list_data, details=None, search_keyword=""):
    jobs = list_data.get("jobs", [])
    print(f"\n{'='*60}")
    print(f"  分析报告: {list_data.get('keyword','')} @ {list_data.get('city','')}")
    print(f"  共 {len(jobs)} 条职位")
    print(f"{'='*60}")

    # 1. 薪资分析
    print("\n--- 薪资分布 ---")
    salary_ranges = Counter()
    for j in jobs:
        s = j.get("salary", "")
        if "K" in s:
            salary_ranges[s] += 1
        elif "元/天" in s:
            salary_ranges[s] += 1
        else:
            salary_ranges["未标注"] += 1
    for s, c in salary_ranges.most_common(15):
        bar = "█" * c
        print(f"  {s:<20} {c:>3}  {bar}")

    # 2. 经验要求
    print("\n--- 经验要求 ---")
    exp_count = Counter()
    for j in jobs:
        tags = j.get("tags", "")
        for t in tags.split(" | "):
            if "年" in t or "应届" in t or "在校" in t or "经验不限" in t:
                exp_count[t] += 1
    for e, c in exp_count.most_common():
        print(f"  {e:<15} {c}")

    # 3. 学历要求
    print("\n--- 学历要求 ---")
    edu_count = Counter()
    for j in jobs:
        tags = j.get("tags", "")
        for t in tags.split(" | "):
            if t in ["大专", "本科", "硕士", "博士", "学历不限"]:
                edu_count[t] += 1
    for e, c in edu_count.most_common():
        print(f"  {e:<10} {c}")

    # 4. 地区分布
    print("\n--- 地区分布 ---")
    loc_count = Counter()
    for j in jobs:
        loc = j.get("location", "")
        # Extract district
        parts = loc.split("·")
        if len(parts) >= 2:
            loc_count[parts[1]] += 1
        elif loc:
            loc_count[loc] += 1
    for loc, count in loc_count.most_common(10):
        print(f"  {loc:<15} {count}")

    # 5. 公司分布
    print("\n--- 高频公司 ---")
    company_count = Counter()
    for j in jobs:
        c = j.get("boss_name", "")
        if c:
            company_count[c] += 1
    for c, n in company_count.most_common(10):
        print(f"  {c:<25} {n} 个岗位")

    # 6. 详情页的技能标签（如有）
    body_freq = Counter()
    if details:
        print("\n--- 技能要求频次（来自 JD 标签）---")
        skill_freq = Counter()
        for d in details:
            for tag in d.get("skill_tags", []):
                skill_freq[tag] += 1
        for s, c in skill_freq.most_common(25):
            bar = "█" * c
            print(f"  {s:<20} {c:>3}/{len(details)}  {bar}")

        # 7. JD 正文关键词（动态提取）
        print("\n--- JD 正文高频技术词 ---")
        tech_terms = extract_tech_terms_from_jds(details, search_keyword)
        for d in details:
            jd_lower = d.get("jd", "").lower()
            for term in tech_terms:
                if term.lower() in jd_lower:
                    body_freq[term] += 1
        for t, c in body_freq.most_common(25):
            pct = c / len(details) * 100
            bar = "█" * c
            print(f"  {t:<20} {c:>3}/{len(details)} ({pct:.0f}%)  {bar}")

    # 8. 简历建议
    print("\n--- 简历建议 ---")
    if details and body_freq:
        noise_list = {'BOSS直聘', 'boss', 'BOSS', '来自BOSS直聘', '金', '金币'}
        top_skills = [s for s, _ in Counter(
            tag for d in details for tag in d.get("skill_tags", [])
        ).most_common(10)]
        # 如果有效标签太少或都是噪音，用 JD 正文关键词代替
        valid_skills = [s for s in top_skills if len(s) >= 2 and s not in noise_list]
        if len(valid_skills) < 3:
            top_skills = [t for t, _ in body_freq.most_common(10)]
        top_body = [t for t, _ in body_freq.most_common(8)] if body_freq else []
        print(f"  技能关键词: {', '.join(top_skills)}")
        print(f"  正文高频词: {', '.join(top_body)}")
        # Experience requirement
        if exp_count:
            top_exp = exp_count.most_common(1)[0][0]
            print(f"  经验要求主流: {top_exp}")
        if edu_count:
            top_edu = edu_count.most_common(1)[0][0]
            print(f"  学历要求主流: {top_edu}")
    else:
        print("  提示: 用 --detail 抓取 JD 详情后可获得更精准的简历建议")


def has_usable_smoke_jobs(jobs):
    for job in jobs:
        if not isinstance(job, dict):
            continue
        if (
            job.get("title")
            and job.get("salary")
            and job.get("salary_source") == "api"
            and job.get("job_link")
        ):
            return True
    return False


def run_smoke_test(cdp_port=_core().DEFAULT_CDP_PORT):
    """Run a real browser/API smoke test without writing result files."""
    core = _core()
    if not core.require_runtime_dependencies("requests", "websocket"):
        return 1

    try:
        cdp = core.CDPSession(cdp_port)
        city_name, city_code = core.resolve_city(core.DEFAULT_CITY_INPUT)
        search_url = core.build_search_url(core.LOGIN_PROBE_QUERY, city_code, 1, {})
        tid, sid = core.create_page_session(cdp)

        print(f"打开 BOSS 搜索页: {core.LOGIN_PROBE_QUERY} @ {city_name}")
        cdp.send("Page.navigate", {"url": search_url}, sid)
        core.time.sleep(4)
        api_url = (f"{core.API_JOB_LIST_PATH}?"
                   f"{core.urlencode({'scene': '1', 'query': core.LOGIN_PROBE_QUERY, 'city': city_code, 'page': 1, 'pageSize': 5})}")
        api_js = core.FETCH_API_JS_TEMPLATE.replace("__API_URL__", api_url)
        jobs = core.parse_api_jobs_eval_value(cdp.eval_js(api_js, sid))
        cdp.send("Target.closeTarget", {"targetId": tid})
        cdp.close()

        if core.has_usable_smoke_jobs(jobs):
            sample = next(job for job in jobs if job.get("salary") and job.get("job_link"))
            print(f"✅ Smoke test 通过: {sample.get('title')} | {sample.get('salary')}")
            return 0
        print("❌ Smoke test 未拿到可用职位；请检查登录态或 BOSS API 返回")
        return 1
    except (core.requests.ConnectionError, core.requests.Timeout, KeyError,
            core.json.JSONDecodeError, core.websocket.WebSocketException, TimeoutError) as e:
        print(f"❌ Smoke test 失败: {e}")
        return 1


def _git_short_sha():
    """尽力取当前 git 短 SHA（非仓库/无 git 时返回空串，不抛）。"""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            capture_output=True, text=True, timeout=5)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""
