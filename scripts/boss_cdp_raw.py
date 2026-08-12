#!/usr/bin/env python3
"""
BOSS直聘职位抓取 + 分析 — 纯 CDP raw protocol

功能:
  1. 搜索特定职位 (关键词 + 城市)
  2. 筛选公司规模、融资阶段、薪资范围、经验、学历、行业
  3. 抓取详情页 JD 并分析薪资范围和技能要求
  4. 输出结构化 JSON + CSV + 终端分析报告
  5. 环境检查、Chrome CDP 自动启动、登录状态检测

用法:
  uv run python3 scripts/boss_cdp_raw.py --keyword "Java 风控" --city 101020100 --pages 5
  uv run python3 scripts/boss_cdp_raw.py --keyword "Java 风控" --scale 305 --salary 406
  uv run python3 scripts/boss_cdp_raw.py --keyword "Java 风控" --analysis
  uv run python3 scripts/boss_cdp_raw.py --keyword "Java 风控" --detail
  uv run python3 scripts/boss_cdp_raw.py --check
  uv run python3 scripts/boss_cdp_raw.py --setup-chrome
  uv run python3 scripts/boss_cdp_raw.py --version
"""

__version__ = "2.3.0"

import json
import math
import time
import random
import sys
import argparse
import os
import re
import hashlib
import csv
import glob
import platform
import subprocess
import shutil
import signal
import logging
import ntpath
import threading
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from dataclasses import dataclass
from datetime import datetime
from collections import Counter
from enum import Enum
from urllib.parse import urlencode, urlparse, urlunparse, parse_qsl

websocket = None
requests = None

# ============================================================
# 全局常量
# ============================================================

# CDP 默认端口（可通过 --cdp-port 覆盖）
DEFAULT_CDP_PORT = 9222

# API 基础路径（便于统一修改）
API_JOB_LIST_PATH = "/wapi/zpgeek/search/joblist.json"
HOT_CITY_URL = "https://www.zhipin.com/wapi/zpgeek/search/job/hot/city.json"
CITY_GROUP_URL = "https://www.zhipin.com/wapi/zpCommon/data/cityGroup.json"

# 请求频率保护
MAX_PAGES = 10          # 单次最大页数
MAX_API_REQUESTS = 500  # 单次最大 API 请求数
API_ATTEMPT_LIMIT = 2   # 列表 API 单页最大尝试次数（规格 NFR-3：最多 1 次自动重试）
MAX_CDP_CONSECUTIVE_ERRORS = 3  # 详情会话连续失败熔断阈值（浏览器会话异常判定）
DEFAULT_CONCURRENCY = 1         # 详情抓取默认并发度（1=串行，保持原行为）
MAX_PENDING_RETRIES = 3         # 详情失败自动重试次数上限（超出后放弃，避免短 JD 等永久失败浪费请求）
LOGIN_PROBE_CACHE_TTL = 600     # 登录探测结果会话内缓存时长（秒，10 分钟）
FORMAT_VERSION = 1              # 导出文件契约版本（ai-pm-job-intel 规格 §3.2；契约变更时递增）
SCRAPE_LOCK_PATH = os.path.expanduser("~/.boss-zhipin-scraper/scrape.lock")  # 单进程互斥锁（规格 §3.6）

def get_default_chrome_path():
    system = platform.system()
    if system == "Darwin":
        return "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if system == "Windows":
        candidates = []
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            candidates.append(ntpath.join(local_app_data, "Google", "Chrome", "Application", "chrome.exe"))
        for env_name in ("PROGRAMFILES", "PROGRAMFILES(X86)"):
            base = os.environ.get(env_name)
            if base:
                candidates.append(ntpath.join(base, "Google", "Chrome", "Application", "chrome.exe"))
        for candidate in candidates:
            if os.path.exists(candidate):
                return candidate
        return candidates[0] if candidates else "chrome.exe"

    candidates = [
        "/usr/bin/google-chrome",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
        "/snap/bin/chromium",
    ]
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    return candidates[0]


def get_default_profile_dir():
    system = platform.system()
    if system == "Darwin":
        return os.path.expanduser("~/Library/Application Support/Google/Chrome")
    if system == "Windows":
        base = os.environ.get("LOCALAPPDATA")
        if not base:
            base = ntpath.join(os.path.expanduser("~"), "AppData", "Local")
        return ntpath.join(base, "Google", "Chrome", "User Data")
    return os.path.expanduser("~/.config/google-chrome")


DEFAULT_CHROME_PATH = get_default_chrome_path()
DEFAULT_PROFILE_DIR = get_default_profile_dir()

DEFAULT_CDP_DATA_DIR = os.path.expanduser("~/.boss-zhipin-scraper/chrome-profile")
DEFAULT_RESULT_DIR = os.path.expanduser("~/.boss-zhipin-scraper/job-result")
DEFAULT_CITY_INPUT = "上海"
LOGIN_PROBE_QUERY = "Java"
LOGIN_PROBE_CITY = "101020100"
LOGIN_PROBE_TARGETS = (
    ("Java", "101020100"),
    ("AI Agent", "101010100"),
    ("产品经理", "101280600"),
)
LOGIN_PROBE_PAGE_SIZE = 10
LOGIN_PROBE_MAX_INTERVAL = 15
LOGIN_PROBE_MAX_TRANSIENT_ERRORS = 2
LOGIN_RESTRICTED_CODES = {31, 37}
# BOSS 风控码会随平台策略变化，码表追不上时按 message 关键字兜底识别风控/限流，
# 避免把「已登录但被风控」误判为 RESPONSE_ERROR 进而当成登录失败。
LOGIN_RESTRICTED_MESSAGE_KEYWORDS = (
    "环境存在异常",
    "访问频繁",
    "操作太频繁",
    "安全校验",
    "滑块",
    "验证",
)
DEFAULT_LOGIN_TIMEOUT = 300

# 全局请求计数器
_request_counter = 0
_live_city_maps_cache = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("boss_cdp")


def default_output_path(kind):
    filename = f"boss_{kind}_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    return os.path.join(DEFAULT_RESULT_DIR, filename)


def require_runtime_dependencies(*names):
    global requests, websocket

    missing = []
    if "requests" in names and requests is None:
        try:
            import requests as requests_module
            requests = requests_module
        except ImportError:
            missing.append("requests")
    if "websocket" in names and websocket is None:
        try:
            import websocket as websocket_module
            websocket = websocket_module
        except ImportError:
            missing.append("websocket-client")
    if missing:
        print(f"缺少依赖: {' '.join(missing)}")
        print("请安装（任选其一）:")
        print(f"  uv add {' '.join(missing)}")
        print(f"  pip install {' '.join(missing)}")
        return False
    return True


# ============================================================
# 筛选参数映射
# Source snapshots:
# - 城市: https://www.zhipin.com/wapi/zpgeek/search/job/hot/city.json + cityGroup.json
# - 筛选项: https://www.zhipin.com/wapi/zpgeek/search/job/condition.json
# ============================================================
# 城市码表已外置到 data/city_codes.json（全量城市，覆盖一二三四五线），
# 见 issue #24。resolve_city 查询链：本地静态 → 运行时拉 BOSS 接口 → 9 位裸码兜底。
# 仓库内路径（开发态）与打包后路径（pip install）都在 _city_data_path() 里处理。
CITY_DATA_FILENAME = "city_codes.json"

_local_city_map_cache = None


def _city_data_path():
    """返回 data/city_codes.json 的路径，兼容仓库开发态与 pip 打包态。"""
    # 1. 仓库开发态：脚本在 scripts/，数据在 ../data/
    repo_data = os.path.join(os.path.dirname(__file__), "..", "data", CITY_DATA_FILENAME)
    if os.path.isfile(repo_data):
        return os.path.normpath(repo_data)
    # 2. 打包态：wheel force-include 到包根 data/，用 importlib.resources 兜底
    try:
        from importlib.resources import files  # py3.9+
        pkg_data = files(__package__ or "__main__").joinpath("..", "data", CITY_DATA_FILENAME) \
            if __package__ else None
    except Exception:  # 有意宽捕：importlib.resources 在不同 Python/打包形态抛不同类型异常
        pkg_data = None
    if pkg_data is not None and os.path.isfile(str(pkg_data)):
        return str(pkg_data)
    # 3. 找不到则返回开发态路径（让调用方决定降级）
    return os.path.normpath(repo_data)


def load_local_city_map():
    """读取本地 data/city_codes.json 静态全量城市码表。

    返回 (name_to_code, code_to_name) 两个字典；读取失败返回 ({}, {})。
    结果缓存，重复调用零开销。
    """
    global _local_city_map_cache
    if _local_city_map_cache is not None:
        return _local_city_map_cache
    name_to_code = {}
    try:
        path = _city_data_path()
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            for name, code in raw.items():
                if name and code is not None:
                    name_to_code[str(name)] = str(code)
    except (OSError, json.JSONDecodeError, ValueError) as e:
        log.debug(f"读取本地城市码表失败: {e}")
    code_to_name = {code: name for name, code in name_to_code.items()}
    _local_city_map_cache = name_to_code, code_to_name
    return _local_city_map_cache

SCALE_MAP = {
    "0-20人": "301", "20-99人": "302", "100-499人": "303",
    "500-999人": "304", "1000-9999人": "305", "10000人以上": "306",
}

STAGE_MAP = {
    "未融资": "801", "天使轮": "802", "A轮": "803", "B轮": "804",
    "C轮": "805", "D轮及以上": "806", "已上市": "807", "不需要融资": "808",
}

SALARY_MAP = {
    "不限": "0", "3K以下": "402", "3-5K": "403", "5-10K": "404",
    "10-20K": "405", "20-50K": "406", "50K以上": "407",
}

EXPERIENCE_MAP = {
    "不限": "0", "在校生": "108", "应届生": "102", "经验不限": "101",
    "1年以内": "103", "1-3年": "104",
    "3-5年": "105", "5-10年": "106", "10年以上": "107",
}

DEGREE_MAP = {
    "不限": "0", "初中及以下": "209", "中专/中技": "208", "高中": "206",
    "大专": "202", "本科": "203", "硕士": "204", "博士": "205",
}

INDUSTRY_MAP = {
    "互联网": "1001", "电子商务": "1002", "金融": "1003", "游戏": "1004",
    "企业服务": "1005", "教育培训": "1006", "社交网络": "1007",
    "医疗健康": "1008", "生活服务": "1009", "广告营销": "1010",
}


# ============================================================
# 全局请求计数器辅助
# ============================================================
def incr_request():
    """递增全局请求计数，达到上限时抛出异常"""
    global _request_counter
    _request_counter += 1
    if _request_counter > MAX_API_REQUESTS:
        raise RuntimeError(f"已达到单次最大请求数 {MAX_API_REQUESTS}，停止抓取")
    if _request_counter >= MAX_API_REQUESTS * 0.8:
        log.warning(f"⚠️ 请求次数接近上限: {_request_counter}/{MAX_API_REQUESTS}")


# ============================================================
# CDP 连接
# ============================================================
class CDPSession:
    def __init__(self, cdp_port=DEFAULT_CDP_PORT):
        if not require_runtime_dependencies("requests", "websocket"):
            raise RuntimeError("缺少 CDP 运行依赖")
        self.cdp_port = cdp_port
        resp = requests.get(f"http://127.0.0.1:{cdp_port}/json/version", timeout=10)
        ws_url = resp.json()["webSocketDebuggerUrl"]
        self.ws = websocket.create_connection(ws_url, timeout=60)
        self.mid = 0

    def send(self, method, params=None, sid=None, timeout=30):
        """发送 CDP 命令并等待匹配的响应。

        Args:
            method: CDP 方法名
            params: 参数字典
            sid: Target session ID
            timeout: 等待响应的超时秒数，默认 30s

        Returns:
            CDP 响应字典

        Raises:
            TimeoutError: 超过 max_retries 仍未收到匹配响应
        """
        self.mid += 1
        msg = {"id": self.mid, "method": method, "params": params or {}}
        if sid:
            msg["sessionId"] = sid
        self.ws.send(json.dumps(msg))

        start_time = time.time()
        max_retries = 1000

        for attempt in range(max_retries):
            # 检查超时
            elapsed = time.time() - start_time
            if elapsed > timeout:
                raise TimeoutError(
                    f"CDP send({method}) 超时 ({timeout}s), "
                    f"已跳过 {attempt} 条不匹配消息"
                )

            try:
                raw = self.ws.recv()
            except websocket.WebSocketTimeoutException:
                raise TimeoutError(f"CDP WebSocket recv 超时, method={method}")

            try:
                r = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                log.debug(f"跳过非 JSON 消息: {raw[:100]}")
                continue

            if r.get("id") == self.mid:
                return r

            # 不匹配的消息：可能是事件通知，记录并跳过
            event_name = r.get("method", "unknown")
            log.debug(f"跳过不匹配消息 (id={r.get('id')}, event={event_name})")

        raise TimeoutError(
            f"CDP send({method}) 在 {max_retries} 条消息内未找到匹配响应"
        )

    def eval_js(self, js, sid):
        r = self.send("Runtime.evaluate", {"expression": js, "returnByValue": True}, sid)
        return r.get("result", {}).get("result", {}).get("value", None)

    def close(self):
        self.ws.close()


BACKGROUND_VISIBILITY_SCRIPT = (
    "Object.defineProperty(document, 'hidden', {get: () => false});"
    "Object.defineProperty(document, 'visibilityState', {get: () => 'visible'});"
    "Object.defineProperty(document, 'webkitHidden', {get: () => false});"
    "Object.defineProperty(document, 'webkitVisibilityState', {get: () => 'visible'});"
)


def create_page_session(cdp, background=True):
    """Create and attach an about:blank target without stealing focus by default.

    Background pages report themselves as hidden, which prevents BOSS detail
    pages from rendering reliably. Register the existing visibility override
    before callers navigate. Interactive callers such as the login flow must
    opt into a foreground target explicitly.
    """
    target = cdp.send(
        "Target.createTarget",
        {"url": "about:blank", "background": background},
    )
    target_id = target["result"]["targetId"]
    attached = cdp.send(
        "Target.attachToTarget",
        {"targetId": target_id, "flatten": True},
    )
    session_id = attached["result"]["sessionId"]
    if background:
        cdp.send(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": BACKGROUND_VISIBILITY_SCRIPT},
            session_id,
        )
    return target_id, session_id


# ============================================================
# 页面级风控/验证码检测与人工介入
#
# API 层风控（code 31/37 等）由 classify_login_probe_response 处理；
# 这里补"页面级"判据：滑块验证、安全验证页、登录墙等需要人工介入的场景。
# 判据参考 nothing248/boss-scrapy 的四重检测思路。
# ============================================================
DEFAULT_RISK_WAIT_TIMEOUT = 120   # 等待人工处理的最长秒数
RISK_WAIT_INTERVAL = 5            # 轮询间隔秒数

RISK_PROBE_JS = """
(function(){
    var title = document.title || '';
    var url = location.href || '';
    var bodyText = document.body ? (document.body.innerText || '') : '';
    var slider = document.querySelector(
        '.nc_scale, .captcha-slider, .puzzle-captcha, .geetest_slider, ' +
        '.yidun_slider, .captcha_verify_box, .verify-captcha'
    );
    return JSON.stringify({
        url: url,
        title: title,
        hasSlider: !!slider,
        hasLoginWall: bodyText.indexOf('登录查看完整内容') !== -1
    });
})()
"""

RISK_TITLE_KEYWORDS = ("安全验证", "安全检查", "滑块验证", "验证码", "安全校验")
RISK_URL_KEYWORDS = ("security-check", "security.html", "verify", "captcha")


def _cdp_exception_types():
    """返回 CDP 层可预期异常的元组。

    websocket-client 是 lazy 导入（模块顶部 websocket=None），且测试环境中是
    Mock（其属性不是异常类）。这里动态解析真实异常类，避免 except 元组里出现
    非异常类型导致 TypeError。
    """
    types = (RuntimeError, TimeoutError, KeyError, OSError)
    ws_exc = getattr(websocket, "WebSocketException", None) if websocket is not None else None
    if isinstance(ws_exc, type) and issubclass(ws_exc, BaseException) and ws_exc not in types:
        types += (ws_exc,)
    return types


def probe_risk_page(cdp, sid):
    """通过注入 JS 探测当前页面是否存在风控/验证码痕迹。

    Returns:
        dict: {"url", "title", "hasSlider", "hasLoginWall"}；探测失败返回 {}。
    """
    try:
        val = cdp.eval_js(RISK_PROBE_JS, sid)
    except _cdp_exception_types():
        log.debug("风控页面探测失败", exc_info=True)
        return {}
    if not val:
        return {}
    try:
        probe = json.loads(val) if isinstance(val, str) else val
    except (json.JSONDecodeError, ValueError, TypeError):
        return {}
    return probe if isinstance(probe, dict) else {}


def classify_risk_page(probe):
    """判断页面探测结果是否命中风控/验证码判据。

    Args:
        probe: probe_risk_page 返回的 dict

    Returns:
        (is_risk, reason): is_risk 为 True 时 reason 说明命中的判据
    """
    if not isinstance(probe, dict):
        return False, ""
    url = str(probe.get("url") or "")
    title = str(probe.get("title") or "")
    if any(kw in url.lower() for kw in RISK_URL_KEYWORDS):
        return True, "访问到验证/安全页面"
    if any(kw in title for kw in RISK_TITLE_KEYWORDS):
        return True, f"页面标题含验证关键词「{title}」"
    if probe.get("hasSlider"):
        return True, "检测到滑块验证元素"
    if probe.get("hasLoginWall"):
        return True, "页面出现登录墙"
    return False, ""


def wait_for_risk_clear(cdp, sid, timeout=DEFAULT_RISK_WAIT_TIMEOUT,
                        interval=RISK_WAIT_INTERVAL):
    """页面命中风控时提示用户人工处理，轮询直到恢复或超时。

    Returns:
        True: 风控已解除（判据消失）；False: 等待超时。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        probe = probe_risk_page(cdp, sid)
        is_risk, reason = classify_risk_page(probe)
        if not is_risk:
            return True
        remaining = int(deadline - time.time())
        print(f"⚠️ 检测到风控/验证码：{reason}。")
        print(f"   请在专用 Chrome 中完成人工验证（滑动/点选等），等待恢复（剩余 {remaining}s）...")
        time.sleep(min(interval, remaining) if remaining > 0 else interval)
    print(f"❌ 等待人工处理超时（{timeout}s），已停止当前任务。")
    print("   可稍后重试；如果频繁出现，请检查网络环境（代理/VPN 出口 IP 可能触发风控）。")
    return False


# ============================================================
# 通过页面内 XHR 调 API 获取列表数据（明文薪资）
# ============================================================
FETCH_API_JS_TEMPLATE = """
(function(){
    var xhr = new XMLHttpRequest();
    xhr.open('GET', '__API_URL__', false);
    xhr.send();
    if (xhr.status !== 200) return JSON.stringify([{error: xhr.status}]);
    var data = JSON.parse(xhr.responseText);
    var jobs = (data.zpData || {}).jobList || [];
    var results = jobs.map(function(j) {
        return {
            title: j.jobName || '',
            salary: j.salaryDesc || '',
            salary_source: j.salaryDesc ? 'api' : 'api_empty',
            location: (j.cityName || '') + '\\u00b7' + (j.areaDistrict || '') + '\\u00b7' + (j.businessDistrict || ''),
            tags: [j.jobExperience || '', j.jobDegree || ''].filter(function(t){return t && t !== '\\u4e0d\\u9650';}).join(' | '),
            boss_name: j.brandName || '',
            company_name: j.brandName || '',
            boss_title: j.bossTitle || '',
            boss_active_status: j.activeTimeDesc || (j.bossOnline ? '\\u5728\\u7ebf' : ''),
            experience: j.jobExperience || '',
            education: j.jobDegree || '',
            company_scale: j.brandScaleName || '',
            company_stage: j.brandStageName || '',
            company_industry: j.brandIndustry || '',
            job_labels: (j.jobLabels || []).join(' | '),
            skills: j.skills || [],
            security_id: j.securityId || '',
            lid: j.lid || '',
            encrypt_job_id: j.encryptJobId || '',
            encrypt_boss_id: j.encryptBossId || '',
            encrypt_brand_id: j.encryptBrandId || '',
            job_link: j.encryptJobId ? 'https://www.zhipin.com/job_detail/' + j.encryptJobId + '.html' : '',
            company_link: j.encryptBrandId ? 'https://www.zhipin.com/gongsi/' + j.encryptBrandId + '.html' : '',
            welfare: (j.welfareList || []).join(' | ')
        };
    });
    return JSON.stringify(results);
})()
"""

# ============================================================
# DEPRECATED: DOM 提取作为 fallback（薪资可能是加密字体）
# 此方法已弃用，仅作为 API 方式失败时的最后降级手段。
# 新代码应优先使用 FETCH_API_JS_TEMPLATE 通过 API 获取数据。
# ============================================================
EXTRACT_LIST_JS = """
(function(){
    var results = [];
    var cards = document.querySelectorAll('li.job-card-box');
    for (var i = 0; i < cards.length; i++) {
        var card = cards[i];
        var nameEl = card.querySelector('.job-name');
        var salaryEl = card.querySelector('.job-salary');
        var locEl = card.querySelector('.company-location');
        var tagEls = card.querySelectorAll('.tag-list li');
        var bossEl = card.querySelector('.boss-name');
        var bossLink = card.querySelector('.boss-info');
        var tags = [];
        for (var j = 0; j < tagEls.length; j++) tags.push(tagEls[j].innerText.trim());
        var jobLink = nameEl ? (nameEl.getAttribute('href') || '') : '';
        if (jobLink && jobLink.charAt(0) === '/') jobLink = 'https://www.zhipin.com' + jobLink;
        var cLink = bossLink ? (bossLink.getAttribute('href') || '') : '';
        if (cLink && cLink.charAt(0) === '/') cLink = 'https://www.zhipin.com' + cLink;
        var t = nameEl ? nameEl.innerText.trim() : '';
        if (t) results.push({
            title: t,
            salary: salaryEl ? salaryEl.innerText.trim() : '',
            salary_source: 'dom_untrusted',
            location: locEl ? locEl.innerText.trim() : '',
            tags: tags.join(' | '),
            boss_name: bossEl ? bossEl.innerText.trim() : '',
            job_link: jobLink,
            company_link: cLink
        });
    }
    return JSON.stringify(results);
})()
"""

# ============================================================
# 详情页提取与校验
# ============================================================
DETAIL_LOGIN_MARKER = "登录查看完整内容"
DETAIL_DESCRIPTION_MARKER = "职位描述"
DETAIL_COMPETITIVENESS_MARKER = "竞争力分析"
DETAIL_SAFETY_MARKER = "BOSS 安全提示"
MIN_DETAIL_TEXT_LENGTH = 120


class DetailExtractionError(ValueError):
    """The rendered page does not contain a usable job description."""


class DetailLoginRequiredError(DetailExtractionError):
    """The detail page is truncated because the BOSS session is not logged in."""


EXTRACT_DETAIL_JS = """
(function(){
    var pageText = document.body ? document.body.innerText : '';
    var tags = [];
    var benefitWords = ['五险','补充医疗','定期体检','带薪年假','年终奖','零食','餐补',
        '节日福利','加班补助','股票期权','员工旅游','交通补助','通讯补贴','团建',
        '生日福利','免费班车','全勤奖','包吃','弹性工作','下午茶','租房补贴',
        '体检','健身','文化','充电假','司龄假','红包','能量补贴','社团','三薪',
        '绩效','底薪','保底','活动基金','学习基金','节日礼品','无障碍'];
    var noiseWords = ['BOSS直聘','boss','BOSS','来自BOSS直聘','金','金币'];
    function isBenefit(t) {
        if (t === '...' || t.length > 15 || t.length < 2) return true;
        for (var i = 0; i < benefitWords.length; i++) {
            if (t.includes(benefitWords[i])) return true;
        }
        for (var i = 0; i < noiseWords.length; i++) {
            if (t === noiseWords[i] || t.includes(noiseWords[i])) return true;
        }
        return false;
    }
    document.querySelectorAll('.job-tags .tag-all span, .job-keyword-list span').forEach(function(s){
        var t = s.innerText.trim();
        if(t && !isBenefit(t)) tags.push(t);
    });
    var jd = '';
    var sections = document.querySelectorAll('.job-detail-section, .job-sec');
    for (var i = 0; i < sections.length; i++) {
        var text = (sections[i].innerText || '').trim();
        if (text.indexOf('职位描述') !== -1 && text.length > jd.length) {
            jd = text;
        }
    }
    return JSON.stringify({
        jd: jd,
        page_text: pageText.substring(0, 12000),
        tags: tags,
        url: location.href
    });
})()
"""


def _normalize_detail_whitespace(text):
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").splitlines()]
    normalized = "\n".join(lines).strip()
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return re.sub(r"[ \t]{2,}", " ", normalized)


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


def map_list_boss_active_status(job):
    """Map list-API job fields to ``boss_active_status``.

    BOSS ``/wapi/zpgeek/search/joblist.json`` typically exposes ``bossOnline``
    but not ``activeTimeDesc``. Prefer ``activeTimeDesc`` when present;
    otherwise map ``bossOnline=True`` to 「在线」. Detailed labels such as
    「刚刚活跃」still come from the detail path.
    """
    if not isinstance(job, dict):
        return ""
    desc = str(job.get("activeTimeDesc") or "").strip()
    if desc:
        return desc
    if job.get("bossOnline"):
        return "在线"
    return ""


def resolve_boss_active_status(list_status="", detail_status=""):
    """Prefer detail activity text; fall back to list mapping result."""
    detail = str(detail_status or "").strip()
    if detail:
        return detail
    return str(list_status or "").strip()


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
    return card_info(end)


def _recruiter_footer_start(lines):
    start, _status = _recruiter_footer_info(lines)
    return start


def extract_detail_fields(extracted, min_length=MIN_DETAIL_TEXT_LENGTH):
    """Return validated JD and boss activity status as separate fields.

    ``jd`` never includes the recruiter card or activity label.
    ``boss_active_status`` is extracted from that card when present.

    ``page_text`` is diagnostic input only. It is never persisted unless it has
    an explicit job-description section that passes all checks.
    """
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
    if not text and DETAIL_DESCRIPTION_MARKER in page_text:
        text = page_text
    if DETAIL_DESCRIPTION_MARKER in text:
        text = text.split(DETAIL_DESCRIPTION_MARKER, 1)[1]

    lines = text.replace("\r\n", "\n").splitlines()
    footer_start, boss_active_status = _recruiter_footer_info(lines)
    if footer_start is not None:
        lines = lines[:footer_start]
    else:
        for index, line in enumerate(lines):
            if line.strip() == DETAIL_SAFETY_MARKER:
                lines = lines[:index]
                break

    jd = _normalize_detail_whitespace("\n".join(lines))
    if len(jd) < min_length:
        raise DetailExtractionError(
            f"job description too short after validation: {len(jd)} < {min_length}"
        )
    return {"jd": jd, "boss_active_status": boss_active_status}


def extract_job_description(extracted, min_length=MIN_DETAIL_TEXT_LENGTH):
    """Return validated JD text without BOSS page chrome."""
    return extract_detail_fields(extracted, min_length=min_length)["jd"]


# ============================================================
# 解析城市参数（支持中文和代码）
# ============================================================
class CityAPIResponseError(ValueError):
    """BOSS 城市接口返回业务错误或无效响应。"""


class CityResolutionError(ValueError):
    """无法把用户输入解析为有效城市码。"""


def fetch_boss_json(url, timeout=10):
    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()

    if not isinstance(data, dict):
        raise CityAPIResponseError(f"BOSS 城市接口返回非对象响应: {url}")

    code = data.get("code")
    if code != 0:
        message = data.get("message") or "未知错误"
        raise CityAPIResponseError(
            f"BOSS 城市接口返回业务错误 code={code}, message={message}: {url}"
        )
    if not isinstance(data.get("zpData"), dict):
        raise CityAPIResponseError(f"BOSS 城市接口响应缺少有效 zpData: {url}")
    return data


def load_live_city_maps(timeout=10):
    global _live_city_maps_cache
    if _live_city_maps_cache is not None:
        return _live_city_maps_cache

    name_to_code = {}

    try:
        hot_city_data = fetch_boss_json(HOT_CITY_URL, timeout=timeout)
        for item in hot_city_data.get("zpData", {}).get("hotCityList", []):
            name = item.get("name")
            code = item.get("code")
            if name and code is not None:
                name_to_code[name] = str(code)

        city_group_data = fetch_boss_json(CITY_GROUP_URL, timeout=timeout)
        for group in city_group_data.get("zpData", {}).get("cityGroup", []):
            for item in group.get("cityList", []):
                name = item.get("name")
                code = item.get("code")
                if name and code is not None:
                    name_to_code.setdefault(name, str(code))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError,
            CityAPIResponseError) as e:
        log.warning(f"加载 BOSS 在线城市映射失败: {e}")

    code_to_name = {code: name for name, code in name_to_code.items()}
    _live_city_maps_cache = name_to_code, code_to_name
    return _live_city_maps_cache


def resolve_city(city_input):
    """把「中文城市名 / 城市码」解析为 (name, code)。

    查询链（逐级降级）:
      1. 本地静态码表 data/city_codes.json（全量、离线可用）
      2. 运行时拉 BOSS 接口 hot/city.json + cityGroup.json（自愈）
      3. 都查不到时接受 9 位裸 city code，其他输入报错
    """
    if not city_input:
        return city_input, city_input

    # 1. 本地静态码表
    local_map, local_reverse = load_local_city_map()
    if city_input in local_map:
        return city_input, local_map[city_input]
    if city_input in local_reverse:
        return local_reverse[city_input], city_input

    # 2. 运行时拉 BOSS 接口
    live_map, live_reverse = load_live_city_maps()
    if city_input in live_map:
        return city_input, live_map[city_input]
    if city_input in live_reverse:
        return live_reverse[city_input], city_input

    # 3. 仍未命中的 9 位纯数字视为用户直接传入的裸 city code
    if re.fullmatch(r"\d{9}", city_input):
        return city_input, city_input

    raise CityResolutionError(
        f"无法解析城市 '{city_input}'：本地城市码表和 BOSS 在线城市接口均未命中。"
        "请传入受支持的中文城市名或 9 位 city code；已停止抓取，"
        "避免将无效城市参数误判为 0 个岗位。"
    )


def list_cities(keyword=None, use_live=True):
    """打印支持的城市列表。keyword 非空时只打印城市名含该关键词的城市。

    优先用运行时拉取的最新码表（use_live=True），拉取失败回退本地静态码表。
    """
    name_to_code = {}
    if use_live:
        live_map, _ = load_live_city_maps()
        name_to_code.update(live_map)
    if not name_to_code:
        local_map, _ = load_local_city_map()
        name_to_code.update(local_map)
    if not name_to_code:
        print("⚠️ 无法加载城市码表（本地静态文件缺失且网络拉取失败）")
        return

    items = sorted(name_to_code.items(), key=lambda kv: kv[0])
    if keyword:
        keyword = keyword.strip()
        items = [(n, c) for n, c in items if keyword in n]
        if not items:
            print(f"没有匹配「{keyword}」的城市")
            return
    print(f"共 {len(items)} 个城市（支持中文城市名或城市码）：")
    for name, code in items:
        print(f"  {name}\t{code}")


class LoginProbeStatus(Enum):
    """Outcome of one login probe request."""

    AVAILABLE = "available"
    UNAUTHENTICATED = "unauthenticated"
    RESTRICTED = "restricted"
    EMPTY = "empty"
    RESPONSE_ERROR = "response_error"


@dataclass(frozen=True)
class LoginProbeResult:
    """Structured login probe result with the original failure context."""

    status: LoginProbeStatus
    code: int | None = None
    message: str = ""
    retryable: bool = False


def classify_login_probe_response(data, http_status=200):
    """Classify a BOSS search response without collapsing failures to bool."""
    if http_status == 401:
        return LoginProbeResult(
            LoginProbeStatus.UNAUTHENTICATED,
            message="HTTP 401",
        )
    if http_status in (403, 429):
        return LoginProbeResult(
            LoginProbeStatus.RESTRICTED,
            message=f"HTTP {http_status}",
        )
    if http_status != 200:
        return LoginProbeResult(
            LoginProbeStatus.RESPONSE_ERROR,
            message=f"HTTP {http_status}",
            retryable=http_status == 0 or http_status >= 500,
        )
    if not isinstance(data, dict):
        return LoginProbeResult(
            LoginProbeStatus.RESPONSE_ERROR,
            message="响应不是 JSON 对象",
            retryable=True,
        )

    raw_code = data.get("code")
    try:
        code = int(raw_code) if raw_code is not None else None
    except (TypeError, ValueError):
        code = None
    message = str(data.get("message") or data.get("msg") or "")

    if code in LOGIN_RESTRICTED_CODES:
        return LoginProbeResult(LoginProbeStatus.RESTRICTED, code=code, message=message)
    if code != 0:
        # code 不在已知风控码集合里时，再按 message 关键字兜底判定是否风控，
        # 避免新风控码被当成不可恢复的 RESPONSE_ERROR 误拦已登录用户。
        if any(kw in message for kw in LOGIN_RESTRICTED_MESSAGE_KEYWORDS):
            return LoginProbeResult(LoginProbeStatus.RESTRICTED, code=code, message=message)
        return LoginProbeResult(LoginProbeStatus.RESPONSE_ERROR, code=code, message=message)

    zp_data = data.get("zpData")
    if not isinstance(zp_data, dict):
        return LoginProbeResult(
            LoginProbeStatus.RESPONSE_ERROR,
            code=code,
            message="响应缺少 zpData",
            retryable=True,
        )
    job_list = zp_data.get("jobList")
    if not isinstance(job_list, list):
        return LoginProbeResult(
            LoginProbeStatus.RESPONSE_ERROR,
            code=code,
            message="响应缺少 jobList",
            retryable=True,
        )
    if not job_list:
        return LoginProbeResult(LoginProbeStatus.EMPTY, code=code)
    if any(
        (job.get("salaryDesc") or "").strip()
        for job in job_list
        if isinstance(job, dict)
    ):
        return LoginProbeResult(LoginProbeStatus.AVAILABLE, code=code)
    return LoginProbeResult(LoginProbeStatus.UNAUTHENTICATED, code=code)


def is_logged_in_search_response(data):
    """Return True only when BOSS returns jobs with plaintext salary."""
    result = classify_login_probe_response(data)
    return result.status is LoginProbeStatus.AVAILABLE


def build_login_probe_url(query, city_code):
    params = {
        "scene": 1,
        "query": query,
        "city": city_code,
        "page": 1,
        "pageSize": LOGIN_PROBE_PAGE_SIZE,
    }
    return f"{API_JOB_LIST_PATH}?{urlencode(params)}"


def probe_login_state(cdp, sid, query=LOGIN_PROBE_QUERY, city_code=LOGIN_PROBE_CITY):
    """Run exactly one budgeted search probe and return its structured state."""
    probe_url = build_login_probe_url(query, city_code)
    js = f"""
    (function(){{
        var xhr = new XMLHttpRequest();
        xhr.open('GET', '{probe_url}', false);
        xhr.send();
        return JSON.stringify({{
            httpStatus: xhr.status,
            body: xhr.responseText
        }});
    }})()
    """
    incr_request()
    val = cdp.eval_js(js, sid)
    if not val:
        return LoginProbeResult(
            LoginProbeStatus.RESPONSE_ERROR,
            message="探测响应为空",
            retryable=True,
        )
    try:
        envelope = json.loads(val) if isinstance(val, str) else val
    except (json.JSONDecodeError, ValueError):
        return LoginProbeResult(
            LoginProbeStatus.RESPONSE_ERROR,
            message="探测响应不是有效 JSON",
            retryable=True,
        )
    if not isinstance(envelope, dict):
        return LoginProbeResult(
            LoginProbeStatus.RESPONSE_ERROR,
            message="探测响应格式异常",
            retryable=True,
        )

    raw_http_status = envelope.get("httpStatus", 200)
    try:
        http_status = int(raw_http_status)
    except (TypeError, ValueError):
        http_status = 0
    body = envelope.get("body", envelope)
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            return LoginProbeResult(
                LoginProbeStatus.RESPONSE_ERROR,
                message="搜索接口响应不是有效 JSON",
                retryable=True,
            )
    return classify_login_probe_response(body, http_status=http_status)


def describe_login_probe_result(result):
    """Return a concise user-facing explanation for a non-available state."""
    context = []
    if result.code is not None:
        context.append(f"code: {result.code}")
    if result.message:
        context.append(result.message)
    suffix = f"（{'; '.join(context)}）" if context else ""

    if result.status is LoginProbeStatus.UNAUTHENTICATED:
        return f"未检测到可用登录态{suffix}"
    if result.status is LoginProbeStatus.RESTRICTED:
        return f"BOSS 接口返回限制状态{suffix}"
    if result.status is LoginProbeStatus.EMPTY:
        return "探测样本没有职位，暂时无法确认登录态"
    return f"登录探测响应异常{suffix}"


# ============================================================
# 登录状态检测
# ============================================================
_LOGIN_PROBE_CACHE = {"ts": 0.0, "result": None}


def check_login_state(cdp_port=DEFAULT_CDP_PORT, use_cache=True):
    """通过 CDP 检测 BOSS直聘登录状态（会话内结果缓存）。

    同一进程内 TTL 内重复调用（如 --check 与抓取前的登录检测）直接复用
    上次探测结果，避免重复开 tab、导航与请求；`--login-timeout` 等待循环
    不受影响（走 wait_for_login，不经过本缓存）。

    Args:
        cdp_port: CDP 端口
        use_cache: False 时强制重新探测（绕过缓存）

    Returns:
        LoginProbeResult: 登录探测的结构化状态
    """
    cached = _LOGIN_PROBE_CACHE
    if use_cache and cached["result"] is not None \
            and time.time() - cached["ts"] < LOGIN_PROBE_CACHE_TTL:
        return cached["result"]
    result = _probe_login_state_uncached(cdp_port)
    cached["ts"] = time.time()
    cached["result"] = result
    return result


def _probe_login_state_uncached(cdp_port=DEFAULT_CDP_PORT):
    """实际探测逻辑（无缓存）：多组关键词/城市轮换探测。

    单次探测可能因关键词恰好 0 结果而误判（EMPTY），轮换可降低误判；
    未登录（UNAUTHENTICATED）与风控（RESTRICTED）是确定状态，命中直接
    返回，不继续轮换。

    Returns:
        LoginProbeResult: 登录探测的结构化状态
    """
    cdp = None
    tid = None
    try:
        cdp = CDPSession(cdp_port)
        tid, sid = create_page_session(cdp)

        # 先导航到 BOSS直聘，确保 cookie 域名正确
        cdp.send("Page.navigate", {"url": "https://www.zhipin.com/"}, sid)
        time.sleep(4)

        last_result = None
        for query, city_code in LOGIN_PROBE_TARGETS:
            result = probe_login_state(cdp, sid, query=query, city_code=city_code)
            if result.status in (
                LoginProbeStatus.AVAILABLE,
                LoginProbeStatus.UNAUTHENTICATED,
                LoginProbeStatus.RESTRICTED,
            ):
                return result
            last_result = result
        return last_result if last_result is not None else LoginProbeResult(
            LoginProbeStatus.RESPONSE_ERROR,
            message="全部探测均无结果",
        )
    except (requests.ConnectionError, requests.Timeout, KeyError,
            json.JSONDecodeError, websocket.WebSocketException,
            TimeoutError, RuntimeError) as e:
        log.error(f"登录状态检测失败: {e}")
        return LoginProbeResult(
            LoginProbeStatus.RESPONSE_ERROR,
            message=str(e),
        )
    finally:
        if cdp is not None:
            if tid is not None:
                try:
                    cdp.send("Target.closeTarget", {"targetId": tid})
                except (KeyError, websocket.WebSocketException, TimeoutError):
                    log.debug("关闭登录探测 target 失败", exc_info=True)
            try:
                cdp.close()
            except websocket.WebSocketException:
                log.debug("关闭登录探测 CDP 连接失败", exc_info=True)


def wait_for_login(cdp_port=DEFAULT_CDP_PORT, timeout=DEFAULT_LOGIN_TIMEOUT, interval=3):
    """Open BOSS login page and wait until plaintext salary is available."""
    cdp = CDPSession(cdp_port)
    tid, sid = create_page_session(cdp, background=False)
    cdp.send(
        "Page.navigate",
        {"url": "https://www.zhipin.com/web/user/"},
        sid,
    )

    deadline = time.time() + timeout
    logged_in = False
    attempt = 0
    transient_errors = 0
    print(f"等待 BOSS 登录完成（最长 {timeout}s）", end="", flush=True)
    try:
        while time.time() <= deadline:
            query, city_code = LOGIN_PROBE_TARGETS[attempt % len(LOGIN_PROBE_TARGETS)]
            try:
                result = probe_login_state(cdp, sid, query=query, city_code=city_code)
            except RuntimeError as e:
                print(f"\n❌ {e}")
                return False

            if result.status is LoginProbeStatus.AVAILABLE:
                logged_in = True
                print("\n✅ 已检测到 BOSS 登录态，且接口返回明文薪资")
                return True
            if result.status is LoginProbeStatus.RESTRICTED:
                print(f"\n❌ {describe_login_probe_result(result)}，已停止登录探测")
                print("   当前问题不是尚未登录；请先在浏览器中完成验证或稍后再试")
                return False
            if result.status is LoginProbeStatus.RESPONSE_ERROR:
                if not result.retryable:
                    print(f"\n❌ {describe_login_probe_result(result)}，已停止登录探测")
                    return False
                transient_errors += 1
                if transient_errors > LOGIN_PROBE_MAX_TRANSIENT_ERRORS:
                    print(f"\n❌ {describe_login_probe_result(result)}，连续异常次数过多")
                    return False
            else:
                transient_errors = 0

            print(".", end="", flush=True)
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            delay = min(interval * (2 ** attempt), LOGIN_PROBE_MAX_INTERVAL, remaining)
            time.sleep(delay)
            attempt += 1
        print("\n❌ 等待登录超时")
        print("   Chrome 会继续保持打开；登录后可重新运行 --check 或抓取命令")
        return False
    finally:
        if logged_in:
            cdp.send("Target.closeTarget", {"targetId": tid})
        cdp.close()


# ============================================================
# CSV 导出
# ============================================================
CSV_COLUMNS = [
    "job_id", "title", "salary", "salary_source", "location", "tags", "boss_name",
    "boss_active_status",
    "company_scale", "company_stage", "company_industry", "skills",
    "job_link", "welfare",
]

DETAIL_CSV_COLUMNS = [
    "job_id", "title", "company", "salary", "salary_source", "location",
    "boss_active_status", "tags_list", "job_link", "skill_tags", "jd",
]


def write_csv(csv_path, jobs):
    """将 jobs 列表写入 CSV 文件"""
    os.makedirs(os.path.dirname(csv_path) or ".", exist_ok=True)
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for j in jobs:
            # 确保每列都有值
            row = {col: j.get(col, "") for col in CSV_COLUMNS}
            writer.writerow(row)
    print(f"CSV 已保存: {csv_path}")


def write_detail_csv(csv_path, details):
    """将岗位详情列表写入 CSV 文件"""
    os.makedirs(os.path.dirname(csv_path) or ".", exist_ok=True)
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=DETAIL_CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for d in details:
            row = {col: d.get(col, "") for col in DETAIL_CSV_COLUMNS}
            if isinstance(row.get("skill_tags"), list):
                row["skill_tags"] = " | ".join(row["skill_tags"])
            writer.writerow(row)
    print(f"详情 CSV 已保存: {csv_path}")


# ============================================================
# 增量写入 JSON
# ============================================================
def merge_unique(existing, incoming, key="job_id", new_overrides=False):
    """按 key 合并去重。

    Args:
        existing: 已有记录列表
        incoming: 新记录列表
        key: 去重字段
        new_overrides: True 时新记录覆盖旧记录（同 key 保留新的）；False 时旧记录优先

    Returns:
        合并后的列表
    """
    if new_overrides:
        by_key = {}
        for item in existing:
            if isinstance(item, dict) and item.get(key):
                by_key[item.get(key)] = item
        for item in incoming:
            if isinstance(item, dict) and item.get(key):
                by_key[item.get(key)] = item
        return list(by_key.values())

    seen = {item.get(key, "") for item in existing if isinstance(item, dict)}
    merged = list(existing)
    for item in incoming:
        if isinstance(item, dict) and item.get(key, "") not in seen:
            seen.add(item.get(key, ""))
            merged.append(item)
    return merged


def _atomic_write_json(path, payload):
    """先写临时文件再原子替换，避免进程中断留下半截 JSON 覆盖旧数据。"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp_path = f"{path}.tmp{os.getpid()}"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


_SENSITIVE_KEYS = ("cookie", "token", "wt2", "zp_stoken", "zp_token",
                   "password", "account", "auth", "secret")
# BOSS 内部标识字段（规格侧建议剔除：下游误读风险，非契约字段；
# 详情抓取用 job_link 即可导航，不依赖这些参数）
_INTERNAL_KEYS = ("security_id", "lid", "encrypt_job_id",
                  "encrypt_boss_id", "encrypt_brand_id")


def _sanitize_job(job):
    """导出前过滤敏感字段与 BOSS 内部标识（规格 NFR-6 + 联调建议）。

    外部数据（--merge/--input）可能夹带凭据字段；列表 API 字段均为公开
    职位信息，不受影响。
    """
    if not isinstance(job, dict):
        return job
    return {k: v for k, v in job.items()
            if not any(s in k.lower() for s in _SENSITIVE_KEYS)
            and k not in _INTERNAL_KEYS}


def flush_jobs(path, meta, jobs):
    """每次有新数据就全量刷写（jobs 去重后），保证异常退出也能保留"""
    existing_jobs = []
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                old = json.load(f)
            existing_jobs = old.get("jobs", [])
        except (json.JSONDecodeError, OSError, ValueError):
            pass
    merged = merge_unique(existing_jobs, jobs)
    meta["format_version"] = FORMAT_VERSION
    meta["total"] = len(merged)
    meta["job_count"] = len(merged)
    meta["jobs"] = [_sanitize_job(j) for j in merged]
    _atomic_write_json(path, meta)


# ============================================================
# 单进程互斥（规格 §3.6：防止多任务并发启动超频/竞争 Chrome）
# 锁文件格式：第一行最大并发数 N，后续每行一个持有 pid，
# 熔断时追加一行 risk（任一并发任务遇风控 → 全停广播）
#   N
#   pid1
#   pid2
#   risk
# 并发上限默认 1（现状）；--max-concurrent N 仅指令显式放开（如 2-3）
# ============================================================
RISK_LINE = "risk"


def _pid_is_running(pid):
    """检查 pid 对应进程是否存活（跨平台）。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        if platform.system() == "Windows":
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"Get-Process -Id {pid} -ErrorAction SilentlyContinue"],
                capture_output=True, text=True, timeout=5)
            return bool(r.stdout.strip())
        os.kill(pid, 0)
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


def _read_scrape_lock():
    """读取锁文件，返回 (max_concurrent, 持有 pid 列表, risk 标志)；损坏返回 (1, [], False)。"""
    try:
        with open(SCRAPE_LOCK_PATH, "r", encoding="utf-8") as f:
            lines = [ln.strip() for ln in f.read().splitlines() if ln.strip()]
    except (OSError, UnicodeDecodeError, ValueError):
        return 1, [], False
    if not lines:
        return 1, [], False
    try:
        max_concurrent = int(lines[0])
    except (TypeError, ValueError):
        max_concurrent = 1
    risk = RISK_LINE in lines
    holders = [ln for ln in lines[1:] if ln != RISK_LINE]
    return max_concurrent, holders, risk


def _write_scrape_lock(max_concurrent, holders, risk=False):
    """原子写锁文件（tmp + os.replace，防并发撕裂）。"""
    tmp = SCRAPE_LOCK_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(str(max_concurrent) + "\n")
        for pid in holders:
            f.write(str(pid) + "\n")
        if risk:
            f.write(RISK_LINE + "\n")
    os.replace(tmp, SCRAPE_LOCK_PATH)


def acquire_scrape_lock(max_concurrent=1):
    """获取并发锁；熔断中或存活持有者已达上限时返回 False。

    max_concurrent=1 时行为与旧版单 pid 互斥一致（硬防线，默认）。
    持锁进程崩溃（pid 已死）自动清理其条目并接管。
    熔断标志（risk）一旦置位即拒绝新任务（挂起等人工确认；
    人工处理后 --reset-lock 清除，再重开）。
    """
    if max_concurrent < 1:
        max_concurrent = 1
    try:
        os.makedirs(os.path.dirname(SCRAPE_LOCK_PATH), exist_ok=True)
        max_c, holders, risk = _read_scrape_lock()
        if risk:
            return False  # 熔断中：必须人工确认（--reset-lock）后才能重开
        if os.path.exists(SCRAPE_LOCK_PATH):
            alive = [p for p in holders if _pid_is_running(p)]
            if len(alive) >= max(max_c, max_concurrent):
                return False
        else:
            alive = []
        _write_scrape_lock(max_concurrent, alive + [str(os.getpid())])
        return True
    except OSError:
        return False


def set_scrape_lock_risk():
    """置熔断标志：任一并发任务遇风控时广播全停（其他任务在页间检查并停止）。"""
    try:
        if not os.path.exists(SCRAPE_LOCK_PATH):
            return
        max_c, holders, risk = _read_scrape_lock()
        if risk:
            return
        _write_scrape_lock(max_c, holders, risk=True)
    except OSError:
        pass


def is_scrape_lock_risk():
    """读取当前锁文件是否已熔断（并发任务全停广播）。"""
    try:
        if not os.path.exists(SCRAPE_LOCK_PATH):
            return False
        _, _, risk = _read_scrape_lock()
        return risk
    except OSError:
        return False


def release_scrape_lock():
    """释放本进程持有的锁；只移除自己的 pid（他人 pid 保留）。

    熔断中（risk 已置位）保留锁文件（挂起等人工确认），不因持有者退出而删除；
    无 risk 且无剩余持有者时删除锁文件。
    """
    try:
        if not os.path.exists(SCRAPE_LOCK_PATH):
            return
        max_c, holders, risk = _read_scrape_lock()
        mine = str(os.getpid())
        rest = [p for p in holders if p != mine]
        if len(rest) == len(holders):
            return  # 锁里没有自己（他人/已清理），不动
        if rest or risk:
            # 仍有持有者，或熔断中需保留风险状态
            _write_scrape_lock(max_c, rest, risk=risk)
        else:
            os.remove(SCRAPE_LOCK_PATH)
    except OSError:
        pass


def clear_scrape_lock_risk():
    """人工确认后清除熔断状态（--reset-lock）。

    仅清除 risk 标志；若锁文件内仍有存活持有者则保留（他人任务在跑不动）。
    """
    try:
        if not os.path.exists(SCRAPE_LOCK_PATH):
            return True
        max_c, holders, risk = _read_scrape_lock()
        if not risk:
            return True
        if holders:
            _write_scrape_lock(max_c, holders, risk=False)
        else:
            os.remove(SCRAPE_LOCK_PATH)
        return True
    except OSError:
        return False


# ============================================================
# 合并外部 JSON 文件
# ============================================================
def merge_jobs(external_path, new_jobs):
    """从外部 JSON 加载 jobs，与 new_jobs 按 job_id 合并去重。

    Args:
        external_path: 已有 JSON 文件路径
        new_jobs: 新抓取的 jobs 列表

    Returns:
        合并后的 jobs 列表
    """
    try:
        with open(external_path, "r", encoding="utf-8") as f:
            old_data = json.load(f)
    except (json.JSONDecodeError, OSError, ValueError) as e:
        log.warning(f"无法加载合并文件 {external_path}: {e}")
        return new_jobs

    old_jobs = old_data.get("jobs", [])
    merged = merge_unique(old_jobs, new_jobs)
    added = len(merged) - len(old_jobs)
    print(f"合并: 旧文件 {len(old_jobs)} 条 + 新抓取 {len(new_jobs)} 条 = {len(merged)} 条 (新增 {added})")
    return merged


def merge_details(external_path, new_details):
    """从外部 JSON 加载详情，与 new_details 按 job_id 合并去重。

    详情文件本身可能是列表结构（scrape_details 输出）或带 jobs/details 键的字典，
    这里都做兼容。优先保留 new_details 中的同名记录（更新覆盖旧值）。

    Args:
        external_path: 已有详情 JSON 文件路径
        new_details: 新抓取的详情列表（可为空）

    Returns:
        合并后的详情列表
    """
    if not external_path:
        return new_details
    try:
        with open(external_path, "r", encoding="utf-8") as f:
            old_data = json.load(f)
    except (json.JSONDecodeError, OSError, ValueError) as e:
        log.warning(f"无法加载合并详情文件 {external_path}: {e}")
        return new_details

    if isinstance(old_data, list):
        old_details = old_data
    elif isinstance(old_data, dict):
        old_details = old_data.get("details") or old_data.get("jobs") or []
    else:
        old_details = []

    merged = merge_details_from_lists(old_details, new_details)
    print(f"合并详情: 旧文件 {len(old_details)} 条 + 新抓取 {len(new_details)} 条 = {len(merged)} 条")
    return merged


def merge_details_from_lists(old_details, new_details):
    """把两份详情列表按 job_id 合并去重，new_details 优先（同 id 用新覆盖旧）。"""
    return merge_unique(old_details, new_details, new_overrides=True)


# ============================================================
# 构建搜索 URL
# ============================================================
def build_search_url(keyword, city_code, page, filters):
    params = {"query": keyword, "city": city_code, "page": page}
    for key, code in filters.items():
        if code:
            params[key] = code
    return f"https://www.zhipin.com/web/geek/job?{urlencode(params)}"


def should_use_dom_fallback(jobs, allow_dom_fallback=False):
    return allow_dom_fallback and not jobs


def parse_api_jobs_eval_value(value):
    if not value:
        return []
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (json.JSONDecodeError, ValueError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []

    jobs = []
    for item in parsed:
        if not isinstance(item, dict) or item.get("error"):
            continue
        if item.get("title") or item.get("job_link"):
            jobs.append(item)
    return jobs


def is_zhipin_host(url):
    """精确校验 URL 主机为 zhipin.com 或其子域（防伪造 host 的钓鱼导航）。

    对照开源 boss-agent-cli 实践：不做子串判断（"zhipin.com" in url 会被
    zhipin.com.evil.example 这类伪造 host 骗过）。
    """
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return False
    host = host.rstrip(".").lower()
    return host == "zhipin.com" or host.endswith(".zhipin.com")


def build_detail_url(job):
    """Build the URL used for detail navigation without mutating job_link.

    仅接受 zhipin.com 主机：job_link 可能来自外部文件（--merge/--input），
    拒绝导航到任意站点。
    """
    link = job.get("job_link", "")
    if not link:
        return ""
    if not is_zhipin_host(link):
        log.warning(f"跳过非 zhipin.com 详情链接（防外部导航）: {link}")
        return ""

    parsed = urlparse(link)
    params = parse_qsl(parsed.query, keep_blank_values=True)
    existing_keys = {key for key, _ in params}
    for query_key, job_key in (("lid", "lid"), ("securityId", "security_id")):
        value = job.get(job_key) or job.get(query_key) or ""
        if value and query_key not in existing_keys:
            params.append((query_key, value))
            existing_keys.add(query_key)

    return urlunparse(parsed._replace(query=urlencode(params)))


def find_latest_detail_file(result_dir=DEFAULT_RESULT_DIR):
    pattern = os.path.join(result_dir, "boss_details_*.json")
    files = [path for path in glob.glob(pattern)
             if os.path.isfile(path) and not path.endswith(".pending.json")]
    if not files:
        return None
    return max(files, key=lambda path: (os.path.getmtime(path), path))


def detail_candidate_paths(input_path=None, detail_output=None, result_dir=DEFAULT_RESULT_DIR):
    candidates = []
    if detail_output:
        candidates.append(detail_output)
    if input_path:
        directory = os.path.dirname(input_path) or "."
        basename = os.path.basename(input_path)
        if basename.startswith("boss_jobs_"):
            candidates.append(os.path.join(directory, basename.replace("boss_jobs_", "boss_details_", 1)))
    latest = find_latest_detail_file(result_dir)
    if latest:
        candidates.append(latest)

    deduped = []
    seen = set()
    for path in candidates:
        normalized = os.path.abspath(os.path.expanduser(path))
        if normalized not in seen:
            deduped.append(path)
            seen.add(normalized)
    return deduped


def load_existing_details(input_path=None, detail_output=None, result_dir=DEFAULT_RESULT_DIR):
    for path in detail_candidate_paths(input_path, detail_output, result_dir):
        if not path or not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                details = json.load(f)
            if isinstance(details, list):
                print(f"加载详情文件: {path}")
                return details
        except (json.JSONDecodeError, OSError, ValueError) as e:
            log.warning(f"无法加载详情文件 {path}: {e}")
    return None


# ============================================================
# 抓取列表
# ============================================================
def scrape_list(keyword, city_input, max_pages, filters, output_path,
                cdp_port=DEFAULT_CDP_PORT, fmt="json", allow_dom_fallback=False,
                max_jobs=None, max_concurrent=1):
    city_name, city_code = resolve_city(city_input)
    # 单进程互斥（规格 §3.6）：默认并发上限 1（现状）；--max-concurrent N 仅指令显式放开
    if not acquire_scrape_lock(max_concurrent=max_concurrent):
        print("❌ 已有抓取任务在运行（并发已达上限），本次拒绝启动。")
        print(f"EXPORT_FAIL reason=lock_held city={city_name} keyword={keyword}")
        return {"keyword": keyword, "city": city_name, "total": 0, "jobs": []}
    cdp = CDPSession(cdp_port)
    all_jobs = []
    seen = set()
    if not output_path:
        output_path = default_output_path("jobs")

    # 显示筛选条件
    filter_desc = []
    if filters.get("scale"):
        for k, v in SCALE_MAP.items():
            if v == filters["scale"]:
                filter_desc.append(f"规模={k}")
    if filters.get("stage"):
        for k, v in STAGE_MAP.items():
            if v == filters["stage"]:
                filter_desc.append(f"融资={k}")
    if filters.get("salary"):
        for k, v in SALARY_MAP.items():
            if v == filters["salary"]:
                filter_desc.append(f"薪资={k}")
    if filters.get("experience"):
        for k, v in EXPERIENCE_MAP.items():
            if v == filters["experience"]:
                filter_desc.append(f"经验={k}")
    if filters.get("degree"):
        for k, v in DEGREE_MAP.items():
            if v == filters["degree"]:
                filter_desc.append(f"学历={k}")
    if filters.get("industry"):
        for k, v in INDUSTRY_MAP.items():
            if v == filters["industry"]:
                filter_desc.append(f"行业={k}")

    print("=== BOSS直聘抓取 ===")
    print(f"关键词: {keyword} | 城市: {city_name} | 页数: {max_pages}")
    if filter_desc:
        print(f"筛选: {' | '.join(filter_desc)}")
    print()

    # 契约 meta（规格 §3.2）：实际翻页数与异常提示，随每次写盘落文件
    actual_pages = 0
    warnings = []

    tid, sid = create_page_session(cdp)

    def human_scroll(cdp, sid):
        """模拟人类滚动: 随机次数、随机距离、随机停顿，偶尔回滚一点"""
        total_scrolls = random.randint(3, 6)
        for i in range(total_scrolls):
            # 大部分往下滚，偶尔往上回滚一点（模拟阅读回看）
            if random.random() < 0.15:
                delta = -random.randint(50, 150)
            else:
                delta = random.randint(150, 500)
            cdp.eval_js(f"window.scrollBy(0,{delta})", sid)
            # 滚动间隔随机：有时快速连续滚，有时停下来"看"
            if random.random() < 0.3:
                time.sleep(random.uniform(2.0, 4.0))
            else:
                time.sleep(random.uniform(0.5, 1.5))

    def human_mouse_jitter(cdp, sid):
        """偶尔移动鼠标位置，模拟人在页面上活动"""
        if random.random() < 0.4:
            x = random.randint(100, 800)
            y = random.randint(100, 600)
            cdp.send("Input.dispatchMouseEvent", {
                "type": "mouseMoved", "x": x, "y": y
            }, sid)

    try:
        for pg in range(1, max_pages + 1):
            # 并发熔断检查：其他任务已广播风控 → 本任务立即全停（不降并发续跑）
            if is_scrape_lock_risk():
                print("⚠️ 并发任务已触发风控熔断，本任务立即停止（保留已抓数据）。")
                warnings.append("并发任务风控熔断")
                print(f"EXPORT_FAIL reason=risk_blocked city={city_name} keyword={keyword}")
                return {"keyword": keyword, "city": city_name,
                        "total": len(all_jobs), "jobs": all_jobs}
            actual_pages = pg
            print(f"--- [{pg}/{max_pages} 页, {len(all_jobs)} 条已抓] ---")
            incr_request()

            # 第一页：导航到搜索页建立 cookie/session
            if pg == 1:
                url = build_search_url(keyword, city_code, pg, filters)
                cdp.send("Page.navigate", {"url": url}, sid)
                time.sleep(random.uniform(6, 10))
                # 页面级风控检测：滑块/验证页/登录墙命中时提示人工介入
                is_risk, reason = classify_risk_page(probe_risk_page(cdp, sid))
                if is_risk:
                    print(f"⚠️ 搜索页 {reason}，等待人工处理...")
                    set_scrape_lock_risk()  # 并发熔断广播：其他任务全停
                    if not wait_for_risk_clear(cdp, sid):
                        print("列表页风控未解除，停止抓取（保留已抓数据）。")
                        warnings.append(f"搜索页风控未解除: {reason}")
                        print(f"EXPORT_FAIL reason=risk_blocked city={city_name} keyword={keyword}")
                        return {"keyword": keyword, "city": city_name,
                                "total": len(all_jobs), "jobs": all_jobs}
                human_scroll(cdp, sid)
                human_mouse_jitter(cdp, sid)

            # 优先用 API 获取明文数据；失败时刷新页面重试（凭证自愈），
            # 让浏览器重新完成挑战/刷新 stoken，最多 API_ATTEMPT_LIMIT 次。
            api_params = {
                "scene": "1",
                "query": keyword,
                "city": city_code,
                "page": pg,
                "pageSize": 30,
            }
            for k, v in filters.items():
                if v:
                    api_params[k] = v
            api_url = f"{API_JOB_LIST_PATH}?{urlencode(api_params)}"

            jobs = []
            for attempt in range(API_ATTEMPT_LIMIT):
                api_js = FETCH_API_JS_TEMPLATE.replace("__API_URL__", api_url)
                val = cdp.eval_js(api_js, sid)
                jobs = parse_api_jobs_eval_value(val)
                if jobs:
                    break
                if attempt < API_ATTEMPT_LIMIT - 1:
                    print(f"  ⚠️ API 第 {attempt + 1} 次未返回数据，刷新页面重试（凭证自愈）...")
                    warnings.append(f"第{pg}页API未返回数据，已刷新重试")
                    cdp.send("Page.navigate",
                             {"url": build_search_url(keyword, city_code, 1, filters)}, sid)
                    time.sleep(random.uniform(6, 10))
                    is_risk, reason = classify_risk_page(probe_risk_page(cdp, sid))
                    if is_risk:
                        print(f"⚠️ 刷新后 {reason}，等待人工处理...")
                        if not wait_for_risk_clear(cdp, sid):
                            print("风控未解除，停止抓取（保留已抓数据）。")
                            warnings.append(f"刷新后风控未解除: {reason}")
                            jobs = []
                            break

            # DOM 提取的薪资可能是加密字体，默认禁用；只有显式允许时才降级。
            if should_use_dom_fallback(jobs, allow_dom_fallback):
                warnings.append(f"第{pg}页API获取失败，回退DOM提取（数据可能不完整）")
                log.warning("⚠️ API 获取失败，回退到 DOM 提取（此方式已弃用，数据可能不完整）")
                if pg > 1:
                    url = build_search_url(keyword, city_code, pg, filters)
                    cdp.send("Page.navigate", {"url": url}, sid)
                    time.sleep(random.uniform(4, 8))
                    human_scroll(cdp, sid)
                val = cdp.eval_js(EXTRACT_LIST_JS, sid)
                if val:
                    try:
                        jobs = json.loads(val) if isinstance(val, str) else val
                    except (json.JSONDecodeError, ValueError):
                        print("  ⚠️ JSON 解析失败")
                        jobs = []
            elif not jobs:
                log.warning("⚠️ API 未返回职位数据，已跳过 DOM fallback；如需强制降级可加 --allow-dom-fallback")

            if not jobs:
                print("  ⚠️ 无数据")
                continue

            new = 0
            for j in jobs:
                key = j.get('job_link') or j['title']
                j['job_id'] = hashlib.md5(key.encode()).hexdigest()[:16]
                if key in seen:
                    continue
                seen.add(key)
                all_jobs.append(j)
                new += 1
                salary = j.get('salary','?')
                scale = j.get('company_scale', '')
                active = j.get('boss_active_status', '')
                extra = f" | {scale}" if scale else ""
                if active:
                    extra += f" | {active}"
                print(f"  ✓ {j['title']} | {salary} | {j.get('location','')} | {j.get('boss_name','')}{extra}")

            print(f"  本页 {len(jobs)} 条, 新增 {new}, 累计 {len(all_jobs)}")

            # 每页抓完就写入文件，异常退出也能保留
            if output_path:
                flush_jobs(output_path, {
                    "keyword": keyword,
                    "city": city_name,
                    "filters": filters,
                    "filter_desc": filter_desc,
                    "scraped_at": datetime.now().isoformat(),
                    "page_count": pg,
                    "warnings": warnings,
                }, all_jobs)

            # 条数上限：抓够即停，不再翻页（BOSS 每页 30 条，实际可能略超上限）
            if max_jobs and len(all_jobs) >= max_jobs:
                print(f"  已抓 {len(all_jobs)} 条 ≥ 目标 {max_jobs}，停止翻页")
                break

            if pg < max_pages:
                # 并发 >1 时页间隔自动拉长（规格 §3.6 修订：12-22s → 20-30s）
                if max_concurrent > 1:
                    d = random.uniform(20, 30)
                else:
                    d = random.uniform(12, 22)
                print(f"  翻页等待 {d:.0f}s...\n")
                # 长等待期间分片检查熔断广播（其他任务风控 → 立即停，不等到翻页完成）
                for _ in range(4):
                    if is_scrape_lock_risk():
                        print("⚠️ 并发任务已触发风控熔断，本任务立即停止（保留已抓数据）。")
                        warnings.append("并发任务风控熔断")
                        print(f"EXPORT_FAIL reason=risk_blocked city={city_name} keyword={keyword}")
                        return {"keyword": keyword, "city": city_name,
                                "total": len(all_jobs), "jobs": all_jobs}
                    time.sleep(d / 4)

    except KeyboardInterrupt:
        print("\n中断")
    except RuntimeError as e:
        print(f"\n⚠️ {e}")
    finally:
        cdp.send("Target.closeTarget", {"targetId": tid})
        cdp.close()
        release_scrape_lock()

    print(f"\n{'='*60}")
    print(f"完成: {len(all_jobs)} 条")

    if all_jobs:
        # 最终写入（含时间戳更新）
        flush_jobs(output_path, {
            "keyword": keyword,
            "city": city_name,
            "filters": filters,
            "filter_desc": filter_desc,
            "scraped_at": datetime.now().isoformat(),
            "page_count": actual_pages or 1,
            "warnings": warnings,
        }, all_jobs)
        print(f"已保存: {output_path}")

        # CSV 导出
        if fmt == "csv":
            csv_path = output_path.rsplit(".", 1)[0] + ".csv"
            write_csv(csv_path, all_jobs)
    else:
        print("无数据")

    # AS-8 结构化结果行（规格 §3.4）：供下游程序/人 30 秒判断本次导出可信度
    print(f"EXPORT_OK jobs={len(all_jobs)} city={city_name} keyword={keyword} path={output_path}")
    return {"keyword": keyword, "city": city_name, "total": len(all_jobs), "jobs": all_jobs}


# ============================================================
# 抓取详情
# ============================================================
def build_detail_record(job, extracted):
    link = job.get("job_link", "")
    boss_active_status = resolve_boss_active_status(
        list_status=job.get("boss_active_status", ""),
        detail_status=extracted.get("boss_active_status", ""),
    )
    return {
        "job_id": job.get("job_id", ""),
        "title": job.get("title", ""),
        "company": job.get("boss_name", ""),
        "salary": job.get("salary", ""),
        "salary_source": job.get("salary_source", ""),
        "location": job.get("location", ""),
        "boss_active_status": boss_active_status,
        "tags_list": job.get("tags", ""),
        "job_link": link,
        "link": link,
        "skill_tags": extracted.get("tags", []),
        "jd": extracted.get("jd", ""),
    }


def eval_detail_with_retry(ws, sid, detail_url, retries=1):
    """提取详情页 JS；完全为空时刷新页面重试（凭证自愈）。

    Returns:
        解析后的 dict（{jd, page_text, tags, url}）；全部失败返回 {"jd": "", "tags": []}。
    """
    def _extract_once():
        val = ws.eval_js(EXTRACT_DETAIL_JS, sid)
        try:
            return json.loads(val) if isinstance(val, str) else {"jd": "", "tags": []}
        except (json.JSONDecodeError, ValueError, TypeError):
            return {"jd": "", "tags": []}

    d = _extract_once()
    for _ in range(max(retries, 0)):
        if d.get("jd") or d.get("page_text"):
            break
        print("  ⚠️ 提取为空，刷新页面重试一次...")
        ws.send("Page.navigate", {"url": detail_url}, sid)
        time.sleep(random.uniform(5, 10))
        d = _extract_once()
    return d


def load_existing_detail_ids(output_path=None):
    """读取已有详情文件中的 job_id 集合，用于跨运行跳过已抓详情。

    Returns:
        set: 已有 job_id；文件不存在或损坏返回空集合。
    """
    if not output_path or not os.path.exists(output_path):
        return set()
    try:
        with open(output_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError, ValueError):
        return set()
    items = data if isinstance(data, list) else []
    return {d.get("job_id", "") for d in items if isinstance(d, dict) and d.get("job_id")}


def pending_path_for(output_path):
    """待重试详情 job_id 清单文件路径（与输出文件同目录）。"""
    return f"{output_path}.pending.json"


def load_pending_ids(output_path, force_ids=None):
    """读取待重试详情 job_id → 已重试次数映射。

    兼容旧格式（纯字符串 job_id 列表，计数归零）；达到重试上限的
    job 直接放弃（不返回），避免永久失败的短 JD 反复消耗请求。

    Args:
        output_path: 详情输出路径（pending 文件与其同目录）
        force_ids: 用户强制重试白名单（iterable of str）；名单内 job_id
            无视重试次数上限，文件中未记录的也会加入返回结果

    Returns:
        dict: {job_id: attempts}
    """
    force = {str(j).strip() for j in (force_ids or []) if str(j).strip()}
    path = pending_path_for(output_path)
    if not os.path.exists(path):
        return {jid: 0 for jid in force}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError, ValueError):
        return {jid: 0 for jid in force}
    if not isinstance(data, list):
        return {jid: 0 for jid in force}

    pending = {}
    for item in data:
        if isinstance(item, str):
            job_id, attempts = item.strip(), 0
        elif isinstance(item, dict):
            job_id = str(item.get("job_id") or "").strip()
            try:
                attempts = int(item.get("attempts") or 0)
            except (TypeError, ValueError):
                attempts = 0
        else:
            continue
        if job_id and (attempts < MAX_PENDING_RETRIES or job_id in force):
            pending[job_id] = attempts
    for jid in force:
        if jid not in pending:
            pending[jid] = 0
    return pending


def save_pending_ids(output_path, ids):
    """原子写回待重试详情（job_id → 重试次数）；空映射时删除文件。

    Args:
        ids: {job_id: attempts} 映射
    """
    path = pending_path_for(output_path)
    if not ids:
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass
        return
    _atomic_write_json(path, [
        {"job_id": job_id, "attempts": attempts}
        for job_id, attempts in sorted(ids.items())
    ])


# ============================================================
# --verify 结果文件校验
# ============================================================
def _classify_result_file(name):
    """按文件名分类结果目录条目：jobs / details / pending / other。"""
    if name.endswith(".pending.json"):
        return "pending"
    if name.startswith("boss_jobs_"):
        return "jobs"
    if name.startswith("boss_details_"):
        return "details"
    return "other"


def list_results(result_dir=DEFAULT_RESULT_DIR):
    """列出结果目录中的结果文件（按修改时间倒序）。

    Returns:
        list of dict: {"path", "kind", "size", "modified"}
    """
    entries = []
    try:
        names = os.listdir(result_dir)
    except OSError:
        return entries
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(result_dir, name)
        kind = _classify_result_file(name)
        try:
            st = os.stat(path)
        except OSError:
            continue
        entries.append({
            "path": path, "kind": kind,
            "size": st.st_size, "modified": st.st_mtime,
        })
    entries.sort(key=lambda e: e["modified"], reverse=True)
    return entries


def archive_results(result_dir=DEFAULT_RESULT_DIR, keep_latest=1, archive_dir=None):
    """归档结果目录中的历史结果文件。

    jobs 与 details 各自保留最新的 keep_latest 个，其余移到 archive 子目录；
    pending 是断点续抓的活动文件，不归档；CSV 不移动。

    Args:
        result_dir: 结果目录
        keep_latest: 每个类型保留的最新文件数
        archive_dir: 归档目录（默认 result_dir/archive）

    Returns:
        int: 归档的文件数
    """
    archive_dir = archive_dir or os.path.join(result_dir, "archive")
    moved = 0
    for kind in ("jobs", "details"):
        candidates = [e for e in list_results(result_dir) if e["kind"] == kind]
        for entry in candidates[keep_latest:]:
            os.makedirs(archive_dir, exist_ok=True)
            dst = os.path.join(archive_dir, os.path.basename(entry["path"]))
            shutil.move(entry["path"], dst)
            moved += 1
            print(f"  📦 归档 {os.path.basename(entry['path'])}")
    return moved


def run_list_results(result_dir=DEFAULT_RESULT_DIR):
    """打印 --list-results 报告并返回退出码。"""
    entries = list_results(result_dir)
    if not entries:
        print(f"结果目录为空: {result_dir}")
        return 0
    print(f"\n=== 抓取结果文件（{len(entries)} 个）===")
    for e in entries:
        modified = datetime.fromtimestamp(e["modified"]).strftime("%Y-%m-%d %H:%M")
        print(f"  [{e['kind']:>7}] {modified}  {e['size']:>9} B  {os.path.basename(e['path'])}")
    print()
    return 0


def run_archive(result_dir=DEFAULT_RESULT_DIR, keep_latest=1):
    """执行 --archive 并返回退出码。"""
    print(f"\n=== 归档历史结果（保留每个类型最新 {keep_latest} 个）===")
    moved = archive_results(result_dir, keep_latest=keep_latest)
    if moved == 0:
        print("  ℹ️  无需归档")
    else:
        print(f"  ✅ 已归档 {moved} 个文件到 {os.path.join(result_dir, 'archive')}")
    print()
    return 0


# ============================================================
# --batch 批量任务编排
# ============================================================
FILTER_KEYS = ["scale", "stage", "salary", "experience", "degree", "industry"]


def load_batch_config(path):
    """加载 --batch 任务配置（JSON 数组）；返回 (tasks, errors)。

    每个任务支持字段：keyword(必填)、city(默认上海)、pages(默认 3，
    自动限制在 1..MAX_PAGES)、sleep(任务间等待秒数，缺省随机 30-60)、
    scale/stage/salary/experience/degree/industry（筛选）。非法任务
    跳过并记录错误，不中断其余任务。

    Args:
        path: 配置文件路径

    Returns:
        (list, list): 合法任务列表与错误信息列表
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError) as e:
        return [], [f"配置文件无法读取: {e}"]
    if not isinstance(data, list):
        return [], ["配置必须是 JSON 数组（每个元素一个任务）"]

    tasks = []
    errors = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            errors.append(f"任务 {i + 1}: 不是对象，已跳过")
            continue
        keyword = str(item.get("keyword") or "").strip()
        if not keyword:
            errors.append(f"任务 {i + 1}: 缺少 keyword，已跳过")
            continue
        try:
            pages = int(item.get("pages") or 3)
        except (TypeError, ValueError):
            errors.append(f"任务 {i + 1}: pages 必须是整数，已跳过")
            continue
        task = {
            "keyword": keyword,
            "city": str(item.get("city") or DEFAULT_CITY_INPUT).strip(),
            "pages": min(max(pages, 1), MAX_PAGES),
        }
        if item.get("sleep") is not None:
            try:
                task["sleep"] = max(float(item["sleep"]), 0)
            except (TypeError, ValueError):
                errors.append(f"任务 {i + 1}: sleep 必须是数字，已忽略该字段")
        for key in FILTER_KEYS:
            if item.get(key):
                task[key] = item[key]
        tasks.append(task)
    return tasks, errors


def run_batch(config_path, cdp_port=DEFAULT_CDP_PORT, max_concurrent=1):
    """逐任务执行批量列表抓取；任务间按 sleep（缺省随机 30-60s）防风控。

    支持 --max-concurrent 透传：多个 batch 进程并行时锁允许多个持有者
    （如 2 个 batch 各跑一半任务 + max_concurrent=2 即并发 2）。

    Returns:
        int: 退出码（0 全成功 / 1 有任务失败或配置错误）
    """
    tasks, errors = load_batch_config(config_path)
    for err in errors:
        print(f"⚠️  {err}")
    if not tasks:
        print("❌ 没有可执行的批量任务")
        return 1

    print(f"\n=== 批量列表抓取（{len(tasks)} 个任务，max_concurrent={max_concurrent}）===")
    failed = 0
    for i, task in enumerate(tasks):
        filters = {k: task[k] for k in FILTER_KEYS if k in task}
        print(f"\n[{i + 1}/{len(tasks)}] {task['keyword']} @ {task['city']} "
              f"（{task['pages']} 页）")
        try:
            scrape_list(
                task["keyword"], task["city"], task["pages"], filters, None,
                cdp_port=cdp_port, max_jobs=None,
                max_concurrent=max_concurrent,
            )
        except Exception as e:  # 有意宽捕：任务级隔离，单个任务失败不中断整个批量
            failed += 1
            print(f"  ❌ 任务失败: {e}")
        if i < len(tasks) - 1:
            gap = task.get("sleep") or random.uniform(30, 60)
            print(f"任务间等待 {gap:.0f}s 防风控...")
            time.sleep(gap)
    print(f"\n✅ 批量任务完成：成功 {len(tasks) - failed}/{len(tasks)}")
    return 0 if failed == 0 else 1


def latest_results_file(kind):
    """默认结果目录下最新文件（kind: "jobs" / "details"）；目录缺失返回 None。"""
    prefix = "boss_jobs_" if kind == "jobs" else "boss_details_"
    candidates = []
    try:
        for name in os.listdir(DEFAULT_RESULT_DIR):
            if name.startswith(prefix) and name.endswith(".json"):
                path = os.path.join(DEFAULT_RESULT_DIR, name)
                candidates.append((os.path.getmtime(path), path))
    except OSError:
        return None
    return max(candidates)[1] if candidates else None


def _latest_details_path(list_path):
    """自动查找与列表同目录的详情文件：同时间戳优先，其次最新。"""
    base = os.path.dirname(list_path) or "."
    stem = os.path.basename(list_path)
    if stem.startswith("boss_jobs_"):
        stamp = stem[len("boss_jobs_"):]
        same_stamp = os.path.join(base, f"boss_details_{stamp}")
        if os.path.exists(same_stamp):
            return same_stamp
    candidates = []
    try:
        for name in os.listdir(base):
            if name.startswith("boss_details_") and name.endswith(".json") \
                    and not name.endswith(".pending.json"):
                path = os.path.join(base, name)
                candidates.append((os.path.getmtime(path), path))
    except OSError:
        return None
    return max(candidates)[1] if candidates else None


def verify_results(list_path, details_path=None):
    """校验已抓取结果文件完整性。

    检查：列表/详情 JSON 可解析性、job 必备字段（job_id/title）、重复
    job_id、详情 JD 完整度（短于 MIN_DETAIL_TEXT_LENGTH 视为残缺）、
    列表-详情覆盖率。

    Args:
        list_path: boss_jobs_*.json 路径
        details_path: boss_details_*.json 路径；不传则自动查找
            （同时间戳优先，其次最新）

    Returns:
        dict: {"ok", "issues", "list": {"count"}, "details": {"count"},
               "detail_path", "coverage", "missing"}
    """
    issues = []

    list_count = 0
    list_ids = set()
    if not os.path.exists(list_path):
        issues.append(f"列表文件不存在: {list_path}")
        list_data = None
    else:
        try:
            with open(list_path, "r", encoding="utf-8") as f:
                list_data = json.load(f)
        except (json.JSONDecodeError, OSError, ValueError) as e:
            issues.append(f"列表文件无法解析: {e}")
            list_data = None
    if list_data is not None:
        jobs = list_data.get("jobs") if isinstance(list_data, dict) else None
        if not isinstance(jobs, list) or not jobs:
            issues.append("列表没有职位数据（jobs 为空或缺失）")
        else:
            list_count = len(jobs)
            seen = set()
            missing_fields = 0
            dup = set()
            for job in jobs:
                if not isinstance(job, dict):
                    continue
                jid = str(job.get("job_id") or "").strip()
                if not jid or not str(job.get("title") or "").strip():
                    missing_fields += 1
                if jid:
                    if jid in seen:
                        dup.add(jid)
                    seen.add(jid)
                    list_ids.add(jid)
            if missing_fields:
                issues.append(f"列表有 {missing_fields} 条记录缺少 job_id 或 title")
            if dup:
                issues.append(f"列表存在重复 job_id: {', '.join(sorted(dup)[:5])}")
            if not list_ids:
                issues.append("列表没有有效的 job_id，无法与详情匹配")

    if details_path is None:
        details_path = _latest_details_path(list_path)
    detail_count = 0
    detail_ids = set()
    if details_path is None or not os.path.exists(details_path):
        issues.append("未找到详情文件（可用 --detail-output 指定路径）")
        details = None
    else:
        try:
            with open(details_path, "r", encoding="utf-8") as f:
                details = json.load(f)
        except (json.JSONDecodeError, OSError, ValueError) as e:
            issues.append(f"详情文件无法解析: {e}")
            details = None
    if details is not None:
        if not isinstance(details, list) or not details:
            issues.append("详情文件没有数据（空列表）")
        else:
            detail_count = len(details)
            seen = set()
            dup = set()
            short_jd = 0
            for d in details:
                if not isinstance(d, dict):
                    continue
                jid = str(d.get("job_id") or "").strip()
                if jid:
                    if jid in seen:
                        dup.add(jid)
                    seen.add(jid)
                    detail_ids.add(jid)
                if len(str(d.get("jd") or "")) < MIN_DETAIL_TEXT_LENGTH:
                    short_jd += 1
            if dup:
                issues.append(f"详情存在重复 job_id: {', '.join(sorted(dup)[:5])}")
            if short_jd:
                issues.append(f"详情有 {short_jd} 条 JD 过短（<{MIN_DETAIL_TEXT_LENGTH} 字），可能抓取残缺")

    missing = sorted(list_ids - detail_ids) if list_ids else []
    coverage = (len(list_ids) - len(missing)) / len(list_ids) if list_ids else 0.0
    if list_ids and coverage < 1.0:
        shown = ", ".join(missing[:5])
        more = f" 等 {len(missing)} 条" if len(missing) > 5 else ""
        issues.append(f"详情覆盖率 {coverage:.0%}，缺失 {len(missing)} 条: {shown}{more}")

    return {
        "ok": not issues,
        "issues": issues,
        "list": {"count": list_count},
        "details": {"count": detail_count},
        "detail_path": details_path,
        "coverage": coverage,
        "missing": missing,
    }


def run_verify(list_path, details_path=None):
    """打印 --verify 报告并返回退出码（0 全过 / 1 有问题）。"""
    print("\n=== 校验抓取结果文件 ===")
    print(f"列表: {list_path}")
    report = verify_results(list_path, details_path)
    print(f"详情: {report['detail_path'] or '未指定'}")
    print(f"列表 {report['list']['count']} 条 / 详情 {report['details']['count']} 条 "
          f"/ 覆盖率 {report['coverage']:.0%}")
    if not report["issues"]:
        print("✅ 校验通过：文件完整、字段齐全、详情覆盖列表")
    else:
        for i in report["issues"]:
            print(f"  ❌ {i}")
    print()
    return 0 if report["ok"] else 1


def progress_step(total):
    """阶段进度汇报粒度：总量 ≤200 条每 10 条一报，超 200 后按 5%（取整）。

    Args:
        total: 待处理总数

    Returns:
        int: 汇报间隔（条数，至少 10）
    """
    return max(10, int(math.ceil(total * 0.05)))


def resume_hint(pending, output_path):
    """生成断点续抓提示；pending 为空返回空串。

    详情输出路径不变时，pending 文件会被自动加载，重跑原命令即自动
    跳过已抓、只补失败详情，无需拼写任何参数。

    Args:
        pending: {job_id: attempts} 待重试映射
        output_path: 详情输出路径

    Returns:
        str: 提示文本（可能为空串）
    """
    if not pending:
        return ""
    path = pending_path_for(output_path)
    return (f"ℹ️  {len(pending)} 个详情待重试（已记录到 {path}）。"
            f"续抓：重跑刚才的命令即可——输出路径不变时自动跳过已抓、"
            f"只补这 {len(pending)} 条")


def _format_elapsed(seconds):
    """耗时格式化：≥60 秒显示"X 分 Y 秒"，否则"Y 秒"。"""
    seconds = int(seconds)
    if seconds >= 60:
        return f"{seconds // 60} 分 {seconds % 60} 秒"
    return f"{seconds} 秒"


def run_summary(elapsed_sec, total, ok_count, reason_counts):
    """生成详情抓取结束统计行；无任务返回空串。

    Args:
        elapsed_sec: 已耗时（秒）
        total: 处理总数
        ok_count: 成功数
        reason_counts: 失败原因分类 {reason: count}（不含成功）

    Returns:
        str: 统计行（可能为空串）
    """
    if total <= 0:
        return ""
    failed = total - ok_count
    reason_txt = "，".join(
        f"{k}:{v}" for k, v in sorted(reason_counts.items())) if reason_counts else "—"
    rate = math.ceil(elapsed_sec / total)
    return (f"  ✅ 完成 {total} 条：成功 {ok_count}，失败 {failed}（{reason_txt}）"
            f"| 耗时 {_format_elapsed(elapsed_sec)}，平均 {rate}s/条")


def progress_line(completed, total, ok_count):
    """生成阶段进度汇总行；未到汇报点返回 None（避免刷屏）。

    每 progress_step(total) 条汇报一次，完成时（completed == total）
    即使不整除也汇报，保证任务结束有最终汇总。

    Args:
        completed: 已处理条数
        total: 总数
        ok_count: 成功条数

    Returns:
        str|None: 汇总行文本
    """
    if total <= 0:
        return None
    if completed % progress_step(total) != 0 and completed != total:
        return None
    pct = completed / total * 100
    return f"  [进度 {completed}/{total} {pct:.0f}%] 成功 {ok_count}，失败 {completed - ok_count}"


class TokenBucket:
    """线程安全的全局速率限制令牌桶。

    容量 capacity 内可突发；令牌按 rate（个/秒）持续补充，
    耗尽时 acquire 阻塞直到令牌补充（并发详情抓取的全局限速用）。
    """
    def __init__(self, rate, capacity):
        self.rate = rate
        self.capacity = capacity
        self._tokens = float(capacity)
        self._last_refill = time.time()
        self._lock = threading.Lock()

    def _refill(self, now):
        elapsed = now - self._last_refill
        if elapsed > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
            self._last_refill = now

    def acquire(self):
        with self._lock:
            self._refill(time.time())
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            deficit = (1.0 - self._tokens) / self.rate
        time.sleep(deficit)
        with self._lock:
            self._last_refill = time.time()
            self._tokens = max(0.0, self._tokens - 1.0)


class AdaptiveRateLimiter:
    """全局限速 + 错误率自适应（Scrapy AutoThrottle 思想）。

    - 滑动窗口（默认 60s）内失败率 > failure_threshold → 速率降半
    - 连续 2 个坏窗口 → acquire 暂停 pause_seconds（等风控窗口过去）
    - 健康窗口 → 速率恢复基线
    """
    def __init__(self, base_rate, capacity=None, window=60.0,
                 failure_threshold=0.3, pause_seconds=60.0):
        self.base_rate = base_rate
        self.capacity = capacity or max(int(base_rate), 1)
        self.window = window
        self.failure_threshold = failure_threshold
        self.pause_seconds = pause_seconds
        self._halved = False
        self._consecutive_bad = 0
        self._window_start = time.time()
        self._window_total = 0
        self._window_failures = 0
        self._bucket = TokenBucket(base_rate, self.capacity)
        self._lock = threading.Lock()

    def current_rate(self):
        """当前生效速率（降半后的值）。"""
        return self.base_rate / 2.0 if self._halved else self.base_rate

    def record_success(self):
        """记录一次成功请求。"""
        self._record(failed=False)

    def record_failure(self):
        """记录一次失败请求。"""
        self._record(failed=True)

    def _record(self, failed):
        with self._lock:
            now = time.time()
            if now - self._window_start >= self.window:
                self._roll_window()
            self._window_total += 1
            if failed:
                self._window_failures += 1

    def _roll_window(self):
        """窗口推进：按失败率调整速率与连续坏窗口计数。"""
        if self._window_total > 0:
            failure_rate = self._window_failures / self._window_total
            if failure_rate > self.failure_threshold:
                self._halved = True
                self._consecutive_bad += 1
            else:
                self._halved = False
                self._consecutive_bad = 0
        self._window_start = time.time()
        self._window_total = 0
        self._window_failures = 0

    def acquire(self):
        """申请一个请求配额；连续坏窗口时先暂停，再走令牌桶。"""
        if self._consecutive_bad >= 2:
            time.sleep(self.pause_seconds)
            self._consecutive_bad = 0
        self._bucket.rate = self.current_rate()
        self._bucket.acquire()


def _scrape_one_detail(job, cdp_port=DEFAULT_CDP_PORT, stop_event=None,
                       limiter=None, verbose=False):
    """抓取单个岗位详情（串行/并发共用的 worker 单元，不写盘、不管理 pending）。

    Args:
        job: 列表 job dict（含 job_link / job_id / title 等）
        cdp_port: CDP 端口
        stop_event: 可选 threading.Event；置位时提前返回（登录墙/熔断等
            全局停止信号），不再发起新会话
        limiter: 可选 AdaptiveRateLimiter；并发模式下在导航前申请全局配额

    Returns:
        dict: {"ok": bool, "detail": dict|None, "job_id": str,
               "reason": str, "message": str}
        reason 取值: "" | "stopped" | "cdp_session" | "risk_timeout"
                   | "invalid_detail" | "login_required"
    """
    job_id = job.get("job_id", "")
    if stop_event is not None and stop_event.is_set():
        return {"ok": False, "detail": None, "job_id": job_id,
                "reason": "stopped", "message": "已收到停止信号"}

    ws = None
    tid = None
    try:
        ws = CDPSession(cdp_port)
        tid, sid = create_page_session(ws)

        detail_url = build_detail_url(job)
        # 并发模式全局限速：导航（真实请求）前申请配额
        if limiter is not None:
            limiter.acquire()
        ws.send("Page.navigate", {"url": detail_url}, sid)
        if verbose:
            print("  加载页面...")
        time.sleep(random.uniform(5, 10))

        # 页面级风控检测：滑块/验证页/登录墙命中时等待人工介入
        is_risk, risk_reason = classify_risk_page(probe_risk_page(ws, sid))
        if is_risk and not wait_for_risk_clear(ws, sid):
            return {"ok": False, "detail": None, "job_id": job_id,
                    "reason": "risk_timeout", "message": risk_reason}

        # 模拟人类阅读详情页的滚动行为
        scroll_count = random.randint(3, 7)
        if verbose:
            print(f"  模拟滚动 ({scroll_count} 次)...")
        for _ in range(scroll_count):
            if stop_event is not None and stop_event.is_set():
                return {"ok": False, "detail": None, "job_id": job_id,
                        "reason": "stopped", "message": "已收到停止信号"}
            if random.random() < 0.12:
                delta = -random.randint(80, 200)
            else:
                delta = random.randint(200, 600)
            ws.eval_js(f"window.scrollBy(0,{delta})", sid)
            if random.random() < 0.35:
                time.sleep(random.uniform(2.0, 5.0))
            else:
                time.sleep(random.uniform(0.8, 1.8))

        # 偶尔模拟鼠标移动
        if random.random() < 0.5:
            ws.send("Input.dispatchMouseEvent", {
                "type": "mouseMoved",
                "x": random.randint(200, 800),
                "y": random.randint(200, 600)
            }, sid)
            time.sleep(random.uniform(0.5, 1.5))

        d = eval_detail_with_retry(ws, sid, detail_url)
        try:
            fields = extract_detail_fields(d)
            d["jd"] = fields["jd"]
            d["boss_active_status"] = fields["boss_active_status"]
        except DetailLoginRequiredError as exc:
            return {"ok": False, "detail": None, "job_id": job_id,
                    "reason": "login_required", "message": str(exc)}
        except DetailExtractionError as exc:
            return {"ok": False, "detail": None, "job_id": job_id,
                    "reason": "invalid_detail", "message": str(exc)}

        return {"ok": True, "detail": build_detail_record(job, d),
                "job_id": job_id, "reason": "", "message": ""}
    except _cdp_exception_types() as exc:
        return {"ok": False, "detail": None, "job_id": job_id,
                "reason": "cdp_session", "message": str(exc)}
    finally:
        if ws is not None:
            try:
                if tid is not None:
                    ws.send("Target.closeTarget", {"targetId": tid})
                ws.close()
            except _cdp_exception_types():
                log.debug("关闭详情会话失败", exc_info=True)


def scrape_details(list_data, max_details=None, output_path=None,
                   cdp_port=DEFAULT_CDP_PORT, fmt="json", pending_ids=None,
                   concurrency=DEFAULT_CONCURRENCY):
    jobs = list_data.get("jobs", [])
    if max_details:
        jobs = jobs[:max_details]
    if not output_path:
        output_path = default_output_path("details")

    print(f"\n=== 抓取岗位详情 ({len(jobs)} 个) ===\n")
    # 断点续抓：先加载已有结果文件，避免"跳过已抓 + 全量覆盖"把旧数据冲掉
    results = []
    if os.path.exists(output_path):
        try:
            with open(output_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                results = data
        except (json.JSONDecodeError, OSError, ValueError):
            log.warning(f"加载已有详情文件失败，从空开始: {output_path}")
    seen_links = set()
    # 历史 job_id 预加载：已抓过的详情直接跳过（省请求、降风控触发概率）
    existing_ids = load_existing_detail_ids(output_path)
    if existing_ids:
        print(f"ℹ️  已加载 {len(existing_ids)} 个历史详情 job_id，命中直接跳过")
    # 断点续跑：上次失败待重试的 job_id（即使已在结果文件里也重新抓取）
    pending = dict(pending_ids) if pending_ids is not None else load_pending_ids(output_path)
    if pending:
        print(f"ℹ️  {len(pending)} 个详情上次抓取失败，本次自动重试")

    # 并发模式（--concurrency > 1）：worker 只取数，主线程统一合并/渐进落盘/pending
    if concurrency > 1:
        print(f"⚡ 并发详情抓取（--concurrency {concurrency}，全局限速 + 错误率自适应降速）")
        results, pending = _scrape_details_parallel(
            jobs, cdp_port, concurrency,
            existing_ids=existing_ids, pending_ids=pending,
            existing_results=results, output_path=output_path)
        if pending:
            print(resume_hint(pending, output_path))
        print(f"\n详情已保存: {output_path}")
        if fmt == "csv":
            csv_path = output_path.rsplit(".", 1)[0] + ".csv"
            write_detail_csv(csv_path, results)
        return results

    consecutive_cdp_errors = 0
    serial_ok = 0
    serial_done = 0
    serial_reasons = {}
    start_time = time.time()

    for idx, job in enumerate(jobs):
        link = job.get("job_link", "")
        title = job.get("title", "")
        company = job.get("boss_name", "")
        job_id = job.get("job_id", "")
        if not link:
            continue

        # 按 link 去重
        if link in seen_links:
            print(f"[{idx+1}/{len(jobs)}] 跳过重复: {company} - {title}")
            continue
        seen_links.add(link)

        # 跨运行跳过已抓详情（pending 中的例外，待重试）
        if job_id and job_id in existing_ids and job_id not in pending:
            print(f"[{idx+1}/{len(jobs)}] 跳过已抓详情: {company} - {title}")
            continue

        t0 = time.time()
        print(f"[{idx+1}/{len(jobs)}] {company} - {title}")

        incr_request()

        result = _scrape_one_detail(job, cdp_port, verbose=True)
        reason = result["reason"]

        if result["ok"]:
            detail = result["detail"]
            results.append(detail)
            serial_ok += 1
            serial_done += 1
            # 抓取成功：从待重试清单移除
            if job_id:
                pending.pop(job_id, None)
            if detail.get("tags"):
                print(f"  技能: {', '.join(detail['tags'])}")
            if detail.get("boss_active_status"):
                print(f"  活跃: {detail['boss_active_status']}")
            print(f"  JD: {len(detail.get('jd',''))} 字 ({time.time()-t0:.0f}s)")
            # 每抓完一个详情就写入，异常退出也能保留
            if output_path:
                _atomic_write_json(output_path, results)
        elif reason == "login_required":
            raise RuntimeError(
                "BOSS detail login expired; stopped before writing truncated JD data"
            )
        elif reason == "cdp_session":
            consecutive_cdp_errors += 1
            serial_done += 1
            serial_reasons[reason] = serial_reasons.get(reason, 0) + 1
            print(f"  ⚠️ CDP 会话建立失败（连续 {consecutive_cdp_errors} 次）: {result['message']}")
            if job_id:
                pending[job_id] = pending.get(job_id, 0) + 1
                save_pending_ids(output_path, pending)
            if consecutive_cdp_errors >= MAX_CDP_CONSECUTIVE_ERRORS:
                print("❌ 连续 CDP 会话失败，判定浏览器会话异常，停止详情抓取。")
                print("   可运行 --stop-chrome 后重新 --setup-chrome 再继续（已抓数据保留，剩余自动重试）。")
                break
            continue
        elif reason == "stopped":
            break
        else:
            serial_done += 1
            serial_reasons[reason] = serial_reasons.get(reason, 0) + 1
            print(f"  跳过无效详情页: {result['message']}")
            if job_id:
                pending[job_id] = pending.get(job_id, 0) + 1
                save_pending_ids(output_path, pending)

        # 详情页间隔加大，随机 10-25 秒
        gap = random.uniform(10, 25)
        print(f"  等待 {gap:.0f}s 后抓下一个...\n")
        progress = progress_line(idx + 1, len(jobs), serial_ok)
        if progress:
            print(progress)
        time.sleep(gap)

    # 最终保存（dirname 为空时回退到当前目录，与循环内/其它写文件处保持一致）
    _atomic_write_json(output_path, results)
    save_pending_ids(output_path, pending)
    if serial_done:
        print(run_summary(time.time() - start_time,
                          serial_done, serial_ok, serial_reasons))
    if pending:
        print(resume_hint(pending, output_path))
    print(f"\n详情已保存: {output_path}")

    if fmt == "csv":
        csv_path = output_path.rsplit(".", 1)[0] + ".csv"
        write_detail_csv(csv_path, results)
    return results


def _scrape_details_parallel(jobs, cdp_port, concurrency, limiter=None,
                             existing_ids=None, pending_ids=None,
                             existing_results=None, output_path=None,
                             write_every=5):
    """并发详情抓取：worker 只取数，主线程统一合并、渐进写盘与 pending。

    Args:
        jobs: 列表 job dict 列表
        cdp_port: CDP 端口
        concurrency: 并发 worker 数
        limiter: 可选 AdaptiveRateLimiter（全局限速）；None 时按并发度自建
        existing_ids: 历史已抓 job_id 集合（跳过）
        pending_ids: 待重试 job_id 集合（优先重抓）
        existing_results: 已有详情列表（断点续抓），并入返回与落盘
        output_path: 非 None 时每 write_every 条渐进原子写盘（中断最多丢
            write_every 条，与串行"每条写盘"的可靠性差距收敛）
        write_every: 渐进写盘间隔（条数）

    Returns:
        (results, pending): results 为全量详情（含 existing_results）；
            pending 为待重试 job_id 集合
    """
    existing_ids = existing_ids if existing_ids is not None else set()
    pending = dict(pending_ids) if pending_ids is not None else {}
    if limiter is None:
        # 默认全局限速：令牌桶仅提供弱错峰（容量=并发，错开同时导航的瞬时
        # 突发）；实际请求间隔主要由每条详情固有的加载/滚动等待（约 20-30s）
        # 决定。失败率升高时由 AdaptiveRateLimiter 降半/暂停兜底。
        limiter = AdaptiveRateLimiter(base_rate=max(concurrency * 0.5, 0.5))

    # 过滤已抓/重复（与串行路径同一套去重逻辑）
    todo = []
    seen_links = set()
    for job in jobs:
        link = job.get("job_link", "")
        job_id = job.get("job_id", "")
        if not link or link in seen_links:
            continue
        seen_links.add(link)
        if job_id and job_id in existing_ids and job_id not in pending:
            continue
        todo.append(job)

    results = list(existing_results) if existing_results is not None else []
    stop_event = threading.Event()
    consecutive_cdp_errors = 0
    start_time = time.time()

    def persist():
        if output_path:
            _atomic_write_json(output_path, results)
            save_pending_ids(output_path, pending)

    def handle_result(job, result):
        # 仅主线程（as_completed 循环）调用，results/pending 无并发访问，无需加锁
        nonlocal consecutive_cdp_errors
        job_id = job.get("job_id", "")
        if result["ok"]:
            limiter.record_success()
            results.append(result["detail"])
            if job_id:
                pending.pop(job_id, None)
            return
        limiter.record_failure()
        reason = result["reason"]
        if reason == "login_required":
            stop_event.set()
        elif reason == "cdp_session":
            consecutive_cdp_errors += 1
            if consecutive_cdp_errors >= MAX_CDP_CONSECUTIVE_ERRORS:
                stop_event.set()
        if job_id:
            pending[job_id] = pending.get(job_id, 0) + 1
            if output_path:
                save_pending_ids(output_path, pending)

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        # 有界提交窗口：同时持有的在飞任务不超过 concurrency*2（背压），
        # 每完成一个补提交一个；停止信号（熔断/登录墙）后不再补提交。
        # 相比一次性提交全部：内存有界、停止即时生效（千级任务也安全）。
        window = max(concurrency * 2, 2)
        todo_iter = iter(todo)
        total = len(todo)
        in_flight = set()

        def run_one(job):
            return job, _scrape_one_detail(job, cdp_port, stop_event, limiter)

        def fill_window():
            while len(in_flight) < window:
                if stop_event.is_set():
                    return
                try:
                    job = next(todo_iter)
                except StopIteration:
                    return
                # 与串行路径一致：每个提交的详情计入全局请求预算（500 上限）
                incr_request()
                in_flight.add(pool.submit(run_one, job))

        fill_window()
        completed = 0
        parallel_ok = 0
        parallel_reasons = {}
        while in_flight:
            done, in_flight = wait(in_flight, return_when=FIRST_COMPLETED)
            for future in done:
                try:
                    job, result = future.result()
                except _cdp_exception_types() as exc:
                    result = {"ok": False, "detail": None,
                              "job_id": "", "reason": "cdp_session",
                              "message": str(exc)}
                handle_result(job, result)
                completed += 1
                if result["ok"]:
                    parallel_ok += 1
                else:
                    reason = result["reason"] or "unknown"
                    parallel_reasons[reason] = parallel_reasons.get(reason, 0) + 1
                mark = "✓" if result["ok"] else f"✗ {result['reason']}"
                print(f"  [并发 {completed}/{total}] {job.get('title', '')} {mark}")
                progress = progress_line(completed, total, parallel_ok)
                if progress:
                    print(progress)
                if output_path and completed % write_every == 0:
                    persist()
            fill_window()
        if completed:
            print(run_summary(time.time() - start_time,
                              completed, parallel_ok, parallel_reasons))
    if output_path:
        persist()
    return results, pending


# ============================================================
# 动态技术术语提取
# ============================================================
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


# ============================================================
# 分析报告
# ============================================================
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


def run_smoke_test(cdp_port=DEFAULT_CDP_PORT):
    """Run a real browser/API smoke test without writing result files."""
    if not require_runtime_dependencies("requests", "websocket"):
        return 1

    try:
        cdp = CDPSession(cdp_port)
        city_name, city_code = resolve_city(DEFAULT_CITY_INPUT)
        search_url = build_search_url(LOGIN_PROBE_QUERY, city_code, 1, {})
        tid, sid = create_page_session(cdp)

        print(f"打开 BOSS 搜索页: {LOGIN_PROBE_QUERY} @ {city_name}")
        cdp.send("Page.navigate", {"url": search_url}, sid)
        time.sleep(4)
        api_url = f"{API_JOB_LIST_PATH}?{urlencode({'scene': '1', 'query': LOGIN_PROBE_QUERY, 'city': city_code, 'page': 1, 'pageSize': 5})}"
        api_js = FETCH_API_JS_TEMPLATE.replace("__API_URL__", api_url)
        jobs = parse_api_jobs_eval_value(cdp.eval_js(api_js, sid))
        cdp.send("Target.closeTarget", {"targetId": tid})
        cdp.close()

        if has_usable_smoke_jobs(jobs):
            sample = next(job for job in jobs if job.get("salary") and job.get("job_link"))
            print(f"✅ Smoke test 通过: {sample.get('title')} | {sample.get('salary')}")
            return 0
        print("❌ Smoke test 未拿到可用职位；请检查登录态或 BOSS API 返回")
        return 1
    except (requests.ConnectionError, requests.Timeout, KeyError,
            json.JSONDecodeError, websocket.WebSocketException, TimeoutError) as e:
        print(f"❌ Smoke test 失败: {e}")
        return 1


# ============================================================
# --check 环境检查
# ============================================================
def run_check(cdp_port=DEFAULT_CDP_PORT):
    """运行环境诊断检查"""
    print("=" * 50)
    print("  BOSS直聘 CDP 环境检查")
    print("=" * 50)
    print()

    all_pass = True

    # 检查 1: Python 依赖
    print("[1/3] Python 依赖...")
    deps_ok = require_runtime_dependencies("websocket", "requests")
    if requests is not None:
        print("  ✅ requests 可导入")
    if websocket is not None:
        print("  ✅ websocket 可导入")
    if deps_ok:
        print("  ✅ 依赖完整")
    else:
        all_pass = False

    # 检查 2: CDP 端口连通性
    print("[2/3] CDP 端口连通性...")
    if requests is None:
        print("  ❌ 跳过 — 缺少 requests")
        all_pass = False
    else:
        try:
            resp = requests.get(f"http://127.0.0.1:{cdp_port}/json/version", timeout=5)
            data = resp.json()
            browser = data.get("Browser", "未知")
            print(f"  ✅ 通过 — CDP 服务: {browser}")
        except (requests.ConnectionError, requests.Timeout):
            print(f"  ❌ 失败 — 无法连接 127.0.0.1:{cdp_port}")
            print(f"     请先启动 Chrome CDP: {sys.executable} {__file__} --setup-chrome")
            all_pass = False
        except (json.JSONDecodeError, KeyError) as e:
            print(f"  ❌ 失败 — CDP 响应异常: {e}")
            all_pass = False

    # 检查 3: BOSS直聘登录状态
    print("[3/3] BOSS直聘登录状态...")
    if not deps_ok:
        print("  ❌ 跳过 — 缺少运行依赖")
        all_pass = False
    else:
        try:
            login_result = check_login_state(cdp_port)
            if login_result.status is LoginProbeStatus.AVAILABLE:
                print("  ✅ 已登录")
            elif login_result.status is LoginProbeStatus.EMPTY:
                print(f"  ⚠️  {describe_login_probe_result(login_result)}")
                all_pass = False
            else:
                print(f"  ❌ {describe_login_probe_result(login_result)}")
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


# ============================================================
# --setup-chrome 自动启动
# ============================================================
def prepare_cdp_profile(copy_login_state=False, reset=False):
    """Prepare an isolated persistent Chrome profile for CDP."""
    cdp_data_dir = DEFAULT_CDP_DATA_DIR
    cdp_default = os.path.join(cdp_data_dir, "Default")

    if reset and os.path.exists(cdp_data_dir):
        shutil.rmtree(cdp_data_dir)

    os.makedirs(cdp_default, exist_ok=True)

    copied = 0
    if copy_login_state:
        default_profile = DEFAULT_PROFILE_DIR
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
        resp = requests.get(f"http://127.0.0.1:{cdp_port}/json/version", timeout=2)
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
                capture_output=True, text=True, timeout=5,
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
            if not is_chrome_command(command):
                continue
            try:
                processes.append((int(item.get("ProcessId")), command))
            except (TypeError, ValueError):
                continue
        return processes

    try:
        r = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        # 进程枚举失败不阻塞主流程：调用方按"无进程"处理
        return []

    processes = []
    for line in r.stdout.splitlines():
        if not is_chrome_command(line):
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
    real_dir = normalize_profile_path(user_data_dir)
    for pid, command in iter_chrome_process_commands():
        if "--user-data-dir=" not in command:
            continue
        path = extract_user_data_dir(command)
        if path and normalize_profile_path(path) == real_dir:
            pids.append(pid)
    return pids


def chrome_user_data_dirs_for_cdp_port(cdp_port):
    """Return user-data-dir paths for Chrome processes using the given CDP port."""
    dirs = []
    port_arg = f"--remote-debugging-port={cdp_port}"
    for _pid, command in iter_chrome_process_commands():
        if port_arg not in command:
            continue
        path = extract_user_data_dir(command)
        if path:
            dirs.append(path)
    return dirs


def cdp_port_uses_profile(cdp_port, cdp_data_dir):
    expected = normalize_profile_path(cdp_data_dir)
    return any(normalize_profile_path(path) == expected for path in chrome_user_data_dirs_for_cdp_port(cdp_port))


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
    pids = chrome_pids_for_user_data_dir(cdp_data_dir)
    if not pids:
        return 0

    for pid in pids:
        try:
            terminate_process(pid, force=False)
        except ProcessLookupError:
            pass
    for _ in range(10):
        time.sleep(0.5)
        if not chrome_pids_for_user_data_dir(cdp_data_dir):
            return len(pids)

    for pid in chrome_pids_for_user_data_dir(cdp_data_dir):
        try:
            terminate_process(pid, force=True)
        except ProcessLookupError:
            pass
    time.sleep(0.5)
    return len(pids)


def wait_for_cdp(cdp_port, timeout=30):
    print("等待 CDP 可用", end="")
    for _ in range(timeout):
        time.sleep(1)
        print(".", end="", flush=True)
        if is_cdp_ready(cdp_port):
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


def run_setup_chrome(cdp_port=DEFAULT_CDP_PORT, copy_login_state=False,
                     reset_profile=False, wait_login=True,
                     login_timeout=DEFAULT_LOGIN_TIMEOUT):
    """自动配置并启动 Chrome CDP 模式"""
    if not require_runtime_dependencies("requests"):
        return 1

    print("=" * 50)
    print("  设置 Chrome CDP 调试模式")
    print("=" * 50)
    print()

    profile = prepare_cdp_profile(copy_login_state=copy_login_state, reset=reset_profile)
    cdp_data_dir = profile["path"]
    print(f"✅ 使用独立 Chrome profile: {cdp_data_dir}")
    if reset_profile:
        print("   已按 --reset-chrome-profile 重建 profile")
    if copy_login_state:
        print(f"   已复制 {profile['copied']} 个登录态文件（Local State + Cookie 相关文件）")
    else:
        print("   默认、首次启动、重复启动都不复制主 Chrome Cookie；首次使用请在此专用 Chrome 中登录 zhipin.com")

    if is_cdp_ready(cdp_port):
        if cdp_port_uses_profile(cdp_port, cdp_data_dir):
            print(f"\n✅ CDP 已就绪 (端口 {cdp_port})")
            if wait_login:
                return 0 if wait_for_login(cdp_port, timeout=login_timeout) else 1
            return 0
        print(f"\n❌ 端口 {cdp_port} 已被其他 Chrome CDP profile 占用")
        print("   请关闭旧 CDP Chrome，或改用 --cdp-port 指定其他端口")
        return 1

    stopped = stop_cdp_chrome(cdp_data_dir)
    if stopped:
        print(f"\n已关闭 {stopped} 个旧的 BOSS CDP Chrome 进程")

    print(f"\n启动 Chrome (CDP 端口: {cdp_port})...")
    cmd = [
        DEFAULT_CHROME_PATH,
        f"--remote-debugging-port={cdp_port}",
        f"--user-data-dir={cdp_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--remote-allow-origins=*",
    ]
    launch_chrome(cmd)

    if not wait_for_cdp(cdp_port):
        return 1

    print()
    print("Chrome 已启动。请在这个专用浏览器中登录 zhipin.com。")
    if wait_login:
        print()
        if not wait_for_login(cdp_port, timeout=login_timeout):
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
    if not require_runtime_dependencies("requests"):
        return 1

    print("=" * 50)
    print("  关闭 BOSS 专用 CDP Chrome")
    print("=" * 50)
    print()

    # 只定位 scraper 专用 profile 目录，不复制、不重置
    profile = prepare_cdp_profile(copy_login_state=False, reset=False)
    cdp_data_dir = profile["path"]

    stopped = stop_cdp_chrome(cdp_data_dir)
    if stopped:
        print(f"\n✅ 已关闭 {stopped} 个 BOSS 专用 Chrome 进程 (profile: {cdp_data_dir})")
    else:
        print(f"\nℹ️  没有找到运行中的 BOSS 专用 Chrome 进程 (profile: {cdp_data_dir})")
    print()
    print("提示：仅关闭 scraper 隔离 profile 的 Chrome，不影响你的主 Chrome。")
    print()
    return 0


# ============================================================
# main
# ============================================================
def main():
    # Windows 控制台默认 GBK 无法编码输出中的 emoji/部分中文，会直接 UnicodeEncodeError
    # （实测 73 个单测中 8 个因此失败）。统一重配为 UTF-8，输出用 errors=replace 兜底。
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(
        description=f"BOSS直聘抓取 + 分析 (CDP Raw) v{__version__}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
筛选参数示例:
  --scale 305          公司规模 (301=0-20人 302=20-99 303=100-499 304=500-999 305=1000-9999 306=10000+)
  --stage 807          融资阶段 (801=未融资 ... 807=已上市 808=不需要融资)
  --salary 406         薪资范围 (402=3K以下 403=3-5K 404=5-10K 405=10-20K 406=20-50K 407=50K+)
  --experience 105     经验要求 (108=在校生 102=应届生 101=经验不限 103=1年以内 104=1-3年 105=3-5年 106=5-10年 107=10年+)
  --degree 203         学历要求 (209=初中及以下 208=中专/中技 206=高中 202=大专 203=本科 204=硕士 205=博士)
  --industry 1001      行业 (1001=互联网 1002=电商 1003=金融 ...)

城市支持中文: --city 上海  或代码: --city 101020100

示例:
  # 基础搜索
  %(prog)s --keyword "Java 风控" --city 上海 --pages 5

  # 筛选大公司 + 高薪
  %(prog)s --keyword "Java 风控" --scale 305 --salary 406

  # 抓列表 + 详情 + 分析报告
  %(prog)s --keyword "Java 风控" --pages 3 --detail --analysis

  # 只分析已有数据
  %(prog)s --input ~/.boss-zhipin-scraper/job-result/boss_jobs_20260609_1200.json --analysis --no-detail

  # 导出 CSV
  %(prog)s --keyword "Java 风控" --pages 3 --format csv

  # 合并旧数据
  %(prog)s --keyword "Java 风控" --pages 3 --merge old_data.json

  # 环境检查
  %(prog)s --check

  # 浏览器/API smoke test
  %(prog)s --smoke-test

  # 启动 Chrome CDP
  %(prog)s --setup-chrome
        """)
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--keyword", default="AI Agent", help="搜索关键词")
    p.add_argument("--city", default=DEFAULT_CITY_INPUT, help=f"城市 (中文名或代码，默认 {DEFAULT_CITY_INPUT})")
    p.add_argument("--pages", type=int, default=3, help=f"抓取页数 (最大 {MAX_PAGES})")
    p.add_argument("--max-jobs", type=int, default=None,
                   help="列表条数上限，抓够即停（BOSS 每页 30 条，实际条数可能略超；不设则按 --pages 抓满）")
    p.add_argument("--max-concurrent", type=int, default=1,
                   help="并发抓取任务数上限（默认 1=单任务互斥，规格 §3.6 硬防线；"
                        "指令显式指定（如 2-3）才放开；任一任务遇风控立即全停）")
    p.add_argument("--output", default=None, help="列表数据输出路径")
    p.add_argument("--detail-output", default=None, help="详情数据输出路径")
    p.add_argument("--cdp-port", type=int, default=DEFAULT_CDP_PORT,
                   help=f"CDP 调试端口 (默认 {DEFAULT_CDP_PORT})")
    p.add_argument("--format", default="json", choices=["json", "csv"],
                   help="输出格式 (默认 json)")
    p.add_argument("--merge", default=None,
                   help="合并已有 JSON 文件 (按 job_id 去重)")

    # 筛选参数
    p.add_argument("--scale", default=None, help="公司规模代码")
    p.add_argument("--stage", default=None, help="融资阶段代码")
    p.add_argument("--salary", default=None, help="薪资范围代码")
    p.add_argument("--experience", default=None, help="经验要求代码")
    p.add_argument("--degree", default=None, help="学历要求代码")
    p.add_argument("--industry", default=None, help="行业代码")

    # 功能开关
    p.add_argument("--detail", action="store_true", default=True, help="抓取详情页 JD（默认开启）")
    p.add_argument("--no-detail", dest="detail", action="store_false", help="不抓取详情页")
    p.add_argument("--max-details", type=int, default=None, help="最多抓几个详情")
    p.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                   help=f"详情抓取并发度（默认 {DEFAULT_CONCURRENCY}=串行；2-3 推荐，"
                        "并发越高成功率越低，含全局限速与错误率自适应降速）")
    p.add_argument("--analysis", action="store_true", help="输出分析报告")
    p.add_argument("--input", default=None, help="从已有 JSON 文件读取（跳过抓取）")
    p.add_argument("--allow-dom-fallback", action="store_true",
                   help="API 无数据时允许降级 DOM 提取（薪资可能受字体反爬影响，默认关闭）")

    # 工具命令
    p.add_argument("--check", action="store_true", help="运行环境诊断检查")
    p.add_argument("--verify", action="store_true",
                   help="校验已抓取结果文件完整性（--input 指定列表或自动取最新；详情自动匹配；只校验不抓取）")
    p.add_argument("--list-results", action="store_true",
                   help="列出结果目录中的历史抓取结果文件")
    p.add_argument("--batch", default=None, metavar="CONFIG.json",
                   help="批量列表抓取：从配置文件（JSON 数组，每个元素一个任务："
                        "keyword/city/pages/sleep/筛选字段）逐任务执行，任务间自动等待防风控")
    p.add_argument("--archive", nargs="?", const="1", default=None,
                   metavar="KEEP",
                   help="归档历史结果文件：每个类型保留最新 KEEP 个（默认 1），其余移入 archive/ 子目录；pending 活动文件不归档")
    p.add_argument("--retry-job", action="append", default=[],
                   metavar="JOB_ID",
                   help="强制重试指定 job_id（可重复指定；无视 pending 重试上限，未记录的也会重抓）")
    p.add_argument("--reset-lock", action="store_true",
                   help="人工确认后清除并发锁的熔断状态（--max-concurrent 任务遇风控全停后重开前使用）")
    p.add_argument("--smoke-test", action="store_true",
                   help="用真实 Chrome/CDP 跑一次 BOSS 搜索 API smoke test（不写结果文件）")
    p.add_argument("--list-cities", nargs="?", const="", default=None,
                   metavar="关键词",
                   help="打印支持的城市列表（可选关键词过滤，如 --list-cities 江）；"
                        "支持全国城市，码表见 data/city_codes.json，运行时自动从 BOSS 同步")
    p.add_argument("--setup-chrome", action="store_true",
                   help="自动启动 Chrome CDP 调试模式")
    p.add_argument("--copy-login-state", action="store_true",
                   help="手动从主 Chrome 导入 Local State + Cookie 相关文件到独立 profile（默认、首次启动、重复启动都不复制）")
    p.add_argument("--reset-chrome-profile", action="store_true",
                   help="重建 BOSS 专用 Chrome profile，会清除此专用浏览器内的登录态")
    p.add_argument("--no-wait-login", action="store_true",
                   help="--setup-chrome 启动后不等待 BOSS 登录完成")
    p.add_argument("--login-timeout", type=int, default=DEFAULT_LOGIN_TIMEOUT,
                   help=f"--setup-chrome 等待登录完成的秒数 (默认 {DEFAULT_LOGIN_TIMEOUT})")
    p.add_argument("--stop-chrome", action="store_true",
                   help="关闭 BOSS 专用 CDP Chrome（按隔离 profile 精准匹配，不影响主 Chrome）")
    p.add_argument("--close-chrome", action="store_true",
                   help="抓取正常结束后自动关闭专用 Chrome（默认不关；异常退出不触发，保留登录态）")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="输出 DEBUG 级别日志（调试 CDP 消息、探测详情等）")

    args = p.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
        log.debug("已开启 DEBUG 日志")

    # --check 模式
    if args.check:
        sys.exit(run_check(args.cdp_port))

    # --verify 模式（只校验结果文件，不抓取、不依赖 Chrome）
    if args.verify:
        list_path = args.input
        if not list_path:
            list_path = latest_results_file("jobs")
            if list_path:
                print(f"未指定 --input，自动使用最新列表文件: {list_path}")
            else:
                print(f"未找到列表文件（--input 或 {DEFAULT_RESULT_DIR} 下的 boss_jobs_*.json）")
                sys.exit(1)
        sys.exit(run_verify(list_path, args.detail_output))

    # --list-results 模式（列出历史结果文件）
    if args.list_results:
        sys.exit(run_list_results())

    # --archive 模式（归档历史结果文件）
    if args.archive is not None:
        try:
            keep = int(args.archive)
        except ValueError:
            print(f"❌ --archive 参数必须是正整数: {args.archive}")
            sys.exit(1)
        sys.exit(run_archive(keep_latest=keep))

    # --batch 模式（批量列表抓取）
    if args.batch:
        if not require_runtime_dependencies("requests", "websocket"):
            sys.exit(1)
        sys.exit(run_batch(args.batch, cdp_port=args.cdp_port,
                           max_concurrent=args.max_concurrent))

    if args.smoke_test:
        sys.exit(run_smoke_test(args.cdp_port))

    # --list-cities 模式（无需 Chrome/CDP，仅需 requests 拉取在线码表；拉取失败回退本地静态码表）
    if args.list_cities is not None:
        if not require_runtime_dependencies("requests"):
            sys.exit(1)
        list_cities(keyword=args.list_cities or None)
        sys.exit(0)

    # --setup-chrome 模式
    if args.setup_chrome:
        sys.exit(run_setup_chrome(
            args.cdp_port,
            copy_login_state=args.copy_login_state,
            reset_profile=args.reset_chrome_profile,
            wait_login=not args.no_wait_login,
            login_timeout=args.login_timeout,
        ))

    # --stop-chrome 模式（关闭 BOSS 专用 CDP Chrome，独立命令）
    if args.stop_chrome:
        sys.exit(run_stop_chrome())

    # --reset-lock 模式（人工确认后清除并发锁熔断状态）
    if args.reset_lock:
        if clear_scrape_lock_risk():
            print("✅ 并发锁熔断状态已清除，可重新开始抓取。")
        else:
            print("❌ 清除失败（锁文件异常）。")
        sys.exit(0)

    if not require_runtime_dependencies("requests", "websocket"):
        sys.exit(1)

    # 抓取前校验城市，避免无效中文名被原样作为 city 参数继续请求。
    if not args.input:
        try:
            resolve_city(args.city)
        except CityResolutionError as e:
            print(f"❌ {e}")
            sys.exit(1)

    # 页数限制
    if args.pages > MAX_PAGES:
        print(f"⚠️ 页数 {args.pages} 超过上限 {MAX_PAGES}，已自动调整为 {MAX_PAGES}")
        args.pages = MAX_PAGES

    # 收集筛选条件
    filters = {}
    for key in ["scale", "stage", "salary", "experience", "degree", "industry"]:
        val = getattr(args, key)
        if val:
            filters[key] = val

    # 加载或抓取列表
    if args.input:
        with open(args.input, encoding="utf-8") as f:
            list_data = json.load(f)
        print(f"从文件加载 {len(list_data.get('jobs',[]))} 条: {args.input}")
    else:
        # 登录状态检测
        print("检测登录状态...")
        login_result = check_login_state(args.cdp_port)
        if login_result.status is LoginProbeStatus.UNAUTHENTICATED:
            print("❌ 未检测到 BOSS直聘登录状态。请先在 Chrome 中登录 zhipin.com。")
            print("   可运行 --check 检查环境，或 --setup-chrome 启动 Chrome。")
            sys.exit(1)
        if login_result.status is LoginProbeStatus.RESTRICTED:
            print(f"❌ {describe_login_probe_result(login_result)}，已停止抓取。")
            print("   请先在浏览器中完成验证或稍后再试，不要重复运行登录探测。")
            sys.exit(1)
        if login_result.status is LoginProbeStatus.RESPONSE_ERROR:
            print(f"❌ {describe_login_probe_result(login_result)}，已停止抓取。")
            sys.exit(1)
        if login_result.status is LoginProbeStatus.EMPTY:
            print(f"⚠️  {describe_login_probe_result(login_result)}；继续执行实际职位搜索。\n")
        else:
            print("✅ 已登录\n")

        list_data = scrape_list(
            args.keyword, args.city, args.pages, filters, args.output,
            cdp_port=args.cdp_port, fmt=args.format,
            allow_dom_fallback=args.allow_dom_fallback,
            max_jobs=args.max_jobs,
            max_concurrent=args.max_concurrent,
        )

    # 合并外部文件
    merged_details = None
    if args.merge:
        merged_jobs = merge_jobs(args.merge, list_data.get("jobs", []))
        list_data["jobs"] = merged_jobs
        list_data["total"] = len(merged_jobs)
        # 重新保存合并结果
        if args.output:
            flush_jobs(args.output, {
                "keyword": list_data.get("keyword", ""),
                "city": list_data.get("city", ""),
                "filters": list_data.get("filters", {}),
                "filter_desc": list_data.get("filter_desc", []),
                "scraped_at": datetime.now().isoformat(),
                "merged_from": args.merge,
            }, merged_jobs)
            print(f"合并结果已保存: {args.output}")
            if args.format == "csv":
                csv_path = args.output.rsplit(".", 1)[0] + ".csv"
                write_csv(csv_path, merged_jobs)
        # 同时加载旧详情，供后续详情抓取/分析合并（按 job_id 去重）
        merged_details = merge_details(args.merge, [])

    # 抓详情
    details = None
    if args.detail and list_data.get("jobs"):
        pending_ids = None
        if args.retry_job:
            # 用户强制重试白名单：无视 pending 重试上限，未记录的先加入重试
            pending_ids = load_pending_ids(
                args.detail_output or default_output_path("details"),
                force_ids=args.retry_job)
        details = scrape_details(
            list_data, args.max_details, args.detail_output,
            cdp_port=args.cdp_port, fmt=args.format,
            concurrency=args.concurrency,
            pending_ids=pending_ids,
        )
        # 若处于合并流程，把旧详情并入本次抓取结果并重新落盘，保证 --merge 后详情不丢失
        if merged_details and args.detail_output:
            details = merge_details_from_lists(merged_details, details)
            _atomic_write_json(args.detail_output, details)
            print(f"合并详情已保存: {args.detail_output}")
            if args.format == "csv":
                detail_csv = args.detail_output.rsplit(".", 1)[0] + ".csv"
                write_detail_csv(detail_csv, details)

    # 分析
    if args.analysis:
        # 如果有详情文件也加载
        if not details:
            details = load_existing_details(args.input, args.detail_output)
        analyze(list_data, details, search_keyword=args.keyword)

    # 抓取正常结束后按需收尾（仅成功路径；异常/登录失败走 sys.exit，不会触发，保留登录态）
    if args.close_chrome:
        profile = prepare_cdp_profile(copy_login_state=False, reset=False)
        stopped = stop_cdp_chrome(profile["path"])
        if stopped:
            print(f"\n🧹 已按 --close-chrome 关闭 BOSS 专用 Chrome 进程：{stopped} 个")
        else:
            print("\nℹ️  --close-chrome 未发现运行中的 BOSS 专用 Chrome 进程")


if __name__ == "__main__":
    main()
