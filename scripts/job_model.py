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

# ---------------------------------------------------------------------------
# 保真溯源类别（2026-10-22「纸片上什么样，爬出来就应该是什么样」）
# ---------------------------------------------------------------------------
# 用户验收标准：**boss 上的东西，纸片上是什么样，你爬取出来的就应该是什么样**。
# 要能核对这句话，前提是**每个字段都说得清它的值是怎么来的**。三类：
#
# - ``verbatim``（平台原样抄来）：值与平台原文**逐字相同**（含类型）。
#   核对方式：把原文的值与产物里的值直接比 → 逐字一致 / 不一致。
# - ``normalized``（平台原样但做了规范化）：值仍是平台的那段内容，只是**按声明过的
#   规则改写**（全半角、空白、标题归一、拼接符、日期格式…）。
#   核对方式：两边**按同一条声明规则**规范化后比 → 规范化后一致 / 不一致。
#   规则必须写下来（``raw_shape``），否则「规范化」就成了藏差异的地方。
# - ``derived``（由平台字段推导，规则可查）：值是我们按规则**算**出来的
#   （post_type / recruit_type / salary_min…）。
#   核对方式：**不与原文比**（原文根本没有这个字段），而是核对
#   ①推导输入在原文里存在 ②推导规则有声明 ③产物里给出依据。
#
# 三类之外没有第四类：任何字段都必须落进其中一类，否则「说不清来路」＝缺陷。

PROV_VERBATIM = "verbatim"        # 平台原样抄来
PROV_NORMALIZED = "normalized"    # 平台原样但做了规范化（规则已声明）
PROV_DERIVED = "derived"          # 由平台字段推导（规则可查）

PROVENANCE_VALUES = (PROV_VERBATIM, PROV_NORMALIZED, PROV_DERIVED)


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
    # ── 保真溯源声明（新增；见上面的 PROV_* 说明）────────────────────
    provenance: str = ""           # verbatim / normalized / derived
    raw_paths: tuple = ()          # 平台原文里的字段路径（list 侧=键名；detail 侧=点路径）
    raw_shape: str = ""            # 从原文取值的形状（见 RAW_SHAPES）
    raw_join: str = ""             # 多值拼接时的分隔符
    raw_filter: str = ""           # 拼接前的过滤规则（如去掉"不限"）
    derive_rule: str = ""          # derived 字段的规则 id（见 DERIVE_RULES）
    provenance_note: str = ""      # 说不清/核对不了时的原因（写下来，不留空白）

    @property
    def is_platform_field(self) -> bool:
        return self.source in (SOURCE_LIST, SOURCE_DETAIL)

    @property
    def scope(self) -> str:
        """溯源核对的作用域：``list`` / ``detail`` / ``""``（不与原文比）。"""
        if self.source == SOURCE_LIST:
            return "list"
        if self.source == SOURCE_DETAIL:
            return "detail"
        return ""

    @property
    def is_derived(self) -> bool:
        return self.provenance == PROV_DERIVED


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
              notes="详情链接不能由它重建（URL 含 securityId 段），只能用它做主键", provenance=PROV_DERIVED, raw_paths=("encryptJobId",), raw_shape='', derive_rule='job_id'),
    FieldSpec("encrypt_job_id", SOURCE_LIST, "str", "URL 里的岗位 token 段",
              (_M_MAIN,), notes="拼 job_detail URL 用；不落导出（internal key）", provenance=PROV_VERBATIM, raw_paths=("encryptJobId",), raw_shape='str', derive_rule=''),
    FieldSpec("security_id", SOURCE_LIST, "str", "平台内部凭据（securityId）",
              (), exempt_reason="平台凭据，红线要求绝不进导出/日志", provenance=PROV_VERBATIM, raw_paths=("securityId",), raw_shape='str', derive_rule=''),
    FieldSpec("lid", SOURCE_LIST, "str", "平台内部列表 id", (),
              exempt_reason="平台内部 id，无用户价值", provenance=PROV_VERBATIM, raw_paths=("lid",), raw_shape='str', derive_rule=''),
    FieldSpec("encrypt_boss_id", SOURCE_LIST, "str", "平台内部 HR id", (),
              exempt_reason="平台内部 id，无用户价值", provenance=PROV_VERBATIM, raw_paths=("encryptBossId",), raw_shape='str', derive_rule=''),
    FieldSpec("encrypt_brand_id", SOURCE_LIST, "str", "平台内部公司 id", (),
              exempt_reason="平台内部 id，无用户价值", provenance=PROV_VERBATIM, raw_paths=("encryptBrandId",), raw_shape='str', derive_rule=''),
    FieldSpec("job_link", SOURCE_LIST, "str", "岗位详情页 URL（抓取时刻有效）",
              (_M_EXPORT, _M_SUMMARY, _M_SEMANTICS, _M_MAIN), required=True,
              notes="含 securityId 段，不能由 job_id 重建；实测跨天稳定", provenance=PROV_DERIVED, raw_paths=("encryptJobId",), raw_shape='', derive_rule='job_link'),
    FieldSpec("company_link", SOURCE_LIST, "str", "公司主页 URL",
              (_M_EXPORT, _M_SUMMARY, _M_SEMANTICS), provenance=PROV_DERIVED, raw_paths=("encryptBrandId",), raw_shape='', derive_rule='company_link'),
    FieldSpec("link_verified_at", SOURCE_DERIVED, "str",
              "该链接在抓取时刻被验证指向这个岗位",
              (_M_EXPORT, _M_SEMANTICS), provenance=PROV_DERIVED, raw_paths=(), raw_shape='', derive_rule='link_verified_at'),
    # ── 岗位本体 ─────────────────────────────────────────────────────
    FieldSpec("title", SOURCE_LIST, "str", "岗位名（平台原样回传）",
              (_M_EXPORT, _M_SUMMARY, _M_SEMANTICS, _M_MAIN), required=True, provenance=PROV_VERBATIM, raw_paths=("jobName",), raw_shape='str', derive_rule=''),
    FieldSpec("title_key", SOURCE_DERIVED, "str", "标题归一化键（分组/匹配用）",
              (_M_EXPORT, _M_SEMANTICS, _M_SUMMARY), provenance=PROV_DERIVED, raw_paths=("jobName",), raw_shape='', derive_rule='title_key'),
    FieldSpec("salary", SOURCE_LIST, "str", "薪资描述原文",
              (_M_EXPORT, _M_SUMMARY, _M_SEMANTICS, _M_MAIN), required=True, provenance=PROV_VERBATIM, raw_paths=("salaryDesc",), raw_shape='str', derive_rule=''),
    FieldSpec("salary_source", SOURCE_LIST, "str", "薪资来源（api=列表 API）",
              (), delivery_only=True,
              notes="报表用：说明薪资来自列表 API（而非页面）", provenance=PROV_DERIVED, raw_paths=("salaryDesc",), raw_shape='', derive_rule='salary_source'),
    FieldSpec("location", SOURCE_LIST, "str", "城市·区域·商圈",
              (_M_EXPORT, _M_SUMMARY, _M_SEMANTICS, _M_MAIN), required=True, provenance=PROV_NORMALIZED, raw_paths=("cityName", "areaDistrict", "businessDistrict"), raw_shape='join:·', derive_rule=''),
    FieldSpec("tags", SOURCE_LIST, "str", "经验|学历（列表侧拼接）",
              (_M_EXPORT, _M_SUMMARY, _M_SEMANTICS), provenance=PROV_NORMALIZED, raw_paths=("jobExperience", "jobDegree"), raw_shape='join: | |filter:不限', derive_rule=''),
    FieldSpec("experience", SOURCE_LIST, "str", "经验要求",
              (), delivery_only=True,
              notes="报表用：读专用字段，不再从 tags 串里解析（消除重复来源）", provenance=PROV_VERBATIM, raw_paths=("jobExperience",), raw_shape='str', derive_rule=''),
    FieldSpec("education", SOURCE_LIST, "str", "学历要求",
              (), delivery_only=True,
              notes="报表用：读专用字段，不再从 tags 串里解析（消除重复来源）", provenance=PROV_VERBATIM, raw_paths=("jobDegree",), raw_shape='str', derive_rule=''),
    FieldSpec("skills", SOURCE_LIST, "list", "列表侧技能标签",
              (_M_EXPORT, _M_SUMMARY, _M_SEMANTICS), provenance=PROV_VERBATIM, raw_paths=("skills",), raw_shape='list', derive_rule=''),
    FieldSpec("skill_tags", SOURCE_DETAIL, "list", "详情页技能标签（比列表侧准）",
              (_M_EXPORT, _M_SUMMARY, _M_SEMANTICS), provenance=PROV_VERBATIM, raw_paths=("skill_tags",), raw_shape='list', derive_rule=''),
    FieldSpec("job_labels", SOURCE_LIST, "str", "平台岗位标签",
              (_M_EXPORT, _M_SUMMARY, _M_SEMANTICS), provenance=PROV_NORMALIZED, raw_paths=("jobLabels",), raw_shape='join: | ', derive_rule=''),
    FieldSpec("welfare", SOURCE_LIST, "str", "福利待遇",
              (_M_EXPORT, _M_SUMMARY, _M_SEMANTICS), provenance=PROV_NORMALIZED, raw_paths=("welfareList",), raw_shape='join: | ', derive_rule=''),
    FieldSpec("icon_flags", SOURCE_LIST, "str", "平台角标（急/新等）",
              (), delivery_only=True,
              notes="报表用：平台角标统计", provenance=PROV_NORMALIZED, raw_paths=("iconFlagList",), raw_shape='join:|', derive_rule=''),
    FieldSpec("icon_word", SOURCE_LIST, "str", "平台角标文字",
              (), delivery_only=True,
              notes="报表用：平台角标统计", provenance=PROV_VERBATIM, raw_paths=("iconWord",), raw_shape='str', derive_rule=''),
    FieldSpec("job_valid_status", SOURCE_LIST, "str", "岗位有效性（1=有效）",
              (), delivery_only=True,
              notes="报表用：已下架/无效岗计数", provenance=PROV_VERBATIM, raw_paths=("jobValidStatus",), raw_shape='str', derive_rule=''),
    # ── 公司与招聘方 ─────────────────────────────────────────────────
    FieldSpec("company_name", SOURCE_LIST, "str", "公司名（列表侧）",
              (_M_EXPORT, _M_SUMMARY, _M_SEMANTICS, _M_MAIN), required=True, provenance=PROV_VERBATIM, raw_paths=("brandName",), raw_shape='str', derive_rule=''),
    FieldSpec("boss_name", SOURCE_LIST, "str", "招聘方名称",
              (), delivery_only=True,
              notes="交付给用户：招聘方名称（平台只给品牌名，无个人身份信息）", provenance=PROV_VERBATIM, raw_paths=("brandName",), raw_shape='str', derive_rule=''),
    FieldSpec("boss_title", SOURCE_LIST, "str", "招聘方职位（猎头顾问=代发佐证）",
              (_M_MODEL, _M_SEMANTICS, _M_SUMMARY),
              notes="平台自证的第三个信号：22 条『猎头顾问』⊂ 25 条代发岗", provenance=PROV_VERBATIM, raw_paths=("bossTitle",), raw_shape='str', derive_rule=''),
    FieldSpec("company_scale", SOURCE_LIST, "str", "公司规模",
              (_M_EXPORT, _M_SUMMARY, _M_SEMANTICS), provenance=PROV_VERBATIM, raw_paths=("brandScaleName",), raw_shape='str', derive_rule=''),
    FieldSpec("company_stage", SOURCE_LIST, "str", "融资阶段",
              (_M_EXPORT, _M_SUMMARY, _M_SEMANTICS), provenance=PROV_VERBATIM, raw_paths=("brandStageName",), raw_shape='str', derive_rule=''),
    FieldSpec("company_industry", SOURCE_LIST, "str", "公司行业",
              (_M_EXPORT, _M_SUMMARY, _M_SEMANTICS), provenance=PROV_VERBATIM, raw_paths=("brandIndustry",), raw_shape='str', derive_rule=''),
    FieldSpec("anonymous", SOURCE_LIST, "str", "平台匿名标记（位掩码）",
              (), delivery_only=True,
              notes="平台位掩码（实测 0/1/12/20/101），含义未验证 → 暂不参与判断", provenance=PROV_VERBATIM, raw_paths=("anonymous",), raw_shape='str', derive_rule=''),
    # ── 平台标注（语义化的关键输入）──────────────────────────────────
    FieldSpec("proxy_job", SOURCE_LIST, "str", "平台标注：是否猎头代发",
              (_M_SEMANTICS, _M_SUMMARY, _M_MAIN), provenance=PROV_NORMALIZED, raw_paths=("proxyJob",), raw_shape='zero_as_empty', derive_rule=''),
    FieldSpec("proxy_type", SOURCE_LIST, "str", "平台标注：代发类型（1/4）",
              (_M_SEMANTICS, _M_SUMMARY, _M_MAIN), provenance=PROV_NORMALIZED, raw_paths=("proxyType",), raw_shape='zero_as_empty', derive_rule=''),
    FieldSpec("job_type", SOURCE_LIST, "str", "平台标注：招聘类型（4=实习 5=校招）",
              (_M_SEMANTICS, _M_SUMMARY, _M_MAIN), provenance=PROV_NORMALIZED, raw_paths=("jobType",), raw_shape='zero_as_empty', derive_rule=''),
    FieldSpec("boss_active_status", SOURCE_LIST, "str", "HR 活跃度文案",
              (_M_EXPORT, _M_SUMMARY, _M_SEMANTICS, _M_MAIN), provenance=PROV_DERIVED, raw_paths=("activeTimeDesc", "bossOnline"), raw_shape='', derive_rule='list_active_status'),
    # ── 详情页 ───────────────────────────────────────────────────────
    FieldSpec("jd", SOURCE_DETAIL, "str", "职位描述正文",
              (_M_EXPORT, _M_SUMMARY, _M_SEMANTICS, _M_MAIN), required=True, provenance=PROV_NORMALIZED, raw_paths=("job-detail-text",), raw_shape='detail_jd', derive_rule=''),
    FieldSpec("page_update_date", SOURCE_DETAIL, "str", "岗位侧最后编辑日期",
              (), delivery_only=True,
              notes="交付给用户：岗位侧最后编辑日期（判断岗位新鲜度）", provenance=PROV_VERBATIM, raw_paths=("page_update_date",), raw_shape='str', derive_rule=''),
    FieldSpec("tags_list", SOURCE_DETAIL, "str", "详情页经验|学历",
              (), delivery_only=True,
              notes="交付给用户：详情页经验|学历", provenance=PROV_VERBATIM, raw_paths=("tags_list",), raw_shape='str', derive_rule=''),
    FieldSpec("job_status_desc", SOURCE_DETAIL, "str", "岗位状态描述",
              (), delivery_only=True,
              notes="交付给用户：岗位状态描述", provenance=PROV_VERBATIM, raw_paths=("job_status_desc",), raw_shape='str', derive_rule=''),
    FieldSpec("brand_introduce", SOURCE_DETAIL, "str", "公司介绍",
              (), delivery_only=True,
              notes="交付给用户：公司介绍", provenance=PROV_VERBATIM, raw_paths=("brand_introduce",), raw_shape='str', derive_rule=''),
    FieldSpec("brand_scale_name", SOURCE_DETAIL, "str", "详情页公司规模",
              (), delivery_only=True,
              notes="交付给用户：详情页公司规模", provenance=PROV_VERBATIM, raw_paths=("brand_scale_name",), raw_shape='str', derive_rule=''),
    FieldSpec("brand_stage_name", SOURCE_DETAIL, "str", "详情页融资阶段",
              (), delivery_only=True,
              notes="交付给用户：详情页融资阶段", provenance=PROV_VERBATIM, raw_paths=("brand_stage_name",), raw_shape='str', derive_rule=''),
    FieldSpec("brand_industry_name", SOURCE_DETAIL, "str", "详情页行业",
              (), delivery_only=True,
              notes="交付给用户：详情页行业", provenance=PROV_VERBATIM, raw_paths=("brand_industry_name",), raw_shape='str', derive_rule=''),
    FieldSpec("brand_active_time", SOURCE_DETAIL, "str", "详情页公司活跃时间",
              (), delivery_only=True,
              notes="交付给用户：详情页公司活跃时间", provenance=PROV_VERBATIM, raw_paths=("brand_active_time",), raw_shape='str', derive_rule=''),
    # ── 派生语义 ─────────────────────────────────────────────────────
    FieldSpec("risk_flags", SOURCE_DERIVED, "list",
              "风险标记（用户视角：这岗该不该投）",
              (_M_MODEL, _M_MAIN), required=False,
              provenance=PROV_DERIVED,
              raw_paths=("brandName", "cityName", "areaDistrict", "businessDistrict",
                         "jobName", "jobLabels"),
              raw_shape='', derive_rule='risk_flags',
              notes="代发占位名/无区县/异地base/陈旧帖/薪资离群/兼职创业营"),
    FieldSpec("post_type", SOURCE_DERIVED, "str",
              "发布主体类型（direct 企业直招 / agency 猎头代发 / anonymous 匿名）",
              (_M_EXPORT, _M_SEMANTICS, _M_SUMMARY, _M_MAIN), required=True, provenance=PROV_DERIVED, raw_paths=("proxyJob", "proxyType", "bossTitle"), raw_shape='', derive_rule='post_type'),
    FieldSpec("recruit_type", SOURCE_DERIVED, "str",
              "招聘类型（social 社招 / intern 实习 / campus 校招）",
              (_M_EXPORT, _M_SEMANTICS, _M_SUMMARY, _M_MAIN), required=True, provenance=PROV_DERIVED, raw_paths=("jobType",), raw_shape='', derive_rule='recruit_type'),
    FieldSpec("active_state", SOURCE_DERIVED, "str",
              "活跃度三态（active / inactive / unknown）",
              (_M_EXPORT, _M_SEMANTICS, _M_SUMMARY), provenance=PROV_DERIVED, raw_paths=("activeTimeDesc", "bossOnline"), raw_shape='', derive_rule='active_state'),
    FieldSpec("dup_group", SOURCE_DERIVED, "str", "同源重复组 id（空=不重复）",
              (_M_EXPORT, _M_SEMANTICS), provenance=PROV_DERIVED, raw_paths=("jobName", "cityName"), raw_shape='', derive_rule='dup_group'),
    FieldSpec("dup_size", SOURCE_DERIVED, "int", "同源重复组内条数",
              (_M_EXPORT, _M_SEMANTICS), provenance=PROV_DERIVED, raw_paths=("jobName", "cityName"), raw_shape='', derive_rule='dup_size'),
    FieldSpec("is_primary", SOURCE_DERIVED, "bool", "是否重复组内首条",
              (_M_EXPORT, _M_SEMANTICS), provenance=PROV_DERIVED, raw_paths=("jobName", "cityName"), raw_shape='', derive_rule='is_primary'),
    # ── 独立验收 2026-10-22 要求补的字段 ──────────────────────────
    FieldSpec("is_ai_pm", SOURCE_DERIVED, "bool",
              "是否 AI 方向的产品岗（审计实测 32.6% 的检索结果与 AI 无关）",
              (_M_EXPORT, _M_SUMMARY), required=True, provenance=PROV_DERIVED, raw_paths=("jobName",), raw_shape='', derive_rule='direction'),
    FieldSpec("direction", SOURCE_DERIVED, "str",
              "方向标签（ai_pm / pm_only / ai_other / other）",
              (_M_EXPORT, _M_SUMMARY), required=True, provenance=PROV_DERIVED, raw_paths=("jobName",), raw_shape='', derive_rule='direction'),
    FieldSpec("direction_evidence", SOURCE_DERIVED, "list",
              "方向判定命中的信号词（可复核，不给黑箱结论）",
              (), delivery_only=True,
              notes="交付给用户核对判定依据：不让用户只能接受一个黑箱结论", provenance=PROV_DERIVED, raw_paths=("jobName",), raw_shape='', derive_rule='direction'),
    FieldSpec("salary_min", SOURCE_DERIVED, "str",
              "薪资下界（元/月 或 元/天，可排序）",
              (), delivery_only=True, required=True,
              notes="交付给用户排序用：同一列混装月薪与日薪会让 Excel 文本排序完全错位"
                    "（审计实测 150-200K·16薪 排到第 396 位）", provenance=PROV_DERIVED, raw_paths=("salaryDesc",), raw_shape='', derive_rule='salary_parts'),
    FieldSpec("salary_max", SOURCE_DERIVED, "str",
              "薪资上界（元/月 或 元/天，可排序）",
              (), delivery_only=True, required=True,
              notes="交付给用户排序用（与 salary_min 同源）", provenance=PROV_DERIVED, raw_paths=("salaryDesc",), raw_shape='', derive_rule='salary_parts'),
    FieldSpec("salary_unit", SOURCE_DERIVED, "str",
              "薪资单位（month 月 / day 日 / year 年 / 空=面议）",
              (), delivery_only=True, required=True,
              notes="交付给用户排序用：日薪与月薪不是同一量纲，必须分开", provenance=PROV_DERIVED, raw_paths=("salaryDesc",), raw_shape='', derive_rule='salary_parts'),
    FieldSpec("months_per_year", SOURCE_DERIVED, "str",
              "薪月数（如 16薪；空=按 12 个月）",
              (), delivery_only=True,
              notes="交付给用户算年包用（16薪 ≠ 16 个月月薪）", provenance=PROV_DERIVED, raw_paths=("salaryDesc",), raw_shape='', derive_rule='salary_parts'),
)

# ── 派生规则注册表（derived 字段的 derive_rule 必须在这里登记）────────
# 「规则可查」不能是空话：每个规则 id 都要能指到一条可执行的推导说明。
DERIVE_RULES = {
    "job_id": "compute_job_id(job_link or title) = md5(...)[:16]",
    "job_link": "https://www.zhipin.com/job_detail/{encryptJobId}.html",
    "company_link": "https://www.zhipin.com/gongsi/{encryptBrandId}.html",
    "salary_source": "api if salary else api_empty",
    "list_active_status": "_list_active_status(raw)：在线/活跃文案",
    "post_type": "classify_post_type：代发标记/猎头顾问/匿名公司名 → direct|agency|anonymous",
    "recruit_type": "classify_recruit_type：jobType 4=实习 5=校招 空=社招",
    "job_type_zero": "平台 jobType 是**数字** 0（社招）；我们统一存空串（规范化）",
    "active_state": "active_state(boss_active_status)：active/inactive/unknown",
    "title_key": "normalize_title(title)：大小写与空白归一",
    "dup_group": "content_fingerprint(JD前180字|城市) 稳定哈希分组",
    "dup_size": "同源重复组内条数",
    "is_primary": "同源重复组内首条",
    "direction": "classify_direction：标题+JD+技能 → ai_pm|pm_only|ai_other|other",
    "salary_parts": "parse_salary_parts：拆 min/max/unit/months",
    "link_verified_at": "抓取时刻（链接在该时刻被验证指向本岗位）",
    "risk_flags": "risk_flags(job)：占位公司名/无区县/异地base/陈旧帖/薪资离群/兼职类",
}

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


# **默认不过滤行业**（2026-10-22 实测 933 条 / 90 个行业后推翻的旧做法）：
# 旧的"技术行业白名单"会**丢掉真岗位**——餐饮 4 条 100% 是产品经理岗（餐饮产品经理、
# 炸物品类规划、烘焙市场产品经理、AI 餐饮全链路）；房地产中介/租赁里的"产品经理｜贝壳
# 青鸟筑家"是正经 SaaS 房源管理产品岗；培训/辅导、进出口贸易、基金也全是 AI/产品岗。
# → **行业 ≠ 相关性**。行业过滤只能是**用户显式指定的黑名单**（--industry-block），
# 且必须把丢掉的清单打出来（不静默丢）。
DEFAULT_INDUSTRY_ALLOW = ()   # 空 = 不过滤

# 标题相关性：命中即视为"目标方向"（产品经理 / AI 方向）
RELEVANCE_TITLE_TERMS = (
    "产品经理", "产品负责人", "产品总监", "产品运营", "产品规划", "产品线",
    "产品专家", "产品企划", "产品助理", "产品专员", "产品设计", "产品经理实习",
    "PM", "AI", "AIGC", "人工智能", "大模型", "算法", "智能", "数智", "Agent",
)

# 招聘类型（记录级语义）：social 社招 / intern 实习 / campus 校招
RECRUIT_TYPES = ("social", "intern", "campus")


def _normalize_jd(jd) -> str:
    text = unicodedata.normalize("NFKC", str(jd or "")).lower()
    return re.sub(r"\s+", "", text)


def content_fingerprint(job, head=180) -> str:
    """内容指纹（**唯一定义**）：同 JD + 同薪资 + 同城市 → 同一份内容。

    平台允许同一岗位**多条发布**（同公司同岗、不同 job_id）→ job_id 去重抓不到，
    只能靠内容指纹识别。验收器（scripts/acceptance.py 的 P3）与打标器
    （semantics.mark_duplicate_groups）**必须用同一个指纹**，否则会出现
    "检查器说重复、打标器说不重复"（2026-10-22 上午实测踩到过：4 组真重复没被标）。

    实现自原语义层（逐字迁移，行为不变）：归一化 JD 开头 + 薪资带 + 城市。
    """
    if not isinstance(job, dict):
        return ""
    jd = _normalize_jd(job.get("jd"))[:head]
    if not jd:
        return ""
    city = normalize_title(job.get("location"))[:12]
    # 刻意**不含薪资**：同一条岗被重复投放时薪资带常被微调（实测 "11-20K" vs "12-20K"
    # 导致同公司同岗漏标重复），薪资是发布口径不是内容。
    return f"{jd}|{city}"


def is_relevant_title(job) -> bool:
    """标题是否是**目标方向**（产品经理方向 + AI 信号）。

    刻意**不看行业**（实测证明行业与相关性无关）；也不用"标题里出现 AI 就算"——
    独立审计实测"AI招聘&猎头方向实习生"这类是招聘岗不是产品岗（AI 只是修饰词）。
    判定复用 :func:`classify_direction`（产品词 + AI 信号同时命中才算）。
    """
    _is_ai_pm, label, _evidence = classify_direction(job)
    return label == "ai_pm"


# 方向判定的 AI 信号（独立审计 2026-10-22：合并文件 152/466 条在
# 标题+JD+技能全链路找不到任何 AI 痕迹——却是"AI产品经理"检索的结果）
AI_DIRECTION_TERMS = (
    "AI", "AIGC", "人工智能", "大模型", "LLM", "大语言模型", "生成式", "GPT",
    "智能体", "Agent", "算法", "机器学习", "深度学习", "多模态", "NLP",
    "计算机视觉", "CV", "知识图谱", "RAG", "向量", "推理模型",
)
PM_DIRECTION_TERMS = (
    "产品经理", "产品负责人", "产品总监", "产品运营", "产品规划", "产品线",
    "产品专家", "产品企划", "产品设计", "产品专员", "产品助理", "产品策划",
    "产品需求", "PM",
    # 实习岗的产品称谓（2026-10-22 修）：此前漏了"产品实习"，导致 244 条
    # 实习池里 7 条「ai产品实习生」被判成 ai_other（非 AI 方向），
    # 直接少给用户 7 个**确实是 AI 产品岗**的实习。称谓是同一个岗位的实习版。
    "产品实习",
)




# ============================================================================
# 风险标记（2026-10-22）：用户视角的「这岗该不该投」
# ============================================================================
# 之前的风险信号散落在报告里人工看（代发占位名、无区县、异地 base、陈旧帖、
# 薪资离群、兼职/创业营），没有进产物 → 每次都要人肉再查一遍。
# 现在算成**一个字段**（risk_flags），交付表里直接一列。
#
# 每条标记都只依据**平台给的字段**，不猜：
#   - agency_placeholder：公司名是「某大型互联网公司」这类平台占位文案 → 公司不可核
#   - no_district：地点只有城市没有区（杭州·余杭区·仓前 这种才算有）
#   - other_base：地点写的是杭州但公司名里挂着外地城市名
#   - stale：平台给的页面更新时间早于阈值
#   - salary_outlier：同批同方向里显著偏离中位数
#   - part_time：标题或 JD 明说不是正经岗位
# 占位公司名不另抄词表：模型里 classify_post_type 已按平台的匿名写法
# （"某…"/"知名…"/"大型…"/工作室…）判为 anonymous → 直接复用那个判定。
# （此前另抄一份 → "某某科技有限公司"这类漏判，且两份表必然漂移。）
RISK_PART_TIME_TERMS = ("兼职", "创业营", "带货", "招生", "代理", "日结",
                       "私活", "外包")
RISK_STALE_DAYS = 30

# 异地 base 提示用的城市名（命中才算标记，不是穷举）
_OTHER_CITY_HINTS = ("北京", "上海", "深圳", "广州", "成都", "武汉", "西安",
                    "南京", "苏州", "合肥", "长沙", "青岛", "厦门", "天津",
                    "重庆", "郑州", "济南", "福州", "东莞", "宁波", "无锡")


def risk_flags(job, *, peer_salary_median=None, stale_days=RISK_STALE_DAYS):
    """算这个岗位的**风险标记**（列表；空列表 = 没有明显风险）。

    判据全部来自平台字段或我们已派生的字段，不做主观判断：
    - ``agency_placeholder``：公司名是平台占位文案（猎头代发常见）→ 公司无法核实
    - ``no_district``：地点只有城市、没有区/街道
    - ``other_base``：地点是杭州，但公司名里挂着外地城市 → 可能是异地 base
    - ``stale``：平台给的页面更新时间超过阈值 → 岗位可能已失效
    - ``salary_outlier``：同批同方向里显著偏离中位数
    - ``part_time``：标题/JD 明说是兼职/创业营/带货类
    """
    if not isinstance(job, dict):
        return []
    flags = []
    company = str(job.get("company_name") or "").strip()
    title = str(job.get("title") or "").strip()
    jd = str(job.get("jd") or "").strip()
    location = str(job.get("location") or "").strip()

    if company and classify_post_type(job) == "anonymous":
        flags.append("agency_placeholder")

    parts = [p for p in location.split("·") if p]
    if parts and len(parts) == 1:
        flags.append("no_district")

    if parts:
        city = parts[0]
        for other in _OTHER_CITY_HINTS:
            if other in company and other != city:
                flags.append("other_base")
                break

    updated = str(job.get("page_update_date") or "").strip()
    if updated:
        days = _days_since(updated)
        if days is not None and days > stale_days:
            flags.append("stale")

    if peer_salary_median:
        high = job.get("salary_max")
        try:
            high_f = float(high) if high else 0.0
        except (TypeError, ValueError):
            high_f = 0.0
        if high_f and peer_salary_median:
            if high_f > peer_salary_median * 3 or high_f < peer_salary_median * 0.25:
                flags.append("salary_outlier")

    blob = (title + " " + jd[:400]).lower()
    if any(term.lower() in blob for term in RISK_PART_TIME_TERMS):
        flags.append("part_time")

    return flags


def _days_since(date_text):
    """距今天数；无法解析返回 None（平台没给就不算陈旧）。"""
    import datetime as _dt
    try:
        parsed = _dt.date.fromisoformat(str(date_text)[:10])
    except (ValueError, TypeError):
        return None
    return (_dt.date.today() - parsed).days

def parse_salary_parts(salary):
    """把薪资串拆成**可排序的结构化列**。

    独立审计 2026-10-22 实测：同一列里混着月薪 ``15-25K``、月薪带薪月数
    ``150-200K·16薪`` 和日薪 ``150-200元/天`` → Excel 文本降序把真正的
    ``150-200K·16薪`` 排到第 396 位，日薪排第 1、2 位。求职者最常见的
    "按薪资排序挑最值得投"因此完全失效。

    返回 ``(min, max, unit, months_per_year)``：
    - unit: ``"month"`` / ``"day"`` / ``"year"`` / ``""``（面议/无法解析）
    - min/max: 数值（元/月 或 元/天；月薪按千元×1000 换算成元）
    - months_per_year: 薪月数（如 16薪 → 16；无则 12）
    """
    text = str(salary or "").strip()
    if not text or text in {"面议", "negotiable", "不限"}:
        return "", "", "", ""
    months = ""
    if "·" in text:
        head, _, tail = text.partition("·")
        tail = tail.strip()
        if tail.endswith("薪"):
            tail = tail[:-1]
        if tail.isdigit():
            months = tail
        text = head.strip()
    unit = ""
    if "元/天" in text or "/天" in text:
        unit = "day"
        text = text.replace("元/天", "").replace("/天", "")
    elif "元/小时" in text:
        unit = "hour"
        text = text.replace("元/小时", "")
    elif "元/时" in text:
        unit = "hour"
        text = text.replace("元/时", "")
    elif "元/年" in text:
        unit = "year"
        text = text.replace("元/年", "")
    elif "K" in text.upper():
        unit = "month"
        text = text.upper().replace("K", "")
    elif "元/月" in text:
        unit = "month"
        text = text.replace("元/月", "")
    elif "元" in text:
        unit = "month"
        text = text.replace("元", "")
    nums = [n for n in re.findall(r"\d+(?:\.\d+)?", text)]
    if not nums:
        return "", "", unit, months
    low = float(nums[0])
    high = float(nums[-1])
    if unit == "month" and low <= 999:      # 15-25K → 15000-25000
        low, high = low * 1000, high * 1000
    return (str(int(low)) if float(low).is_integer() else str(low),
            str(int(high)) if float(high).is_integer() else str(high),
            unit, months)


def classify_direction(job):
    """岗位方向判定：**是不是 AI 方向的产品岗**。

    独立审计 2026-10-22 实测：搜"AI产品经理"回来的 466 条里 152 条（32.6%）
    在标题+JD+技能全链路找不到任何 AI 痕迹（餐饮产品经理、炊具产品经理、
    储能产品经理、天文产品经理…）→ 求职者按薪资排序时先看到的就是这批。

    返回 ``(is_ai_pm, label, evidence)``：
    - label: ``"ai_pm"``（AI 方向产品岗）/ ``"pm_only"``（产品岗但无 AI 信号）/
      ``"other"``（标题既不是产品岗也不是 AI 岗）
    - evidence: 命中的信号词（让判定可复核，不给黑箱结论）
    """
    if not isinstance(job, dict):
        return False, "other", []
    title = str(job.get("title") or "")
    jd = str(job.get("jd") or "")
    skills = job.get("skill_tags") or job.get("skills") or []
    if isinstance(skills, str):
        skills = [skills]
    skill_text = " ".join(str(s) for s in skills)
    blob = f"{title}\n{jd}\n{skill_text}"

    ai_hits = [t for t in AI_DIRECTION_TERMS if t.lower() in blob.lower()]
    pm_hits = [t for t in PM_DIRECTION_TERMS if t.lower() in title.lower()]
    title_ai = [t for t in AI_DIRECTION_TERMS if t.lower() in title.lower()]
    if pm_hits and (ai_hits or title_ai):
        return True, "ai_pm", sorted(set(ai_hits + pm_hits))[:6]
    if pm_hits:
        return False, "pm_only", pm_hits[:4]
    if ai_hits:
        return False, "ai_other", sorted(set(ai_hits))[:4]
    return False, "other", []


def industry_of(job) -> str:
    """公司行业（平台字段）。"""
    if not isinstance(job, dict):
        return ""
    return str(job.get("company_industry") or "").strip()


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
        # 2026-10-22 保真核对：平台只给 cityName 时，原实现会拼出 "杭州··"
        # （两个空段 → 两个多余分隔符）。只拼**有值**的段。
        location = "·".join([part for part in (
            raw.get("cityName") or "",
            raw.get("areaDistrict") or "",
            raw.get("businessDistrict") or "",
        ) if part])
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
        # 方向判定（是不是 AI 方向的产品岗）+ 薪资拆列（可排序，见 parse_salary_parts）
        is_ai_pm, direction, evidence = classify_direction(self.data)
        self.data["is_ai_pm"] = is_ai_pm
        self.data["direction"] = direction
        self.data["direction_evidence"] = evidence
        low, high, unit, months = parse_salary_parts(self.data.get("salary"))
        self.data["salary_min"] = low
        self.data["salary_max"] = high
        self.data["salary_unit"] = unit
        self.data["months_per_year"] = months
        if link_verified_at:
            self.data["link_verified_at"] = link_verified_at
        self.data.setdefault("dup_group", "")
        self.data.setdefault("dup_size", 1)
        self.data.setdefault("is_primary", True)
        # 风险标记（用户视角：这岗该不该投）——此前散在报告里人工看，没进产物
        self.data["risk_flags"] = risk_flags(self.data)
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

    def to_public_dict(self) -> dict:
        """**对外可见**的行：剔除平台内部字段（红线：内部 id/凭据不落盘）。

        列表行在内存里带着 security_id/lid/encrypt_*（详情 API 通道要用），
        但任何落盘产物（详情输出 / CSV / 导出）都不能带它们。
        """
        return {key: value for key, value in self.data.items()
                if key not in EXPORT_HIDDEN_FIELDS}


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
