"""岗位数据模型 —— 采集层与消费层之间**唯一的真相来源**。

为什么要有这个模块（2026-10-22 的根因定位）
------------------------------------------------
平台列表 API 给我们的 29 个字段里，**10 个（34%）被采集却零下游消费**：
`proxyJob`/`proxyType`（猎头代发标注）、`jobType`（实习/校招标注）、`activeTimeDesc`
（HR 活跃度）、`jobValidStatus`、`iconFlagList`/`iconWord`、`brandIndustry`、
`welfare`、`bossTitle`、`jobDegree`。

这不是"漏了几个字段"，是**缺一个数据模型**：

- 采集器把平台的原始结构拍平成一堆 key-value，**字段的含义只存在于字段名里**；
- 谁用、怎么用、能不能信、口径是什么，全靠"下一个读代码的人记得"；
- 于是"抄了不用"在工程上是**合法的**——代码不报错、测试不红、CI 全绿，
  没人能说它错；缺陷一直堆到"人肉读数据"那一层才暴露。

而这些缺陷的后果是实打实的：25 条猎头代发岗（薪资面议、虚高）混进结果、35 条实习/校招岗
（日薪口径）混进社招、岗位更新时间与技能标签丢失、"平台没给活跃信息"被当成"不活跃"。

现在：**平台原始字段 → JobRecord（唯一模型）→ 导出 / CSV / 分析 / 自检 全部只认模型。**

模型的硬规则
------------
1. **字段进模型 = 必须声明四件事**：来源（平台哪个字段 / 我们推导）、类型、语义、
   以及**它被谁消费**（``used_by``）。若确实无需消费，必须写明 ``exempt_reason``
   （例如平台内部 id）——不能沉默地抄进来。
2. **记录级语义只在模型里定义一次**：发布主体类型、招聘类型、活跃度三态、标题归一化、
   薪资解析。其它模块一律调用模型，不再各自实现一遍。
3. **``tests/test_job_model.py`` 会验证上面的规则**：新增字段若既无消费方也没写豁免理由，
   测试直接失败——**"抄了不用"从此不是合法的**。

集合级语义（重复分组、供给编排、导出不变量）留在 ``semantics.py``：那类逻辑天然作用于
一组记录，不属于单条记录。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from dataclasses import field as _dc_field

# ---------------------------------------------------------------------------
# 1) 字段契约
# ---------------------------------------------------------------------------

SOURCE_LIST = "list_api"          # 平台列表 API 字段
SOURCE_DETAIL = "detail_page"     # 平台详情页字段
SOURCE_DERIVED = "derived"        # 我们推导（模型语义层）


@dataclass(frozen=True)
class FieldSpec:
    """一条岗位字段的契约。"""

    name: str
    source: str
    type: str
    semantic: str
    used_by: tuple = ()            # 消费方模块名（下游真的引用它的模块）
    required: bool = False
    exempt_reason: str = ""        # 允许"无消费方"时必须写明理由
    delivery_only: bool = False    # 仅交付给用户（导出里可见），不参与任何判断
    notes: str = ""

    @property
    def is_platform_field(self) -> bool:
        return self.source in (SOURCE_LIST, SOURCE_DETAIL)


# 消费方模块名（与仓库里的模块一一对应，CI 会核对）
_M_MODEL = "job_model"
_M_EXPORT = "export_contract"
_M_SUMMARY = "job_summary"
_M_SEMANTICS = "semantics"
_M_MAIN = "boss_cdp_raw"

FIELD_SPECS: tuple = (
    # ── 标识 ──────────────────────────────────────────────────────────
    FieldSpec("job_id", SOURCE_LIST, "str", "岗位唯一标识（耐久主键）",
              (_M_EXPORT, _M_SUMMARY, _M_SEMANTICS, _M_MAIN), required=True,
              notes="详情链接不能由它重建（URL 含 securityId 段），只能用它做主键"),
    FieldSpec("encrypt_job_id", SOURCE_LIST, "str", "URL 里的岗位 token 段",
              (_M_MAIN,), notes="拼 job_detail URL 用；不落导出（internal key）"),
    FieldSpec("security_id", SOURCE_LIST, "str", "平台内部凭据（securityId）",
              (), exempt_reason="平台凭据，红线要求绝不进导出/日志"),
    FieldSpec("lid", SOURCE_LIST, "str", "平台内部列表 id", (),
              exempt_reason="平台内部 id，无用户价值"),
    FieldSpec("encrypt_boss_id", SOURCE_LIST, "str", "平台内部 HR id", (),
              exempt_reason="平台内部 id，无用户价值"),
    FieldSpec("encrypt_brand_id", SOURCE_LIST, "str", "平台内部公司 id", (),
              exempt_reason="平台内部 id，无用户价值"),
    FieldSpec("job_link", SOURCE_LIST, "str", "岗位详情页 URL（抓取时刻有效）",
              (_M_EXPORT, _M_SUMMARY, _M_SEMANTICS, _M_MAIN), required=True,
              notes="含 securityId 段，不能由 job_id 重建；实测跨天稳定"),
    FieldSpec("company_link", SOURCE_LIST, "str", "公司主页 URL",
              (_M_EXPORT, _M_SUMMARY, _M_SEMANTICS)),
    FieldSpec("link_verified_at", SOURCE_DERIVED, "str",
              "该链接在抓取时刻被验证指向这个岗位",
              (_M_EXPORT, _M_SEMANTICS)),
    # ── 岗位本体 ─────────────────────────────────────────────────────
    FieldSpec("title", SOURCE_LIST, "str", "岗位名（平台原样回传）",
              (_M_EXPORT, _M_SUMMARY, _M_SEMANTICS, _M_MAIN), required=True),
    FieldSpec("title_key", SOURCE_DERIVED, "str", "标题归一化键（分组/匹配用）",
              (_M_EXPORT, _M_SEMANTICS, _M_SUMMARY)),
    FieldSpec("salary", SOURCE_LIST, "str", "薪资描述原文",
              (_M_EXPORT, _M_SUMMARY, _M_SEMANTICS, _M_MAIN), required=True),
    FieldSpec("salary_source", SOURCE_LIST, "str", "薪资来源（api=列表 API）",
              (), delivery_only=True,
              notes="报表用：说明薪资来自列表 API（而非页面）"),
    FieldSpec("location", SOURCE_LIST, "str", "城市·区域·商圈",
              (_M_EXPORT, _M_SUMMARY, _M_SEMANTICS, _M_MAIN), required=True),
    FieldSpec("tags", SOURCE_LIST, "str", "经验|学历（列表侧拼接）",
              (_M_EXPORT, _M_SUMMARY, _M_SEMANTICS)),
    FieldSpec("experience", SOURCE_LIST, "str", "经验要求",
              (), delivery_only=True,
              notes="报表用：读专用字段，不再从 tags 串里解析（消除重复来源）"),
    FieldSpec("education", SOURCE_LIST, "str", "学历要求",
              (), delivery_only=True,
              notes="报表用：读专用字段，不再从 tags 串里解析（消除重复来源）"),
    FieldSpec("skills", SOURCE_LIST, "list", "列表侧技能标签",
              (_M_EXPORT, _M_SUMMARY, _M_SEMANTICS)),
    FieldSpec("skill_tags", SOURCE_DETAIL, "list", "详情页技能标签（比列表侧准）",
              (_M_EXPORT, _M_SUMMARY, _M_SEMANTICS)),
    FieldSpec("job_labels", SOURCE_LIST, "str", "平台岗位标签",
              (_M_EXPORT, _M_SUMMARY, _M_SEMANTICS)),
    FieldSpec("welfare", SOURCE_LIST, "str", "福利待遇",
              (_M_EXPORT, _M_SUMMARY, _M_SEMANTICS)),
    FieldSpec("icon_flags", SOURCE_LIST, "str", "平台角标（急/新等）",
              (), delivery_only=True,
              notes="报表用：平台角标统计"),
    FieldSpec("icon_word", SOURCE_LIST, "str", "平台角标文字",
              (), delivery_only=True,
              notes="报表用：平台角标统计"),
    FieldSpec("job_valid_status", SOURCE_LIST, "str", "岗位有效性（1=有效）",
              (), delivery_only=True,
              notes="报表用：已下架/无效岗计数"),
    # ── 公司与招聘方 ─────────────────────────────────────────────────
    FieldSpec("company_name", SOURCE_LIST, "str", "公司名（列表侧）",
              (_M_EXPORT, _M_SUMMARY, _M_SEMANTICS, _M_MAIN), required=True),
    FieldSpec("boss_name", SOURCE_LIST, "str", "招聘方名称",
              (), delivery_only=True,
              notes="交付给用户：招聘方名称（平台只给品牌名，无个人身份信息）"),
    FieldSpec("boss_title", SOURCE_LIST, "str", "招聘方职位（猎头顾问=代发佐证）",
              (_M_MODEL, _M_SEMANTICS, _M_SUMMARY),
              notes="平台自证的第三个信号：22 条『猎头顾问』⊂ 25 条代发岗"),
    FieldSpec("company_scale", SOURCE_LIST, "str", "公司规模",
              (_M_EXPORT, _M_SUMMARY, _M_SEMANTICS)),
    FieldSpec("company_stage", SOURCE_LIST, "str", "融资阶段",
              (_M_EXPORT, _M_SUMMARY, _M_SEMANTICS)),
    FieldSpec("company_industry", SOURCE_LIST, "str", "公司行业",
              (_M_EXPORT, _M_SUMMARY, _M_SEMANTICS)),
    FieldSpec("anonymous", SOURCE_LIST, "str", "平台匿名标记（位掩码）",
              (), delivery_only=True,
              notes="平台位掩码（实测 0/1/12/20/101），含义未验证 → 暂不参与判断"),
    # ── 平台标注（语义化的关键输入）──────────────────────────────────
    FieldSpec("proxy_job", SOURCE_LIST, "str", "平台标注：是否猎头代发",
              (_M_SEMANTICS, _M_SUMMARY, _M_MAIN)),
    FieldSpec("proxy_type", SOURCE_LIST, "str", "平台标注：代发类型（1/4）",
              (_M_SEMANTICS, _M_SUMMARY, _M_MAIN)),
    FieldSpec("job_type", SOURCE_LIST, "str", "平台标注：招聘类型（4=实习 5=校招）",
              (_M_SEMANTICS, _M_SUMMARY, _M_MAIN)),
    FieldSpec("boss_active_status", SOURCE_LIST, "str", "HR 活跃度文案",
              (_M_EXPORT, _M_SUMMARY, _M_SEMANTICS, _M_MAIN)),
    # ── 详情页 ───────────────────────────────────────────────────────
    FieldSpec("jd", SOURCE_DETAIL, "str", "职位描述正文",
              (_M_EXPORT, _M_SUMMARY, _M_SEMANTICS, _M_MAIN), required=True),
    FieldSpec("page_update_date", SOURCE_DETAIL, "str", "岗位侧最后编辑日期",
              (), delivery_only=True,
              notes="交付给用户：岗位侧最后编辑日期（判断岗位新鲜度）"),
    FieldSpec("tags_list", SOURCE_DETAIL, "str", "详情页经验|学历",
              (), delivery_only=True,
              notes="交付给用户：详情页经验|学历"),
    FieldSpec("job_status_desc", SOURCE_DETAIL, "str", "岗位状态描述",
              (), delivery_only=True,
              notes="交付给用户：岗位状态描述"),
    FieldSpec("brand_introduce", SOURCE_DETAIL, "str", "公司介绍",
              (), delivery_only=True,
              notes="交付给用户：公司介绍"),
    FieldSpec("brand_scale_name", SOURCE_DETAIL, "str", "详情页公司规模",
              (), delivery_only=True,
              notes="交付给用户：详情页公司规模"),
    FieldSpec("brand_stage_name", SOURCE_DETAIL, "str", "详情页融资阶段",
              (), delivery_only=True,
              notes="交付给用户：详情页融资阶段"),
    FieldSpec("brand_industry_name", SOURCE_DETAIL, "str", "详情页行业",
              (), delivery_only=True,
              notes="交付给用户：详情页行业"),
    FieldSpec("brand_active_time", SOURCE_DETAIL, "str", "详情页公司活跃时间",
              (), delivery_only=True,
              notes="交付给用户：详情页公司活跃时间"),
    # ── 派生语义 ─────────────────────────────────────────────────────
    FieldSpec("post_type", SOURCE_DERIVED, "str",
              "发布主体类型（direct 企业直招 / agency 猎头代发 / anonymous 匿名）",
              (_M_EXPORT, _M_SEMANTICS, _M_SUMMARY, _M_MAIN), required=True),
    FieldSpec("recruit_type", SOURCE_DERIVED, "str",
              "招聘类型（social 社招 / intern 实习 / campus 校招）",
              (_M_EXPORT, _M_SEMANTICS, _M_SUMMARY, _M_MAIN), required=True),
    FieldSpec("active_state", SOURCE_DERIVED, "str",
              "活跃度三态（active / inactive / unknown）",
              (_M_EXPORT, _M_SEMANTICS, _M_SUMMARY)),
    FieldSpec("dup_group", SOURCE_DERIVED, "str", "同源重复组 id（空=不重复）",
              (_M_EXPORT, _M_SEMANTICS)),
    FieldSpec("dup_size", SOURCE_DERIVED, "int", "同源重复组内条数",
              (_M_EXPORT, _M_SEMANTICS)),
    FieldSpec("is_primary", SOURCE_DERIVED, "bool", "是否重复组内首条",
              (_M_EXPORT, _M_SEMANTICS)),
)

FIELD_INDEX = {spec.name: spec for spec in FIELD_SPECS}

# 导出里允许出现的字段（模型字段 + 平台原始字段的子集）
EXPORT_FIELDS = tuple(spec.name for spec in FIELD_SPECS)

# **绝不进导出/CSV 的内部字段**（红线：securityId 等平台凭据不落盘）
EXPORT_HIDDEN_FIELDS = (
    "security_id", "lid",
    "encrypt_job_id", "encrypt_boss_id", "encrypt_brand_id",
)

# 对外可见的字段（导出行与 CSV 列的来源）
EXPORT_PUBLIC_FIELDS = tuple(
    name for name in EXPORT_FIELDS if name not in EXPORT_HIDDEN_FIELDS)


# ---------------------------------------------------------------------------
# 2) 记录级语义（唯一判定点）
# ---------------------------------------------------------------------------

# 平台用 proxyJob/proxyType 标注"这条是代发"。实测（294 条）：
#   proxyJob=1 → 25 条；其中 proxyType=1（22 条，HR 职位=猎头顾问）、
#   proxyType=4（3 条，HR 职位=招聘者/招聘主管，第三方人力代发）
#   proxyJob 空 → 269 条全是企业直发
# 猎头/代发岗常见的匿名公司名写法（"某"字头、模糊描述）
_ANON_PATTERNS = (
    re.compile(r"^某"),
    re.compile(r"^(知名|大型|头部|顶级|互联网大厂|上市|一线)"),
    re.compile(r"(公司|集团|科技|网络|信息|技术)?$"),
)
# 明显是"个人/工作室"形态的发布主体（不一定是代发，但同样不可信）
_FRAGILE_TYPES = ("工作室", "个体", "自由职业", "兼职")
# 招聘类型（jobType）：4=实习、5=校招（实证见 FIELD_SPECS 说明）
RECRUIT_TYPE_BY_JOB_TYPE = {"4": "intern", "5": "campus"}
# 猎头顾问（HR 职位）——平台自证的代发信号，用于兜底
_AGENCY_BOSS_TITLES = {"猎头顾问"}

_ZOMBIE_ACTIVITY_RE = re.compile(r"(?:\d+\s*)?(?:个?月|周|年)\s*前活跃")
_TITLE_PUNCT_RE = re.compile(r"[·・\-—_、,，。;；:：!！?？()（）\[\]【】\"'`]+")

_SALARY_K_RE = re.compile(r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*K", re.IGNORECASE)
_SALARY_DAY_RE = re.compile(r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*元/天")
_SALARY_MONTHS_RE = re.compile(r"(\d+)\s*薪")


def compute_job_id(key) -> str:
    """岗位 id = md5(job_link 或 title)[:16]（**唯一生成点**）。

    列表侧与详情侧靠它对齐；改这里会同时改掉两侧的匹配键。
    """
    import hashlib
    return hashlib.md5(str(key or "").encode()).hexdigest()[:16]


def normalize_title(title) -> str:
    """标题归一化键：全角转半角、去空白、去标点、小写。

    用于**分组/匹配**，不用于展示（展示保留平台原值）。平台原样回传 →
    `AI产品经理` 67 / `ai产品经理` 55 / `AI 产品经理` 6，按字符串分组会漏。
    """
    text = unicodedata.normalize("NFKC", str(title or "")).strip().lower()
    text = re.sub(r"\s+", "", text)
    return _TITLE_PUNCT_RE.sub("", text)


def parse_salary_monthly(salary):
    """薪资原文 → 月薪范围（千元）。支持 "20-40K" / "20-40K·15薪" / "300-500元/天"。"""
    text = str(salary or "").strip()
    if not text:
        return None
    match = _SALARY_K_RE.search(text)
    if match:
        return float(match.group(1)), float(match.group(2))
    match = _SALARY_DAY_RE.search(text)
    if match:
        return (round(float(match.group(1)) * 22 / 1000, 1),
                round(float(match.group(2)) * 22 / 1000, 1))
    return None


def parse_salary_annual(salary):
    """薪资原文 → 年薪范围（千元）："20-40K·15薪" → (300, 600)。"""
    monthly = parse_salary_monthly(salary)
    if monthly is None:
        return None
    low, high = monthly
    match = _SALARY_MONTHS_RE.search(str(salary or ""))
    months = int(match.group(1)) if match else 12
    return round(low * months, 1), round(high * months, 1)


def classify_post_type(job) -> str:
    """发布主体类型：``direct`` / ``agency`` / ``anonymous``。

    判定优先级（依据都是**平台自己的标注**，不靠猜）：
    1. 平台标的 ``proxy_job`` / ``proxy_type`` 非空 → ``agency``
    2. HR 职位 = 「猎头顾问」→ ``agency``（兜底信号：实测 22 条全部命中代发、零漏判）
    3. 公司名匿名（"某…"/"知名…"/工作室…）→ ``anonymous``
    4. 其余 → ``direct``
    """
    if not isinstance(job, dict):
        return "direct"
    proxy = str(job.get("proxy_job") or "").strip()
    proxy_type = str(job.get("proxy_type") or "").strip()
    if proxy or proxy_type:
        return "agency"
    if str(job.get("boss_title") or "").strip() in _AGENCY_BOSS_TITLES:
        return "agency"
    company = str(job.get("company_name") or job.get("company") or "").strip()
    if company:
        for pattern in _ANON_PATTERNS[:2]:
            if pattern.search(company):
                return "anonymous"
        if any(key in company for key in _FRAGILE_TYPES):
            return "anonymous"
    return "direct"


def is_agency_like(job) -> bool:
    """猎头代发或匿名发布（薪资带虚高、公司不可核，求职端需区别对待）。"""
    return classify_post_type(job) in ("agency", "anonymous")


def classify_recruit_type(job) -> str:
    """招聘类型：``social`` / ``intern`` / ``campus``（平台 jobType：4=实习、5=校招）。"""
    if not isinstance(job, dict):
        return "social"
    raw = str(job.get("job_type") or "").strip()
    return RECRUIT_TYPE_BY_JOB_TYPE.get(raw, "social")


def active_state(status) -> str:
    """HR 活跃度三态：``active`` / ``inactive`` / ``unknown``。

    **"平台没给"不等于"不活跃"**（实测 17 条两边都没有活跃标签）——当成不活跃会误杀，
    当成活跃会误导。
    """
    text = str(status or "").strip()
    if not text:
        return "unknown"
    if _ZOMBIE_ACTIVITY_RE.search(text):
        return "inactive"
    if text == "在线" or text.endswith("活跃"):
        return "active"
    return "unknown"


# ---------------------------------------------------------------------------
# 3) 记录对象
# ---------------------------------------------------------------------------

@dataclass
class JobRecord:
    """一条岗位记录（列表侧与详情侧合并后的规范形态）。"""

    data: dict = _dc_field(default_factory=dict)

    # -- 构造 ----------------------------------------------------------
    @classmethod
    def from_list_payload(cls, raw) -> JobRecord:
        """平台 joblist.json 原始条目 → JobRecord。"""
        if not isinstance(raw, dict):
            return cls({})
        encrypt_job_id = str(raw.get("encryptJobId") or "")
        encrypt_brand_id = str(raw.get("encryptBrandId") or "")
        salary = raw.get("salaryDesc") or ""
        location = "·".join([
            raw.get("cityName") or "",
            raw.get("areaDistrict") or "",
            raw.get("businessDistrict") or "",
        ])
        tags = " | ".join(
            t for t in (raw.get("jobExperience") or "", raw.get("jobDegree") or "")
            if t and t != "不限"
        )

        def optional(key):
            value = raw.get(key)
            return "" if value is None else value

        data = {
            "title": raw.get("jobName") or "",
            "salary": salary,
            "salary_source": "api" if salary else "api_empty",
            "location": location,
            "tags": tags,
            "boss_name": raw.get("brandName") or "",
            "company_name": raw.get("brandName") or "",
            "boss_title": raw.get("bossTitle") or "",
            "boss_active_status": _list_active_status(raw),
            "anonymous": optional("anonymous"),
            "job_valid_status": optional("jobValidStatus"),
            "icon_flags": "|".join(raw.get("iconFlagList") or []),
            "icon_word": raw.get("iconWord") or "",
            "proxy_job": raw.get("proxyJob") or "",
            "proxy_type": raw.get("proxyType") or "",
            "job_type": raw.get("jobType") or "",
            "experience": raw.get("jobExperience") or "",
            "education": raw.get("jobDegree") or "",
            "company_scale": raw.get("brandScaleName") or "",
            "company_stage": raw.get("brandStageName") or "",
            "company_industry": raw.get("brandIndustry") or "",
            "job_labels": " | ".join(raw.get("jobLabels") or []),
            "skills": raw.get("skills") or [],
            "security_id": raw.get("securityId") or "",
            "lid": raw.get("lid") or "",
            "encrypt_job_id": encrypt_job_id,
            "encrypt_boss_id": str(raw.get("encryptBossId") or ""),
            "encrypt_brand_id": encrypt_brand_id,
            "job_link": (
                f"https://www.zhipin.com/job_detail/{encrypt_job_id}.html"
                if encrypt_job_id else ""
            ),
            "company_link": (
                f"https://www.zhipin.com/gongsi/{encrypt_brand_id}.html"
                if encrypt_brand_id else ""
            ),
            "welfare": " | ".join(raw.get("welfareList") or []),
        }
        record = cls(data)
        record.data["job_id"] = compute_job_id(
            record.data.get("job_link") or record.data.get("title") or "")
        return record

    @classmethod
    def from_detail_payload(cls, job, extracted, link_verified_at="") -> JobRecord:
        """列表侧 job + 详情抽取结果 → JobRecord（详情字段覆盖同名列表字段）。"""
        base = dict(job or {})
        base.update({
            "skill_tags": (extracted or {}).get("tags", []),
            "jd": (extracted or {}).get("jd", ""),
            "page_update_date": (extracted or {}).get("page_update_date", ""),
            "tags_list": (extracted or {}).get("tags_list", ""),
            "job_status_desc": (extracted or {}).get("job_status_desc", ""),
            "brand_introduce": (extracted or {}).get("brand_introduce", ""),
            "brand_scale_name": (extracted or {}).get("brand_scale_name", ""),
            "brand_stage_name": (extracted or {}).get("brand_stage_name", ""),
            "brand_industry_name": (extracted or {}).get("brand_industry_name", ""),
            "brand_active_time": (extracted or {}).get("brand_active_time", ""),
        })
        record = cls(base)
        record.apply_derived(link_verified_at=link_verified_at)
        return record

    # -- 派生语义（唯一判定点）------------------------------------------
    def apply_derived(self, link_verified_at="") -> JobRecord:
        """把派生字段（发布主体/招聘类型/活跃三态/标题键/链接时刻）写回记录。"""
        self.data["post_type"] = classify_post_type(self.data)
        self.data["recruit_type"] = classify_recruit_type(self.data)
        self.data["active_state"] = active_state(self.data.get("boss_active_status"))
        self.data["title_key"] = normalize_title(self.data.get("title"))
        if link_verified_at:
            self.data["link_verified_at"] = link_verified_at
        self.data.setdefault("dup_group", "")
        self.data.setdefault("dup_size", 1)
        self.data.setdefault("is_primary", True)
        return self

    @property
    def job_id(self) -> str:
        return str(self.data.get("job_id") or "")

    @property
    def title(self) -> str:
        return str(self.data.get("title") or "")

    @property
    def company_name(self) -> str:
        return str(self.data.get("company_name") or "")

    @property
    def salary(self) -> str:
        return str(self.data.get("salary") or "")

    @property
    def post_type(self) -> str:
        return str(self.data.get("post_type") or classify_post_type(self.data))

    @property
    def recruit_type(self) -> str:
        return str(self.data.get("recruit_type") or classify_recruit_type(self.data))

    @property
    def active_state(self) -> str:
        return str(self.data.get("active_state") or active_state(
            self.data.get("boss_active_status")))

    @property
    def title_key(self) -> str:
        return str(self.data.get("title_key") or normalize_title(self.data.get("title")))

    @property
    def salary_monthly(self):
        return parse_salary_monthly(self.data.get("salary"))

    @property
    def is_agency(self) -> bool:
        return self.post_type in ("agency", "anonymous")

    @property
    def is_social_hire(self) -> bool:
        return self.recruit_type == "social"

    @property
    def is_direct_social(self) -> bool:
        """社招 + 企业直发（求职该看的口径）。"""
        return self.is_social_hire and self.post_type == "direct"

    # -- 合并与输出 ----------------------------------------------------
    def merge_detail(self, detail) -> JobRecord:
        """并入详情侧字段（详情优先），再重算派生语义。"""
        detail = detail or {}
        merged = dict(self.data)
        for key, value in detail.items():
            if value in (None, "", [], {}):
                continue
            merged[key] = value
        record = JobRecord(merged)
        record.apply_derived(link_verified_at=self.data.get("link_verified_at", ""))
        return record

    def to_dict(self) -> dict:
        """导出用的行（扁平 dict）。"""
        return dict(self.data)

    def to_export_dict(self) -> dict:
        """导出用的行：模型字段齐全 + 平台原始字段保留。"""
        row = dict(self.data)
        for spec in FIELD_SPECS:
            row.setdefault(spec.name, "" if spec.type != "list" else [])
        return row


def _list_active_status(raw) -> str:
    """列表侧活跃度文案：优先 activeTimeDesc，否则 bossOnline → 「在线」。"""
    desc = str(raw.get("activeTimeDesc") or "").strip()
    if desc:
        return desc
    if raw.get("bossOnline"):
        return "在线"
    return ""


# ---------------------------------------------------------------------------
# 4) 契约自检（CI 用）
# ---------------------------------------------------------------------------

def spec_issues():
    """字段契约自身的问题（重复声明 / 缺语义 / 缺消费方且没写豁免理由）。"""
    issues = []
    seen = set()
    for spec in FIELD_SPECS:
        if spec.name in seen:
            issues.append(f"字段重复声明: {spec.name}")
        seen.add(spec.name)
        if not spec.semantic:
            issues.append(f"{spec.name}: 缺少语义说明")
        if (spec.is_platform_field and not spec.used_by
                and not spec.exempt_reason and not spec.delivery_only):
            issues.append(
                f"{spec.name}: 采集了却没有任何消费方，既没写豁免理由也没声明"
                "仅交付给用户（抄了不用 = 未声明的缺陷）")
    return issues


def audit_unused_fields(repo_root=None) -> list:
    """在真实源码里核对：声明了消费方的字段，**真的**被那些模块引用了吗？

    返回"声明了消费方但源码里查无引用"的字段名（说明声明与实现不一致）。
    """
    import os

    if repo_root is None:
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    modules = {
        _M_MODEL: os.path.join(repo_root, "scripts", "job_model.py"),
        _M_EXPORT: os.path.join(repo_root, "scripts", "export_contract.py"),
        _M_SUMMARY: os.path.join(repo_root, "scripts", "job_summary.py"),
        _M_SEMANTICS: os.path.join(repo_root, "scripts", "semantics.py"),
        _M_MAIN: os.path.join(repo_root, "scripts", "boss_cdp_raw.py"),
    }
    cache = {}
    for name, path in modules.items():
        try:
            with open(path, encoding="utf-8") as handle:
                cache[name] = handle.read()
        except OSError:
            cache[name] = ""
    missing = []
    for spec in FIELD_SPECS:
        if not spec.used_by or spec.delivery_only:
            continue
        referenced = [c for c in spec.used_by if spec.name in cache.get(c, "")]
        if not referenced:
            missing.append(
                f"{spec.name}（声明 {'/'.join(spec.used_by)} 消费，但这些模块的源码里"
                "都查无引用）")
    return missing
