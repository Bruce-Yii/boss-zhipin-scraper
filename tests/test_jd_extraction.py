"""JD 抽取回归测试 —— 2026-10-22 独立审计证实的三类详情抽取缺陷。

为什么单独一个文件：``tests/test_chrome_setup.py`` 是 2026-08 起累积的大文件，
塞进它会让「这次修的是哪几个缺陷」不可见。这里只放**详情正文抽取**的反例用例。

**纯函数级**：只喂构造好的 ``extracted`` / ``page_text``，**不需要 Chrome / 网络**。

三类缺陷（夹具全部取自线上详情页原文，job_id 见用例注释）：

* **A 头部被截断**（10 条）：正文里再次出现「职位描述」时，抽取无条件按标记切 →
  标记**之前**的正文整段丢失。最严重 ``81a080c6c9a9187e``（蚂蚁集团 研究型实习生-
  可信AI安全）：交付 400 字 / 线上 619 字，丢了开头 219 字（35%），交付的 JD 以
  孤立的「：」开头。
* **B 尾部混进招聘者卡片**（5 条）：卡片形态随页面改版漂移，旧判据（倒数第 2 行是
  孤立的「·」）失配 → 招聘者姓名/活跃度/公司整块留在 JD 里。
* **C 平台技能标签粘在 JD 开头**（4 条）：页面容器兜底取正文时，技能标签和正文在
  同一块里 → 标签行粘在 JD 最前面。

外加一条**防漂移守卫**：垃圾尾（页面模块文案）的判定口径只有一处——
``scripts/semantics.py`` 的 ``check_invariants``（验收器已改为委托它）；
实现侧不许再抄第二份标记表。
"""

import atexit
import importlib.util
import inspect
import os
import pathlib
import re
import sys
import tempfile
import unittest
from unittest import mock

# Windows 控制台默认 GBK，断言里含中文/emoji 会 UnicodeEncodeError
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

# 状态目录（锁 / cooldown / 审计 / 日预算 / 坏页台账）重定向到临时目录——
# 单测绝不许改写用户真实的 ~/.boss-zhipin-scraper/。
_TMP = tempfile.TemporaryDirectory(prefix="boss-jd-test-")
atexit.register(_TMP.cleanup)
os.environ.setdefault("BOSS_STATE_DIR", _TMP.name)
os.environ.setdefault("BOSS_AUDIT_PATH", os.path.join(_TMP.name, "risk_events.jsonl"))
os.environ.setdefault("BOSS_TRACE_PATH", os.path.join(_TMP.name, "runner_trace.jsonl"))
os.environ.setdefault("BOSS_DAILY_BUDGET_PATH", os.path.join(_TMP.name, "daily_budget.json"))
os.environ.setdefault("BOSS_KNOWN_BAD_PATH", os.path.join(_TMP.name, "known_bad_jobs.json"))

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "boss_cdp_raw.py"
SEMANTICS_PATH = ROOT / "scripts" / "semantics.py"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_scraper():
    sys.modules.setdefault("websocket", mock.Mock())
    sys.modules.setdefault("requests", mock.Mock())
    module = load_module("boss_cdp_raw", SCRIPT_PATH)
    # 日预算/坏页台账：单测用例共用 job_id 会互相污染，放行即可
    module._budget_consume = (
        lambda n=1, limit=None, path=None, job_id=None: (True, 0, limit or 0, ""))
    module._known_failures.is_known = lambda job_id: False
    module._known_failures.record = lambda *args, **kwargs: None
    return module


# ===========================================================================
# 线上原文夹具（逐字取自详情页，勿手工改写）
# ===========================================================================

# 81a080c6c9a9187e 蚂蚁集团「研究型实习生-可信AI安全」——线上 619 字，
# 正文自带小标题「职位描述：」，且**前面还有一整段**「研究领域：…」。
JBD_81A080C6_LIVE = "\n".join([
    "研究领域：",
    "随着大模型及Agent技术的持续演进，其在内容理解、决策推理、任务执行等方面的能⼒显著提升。"
    "但与此同时，模型与Agent的安全性、可控性和可信度问题 也逐渐成为制约其⼤规模应⽤的关键因素。"
    "当前的防护措施主要集中在后验过滤或人工规则层⾯，难以适应智能体的自主性需求。"
    "本项目聚焦于轻量化、可验证的AI安全对齐机制研究，从模型行为约束、风险检测与安全提示三个维度，"
    "探索小规模可验证的技术路线，为后续大模型安全体系奠定基础。",
    "职位描述：",
    "1、大模型对抗攻击：研究大模型（llm，vlm，agent）存在的安全性（安全与幻觉）问题，"
    "研究大模型对抗攻击/越狱算法和生成式数据增强。",
    "2、大模型对抗防御：研究大模型（llm，vlm，agent）安全防御算法机制，"
    "对大模型输入输出做识别、改写、抑制。",
    "3、大模型训练：内生安全对齐，使用continue pretrain、MoE、RAG、AutoPrompt等方法解决大模型安全相关问题。",
    "职位要求：",
    "1、具有多模内容理解、大模型、AI安全相关工作经验优先；",
    "2、计算机、电子等相关专业背景；具备扎实的机器学习基础和较强的学习能力；",
    "3、熟悉PyTorch、Tensorflow相关深度学习算法框架，有相应的模型设计和实现经验；",
    "4、在机器学习/CV/NLP/人工智能相关领域有顶会论文或者有算法竞赛top经验者优先。",
    "5、有一定工程基础或者网络安全经验者有加分。",
    "6、至少6个月的全职工作。",
])

# ecad3d515b84fbfc 钉钉「千问办公-AI产品实习生」——线上正文**第一行就是**
# 「职位描述：」，这一行该去掉，但后面一个字都不能少。
JBD_ECAD3D51_LIVE = "\n".join([
    "职位描述：",
    "1、深度参与千问办公（AI 桌面智能体）的核心体验设计与功能迭代，从用户视角出发定义智能体的"
    "交互模式、任务流程与反馈机制； 2、参与智能体能力建设，包括 Skills ⽣态、Memory 记忆体系、"
    "上下⽂管理等模块的产品设计，理解 Agent 能⼒边界并推动体验落地； 3、跟踪 AI Agent 领域"
    "前沿产品动态（Claude Cowork、Codex、WorkBuddy 等），输出有深度的竞品分析与产品洞察； "
    "4、参与用户研究与行为数据分析，将真实反馈转化为明确的产品优化⽅向，持续打磨智能体的"
    "端到端使用体验； 5、协助撰写产品文档、PRD 及功能说明，参与产品从定义到上线的全流程。",
    "职位要求：",
    "1、本科及以上学历在读，计算机、人工智能、设计、统计学等相关专业优先，可实习 6 个月以上，"
    "每周至少 4 天； 2、AI 工具深度使用者——日常高频使用 Claude Code / Cursor / QoderWork / "
    "Codex 等 AI Agent 类产品，能清晰表达使用体感与改进思路； 3、有可展示的作品"
    "（实习项目、个人产品、上线 App、开源贡献均可），能证明动手做过而非仅停留在想法层面； "
    "4、对用户体验有敏锐感知，善于从自身使用场景中发现问题并提出解决方案； 5、具备较强的自驱力与"
    "学习能力，对 AI Agent、Skills、Memory 等概念有基本认知者优先。",
    "加分项：",
    "有 AI 产品 / 智能体相关实习经验；熟悉 vibe coding 或具备基础编程能力（Python/SQL）；"
    "有数据分析习惯。",
])

# 86947a8aaece8c1b VAST「AI 创作运营实习生」——正文自带「【职位描述】」小标题，
# 且**标题之前**还有一行平台提示「【英语不可作为工作语言勿投】」。
JBD_86947A8A_LIVE = "\n".join([
    "【英语不可作为工作语言勿投】",
    "【职位描述】",
    "1.关注海外社媒、兴趣文化与消费趋势，结合用户需求、商品⽅向和竞品动态，挖掘内容选题与创作玩法机会。",
    "2.使⽤ AI ⼯具进行 Prompt 调试和多轮生成测试，持续优化创作效果和⽤户体验。",
    "3.参与内容产品的策划、素材准备、上线协同和发布检查。",
    "4.参与内容产品 GTM 方案制定，梳理目标用户、使用场景、核心卖点、展示素材、教程内容和传播需求。",
    "5.跟踪内容产品的使用、生成、加购、支付和用户反馈，输出复盘和迭代建议。",
    "6.与产品、设计、电商、增长等团队协作，推动项目按计划上线。",
    "7.基于数据和反馈提出内容产品的优化、延展、拆分或淘汰建议。",
    "【职位要求】",
    "1.研究、信息整理和结构化表达能力强，能把分散资料整理成清晰结论。",
    "2.有基本审美和视觉判断力，对 AI 创作、内容产品或消费品有兴趣。",
    "3.对海外市场、社媒内容和消费趋势保持敏感，能够主动关注并理解不同文化语境下的流行内容与用户偏好。",
    "4.能熟练使用 ChatGPT、图像/视频生成工具或其他 AI 工具；愿意持续试错。",
    "5.具备基础英文阅读和资料检索能力。",
    "6.具备良好的沟通与项目协同能力，能清晰表达观点并推动跨团队合作。",
    "7.做事细致、有执行力，能够按流程记录测试过程并按时交付。",
    "8.有设计、内容、用户研究、产品运营或 AI 工具经验者优先。",
])

# 2f453051efb099c9 出海引擎科技「AI Agent 研发实习生」——正文第一段是
# 【公司介绍】，第二段才是【职位描述】。
JBD_2F453051_LIVE = "\n".join([
    "【公司介绍】",
    "2024 年成立以来，我们快速成长为 TikTok Shop 美国市场头部增长伙伴：2026 年月 GMV 突破 "
    "X千万美元（由于保密无法透露具体数字）、月增速超过 40%，荣获 TikTok Top ⽣态奖"
    "（全球仅 5 家），并⼊选《福布斯》Startups 100 榜单。基于⼀线电商运营经验，我们推出了 "
    "OperaX 与 AutoBoost，致力于打造真正理解业务、独立决策、持续学习并完成经营闭环的下⼀代电商 "
    "Agent，已助力全球品牌实现月度超 1 亿美元 GMV 增长。Human 负责定义想象力，Agent 负责释放生产力。"
    "我们寻找对 AI Agent 充满热情的核心工程师，共同探索 LLM、Memory、Tool Use、多 Agent 协作与自主决策。"
    "你将主导核心系统的架构设计与工程落地 —— 这不只是一份写代码的工作，而是一次定义下一代生产力系统、"
    "创造全新工作方式的机会。加入我们，一起让 AI 从 “会回答问题”，走向 “能完成工作、创造结果”。",
    "【职位描述】",
    "1、负责跨境电商托管Agent应用开发，设计并实现端到端的电商运营（市场分析、货架运营、达人运营、"
    "投放运营）的agent应用，提升业务运营的效率；",
    "2、优化Agent应用性能效率和产出效果，深度理解Agent的实现机制和业务流程，针对评测过程中发现的问题，"
    "结合具体案例进行根源分析，量化输出可落地的优化方向与改进建议；",
    "3、持续推进Agent Context Engineering，优化包括RAG、工具系统在内的生态体系，提高Agent的运行效率和"
    "稳定性，为高并发、多场景的业务环境提供有力的技术支撑；",
    "【职位要求】",
    "1、掌握至少一种主流编程语言，如Python、Java、Go等，熟悉Web开发框架，能够高效地进行前后端开发工作，"
    "具备良好的数据结构和算法基础；",
    "2、熟悉AI Agent相关技术，了解工具开发，Agent流程设计，评估系统，上下文工程等体系；",
    "3、具有较强的学习能力和求知欲，能够快速掌握新知识、新技术，关注AI领域的发展动态，推动Agent应用的"
    "持续优化；",
])

# --- Bug B：线上正文 + 招聘者卡片（卡片是页面容器兜底取正文时一起带进来的） ---

# 6efe2d54a695e086 杭州北橙星文化传媒「ai产品经理」：姓名 + 在线 + 公司·职位
# （线上原文每行尾部有两个空格，归一化时会去掉，夹具按清洗后的形态写）
JBD_6EFE2D54_BODY = "\n".join([
    "岗位职责：",
    "1、负责AI产品的需求调研与用户场景分析，输出产品规划和功能设计；",
    "2、协调技术与设计团队，推动AI功能的研发落地与迭代优化；",
    "3、跟踪AI行业动态与竞品发展，结合业务需求制定产品策略；",
    "4、监控产品上线后的使用数据与用户反馈，持续提升产品体验与价值；",
    "任职要求：",
    "1、具备AI产品设计或相关领域实践经验，熟悉⼈⼯智能技术应⽤场景；",
    "2、熟悉主流AI模型及工具的应用逻辑，对⾃然语言处理、计算机视觉等方向有基本理解；",
    "3、具备较强的逻辑思维与用户洞察能力，能独立完成需求分析与产品原型设计；",
    "4、良好的沟通协作能力，能够高效联动研发、运营等多部门推进项目落地；",
])
JBD_6EFE2D54_INPUT = (
    JBD_6EFE2D54_BODY + "\n\n 陈先生\n\n 在线\n\n 杭州北橙星文化传媒·总经理")

# 4aae2f83bb36044d 杭州梵域智能科技「AI产品经理实习生」：姓名 + 在线 + 纯公司名
JBD_4AAE2F83_BODY = (
    "岗位职责1.参与NPC智能体与企业知识中心两条产品线的需求分析，梳理业务场景，协助撰写PRD、"
    "原型设计等产品文档，跟进需求从设计到上线的全流程。2.熟练使用Claude Code、Codex等AI Coding工具，"
    "结合Prompt⼯程完成产品Demo、原型与内部⼯具的快速搭建和验证。3.协助进⾏AI产品市场调研与竞品分析"
    "（如Glean、Notion AI、Confluence AI等同类产品），跟踪AI工具/平台动态，为产品优化提出建议。"
    "4.参与产品配套工具与Skill的设计，沉淀可复⽤的Skills、提示词模板和最佳实践文档，"
    "提升团队AI工具使用效率。岗位要求1.27届本科及以上，计算机、人工智能、软件工程、信息管理相关专业优先。"
    "2.有Claude Code、Codex等AI Coding工具的实际使用经验者优先。3.有产品实战经验（原型设计、PRD撰写）"
    "者优先；了解AI基础知识，对AI技术有浓厚兴趣。4.文档撰写与表达能力好，自学能力强，能快速上手新工具。")
JBD_4AAE2F83_INPUT = JBD_4AAE2F83_BODY + "\n\n 孙女士\n\n 在线\n\n 杭州梵域智能科技"

# df40fc5c077e68d3 上海阿卡比科技「ai产品实习生」：姓名 + 公司·HR（**没有**活跃行）
JBD_DF40FC5C_BODY = (
    "岗位职责：1、负责对用户的需求进行分析和拆解，撰写产品需求文档（PRD）；2、协助产品经理策划 "
    "AIGC 海外 C 端产品功能（角色聊天、多模态AI功能设计、订阅会员、虚拟币消耗体系等），"
    "有效执⾏任务并推进项⽬落地；3、参与产品各环节规划及讨论，基于业务逻辑结合公司战略及⾏业现状"
    "输出建设性的建议；4、调研行业最新动向（AI 陪伴 / AI Companions / AIGC 应用赛道），"
    "分析业务数据和⽤户需求，找到业务创新方向；5、跟进用户反馈与竞品动态，协助整理用户体验问题清单"
    "并推动优化。任职要求：1、全日制本科及以上的在校生，专业不限；2、有互联网产品实习经验，"
    "或在社区社交、内容消费领域产品实习过者优先；3、对 AIGC 领域感兴趣，有想法和认知加分，"
    "自驱力和学习能力强，工作投入度高加分；4、对乙女游戏 / 恋爱向 ACGN 内容感兴趣，"
    "资深乙女游戏玩家加分；5、线下全职实习至少 3 个月以上，每周到岗5天，具备基本的逻辑思维和"
    "文档撰写能力者优先。")
JBD_DF40FC5C_INPUT = JBD_DF40FC5C_BODY + "\n\n 江女士\n\n 上海阿卡比科技·HR"

# 23d1e7d7dfa57cc0 杭州至优进化科技「ai产品实习生」：卡片**只剩一个姓名行**
JBD_23D1E7D7_BODY = (
    "关于我们我们是由清华大学资深教授团队发起的AI科技型初创公司，专注AI Agent、自进化AI、"
    "因果推理模型与企业级智能化系统落地。公司拥有两项世界排名第一的原创算法成果，"
    "已获得千万级专业投资机构支持，正处于从技术验证⾛向产品化、商业化的关键阶段。"
    "你将负责什么1.参与AI产品的需求调研、竞品分析与⽤户研究，协助完成产品规划与路线图设计。"
    "2.协助撰写产品PRD、交互⽂档，与算法、工程团队紧密协作，推动产品功能落地。"
    "3.跟踪产品数据与用户反馈，参与产品迭代优化，持续提升⽤户体验。"
    "4.参与AI Agent、大模型应用、企业知识库等方向的产品设计与场景验证。"
    "你将获得什么12.大厂资深产品经理一对一带教，导师均来自头部科技公司，"
    "手把手带你走完从需求到落地的完整产品链路。13.充足的设备与资金支持，配备高端算力资源与调研经费，"
    "想验证的想法都能放手试。14.丰富的真实项目，直接参与金融、工业、科研等高价值行业客户场景，"
    "不是打杂写文档，积累稀缺AI产品实战履历。15.表现优异者可直接获得return offer，"
    "毕业即入职核心产品岗。16.扁平化团队，无层级内耗，和清华教授、顶尖算法工程师并肩成长，"
    "打通产品到产品的全链路认知。")
JBD_23D1E7D7_INPUT = JBD_23D1E7D7_BODY + "\n\n 史女士"

# 702e928df2e72b9e 合肥长元禾科技「AI产品经理」：卡片**只剩一个姓名行**
JBD_702E928D_BODY = (
    "AI 产品实习生招募 | 和一线团队一起，把求职智能体带给千万人我们是谁？我们是⼀⽀已获天使轮融资的 "
    "AI 原⽣产品团队，核心成员来自阿⾥巴巴、字节跳动等一线互联网公司的 AI 部门。"
    "我们正在打造「面面赢」求职智能体，用前沿 AI 能力做每一个求职者的私人求职王牌搭子——"
    "目前产品已上线，正处于快速迭代、急需优秀产品力量加入的阶段。你会做什么？"
    "· 深度参与 AI 原生产品的用户洞察与需求挖掘，通过用户访谈、数据分析等方式，"
    "为产品迭代提供决策依据· 协助产品功能的方案设计、PRD 撰写与落地推进，全程跟进从想法到上线的闭环"
    "· 与算法、工程、运营团队紧密协作，在真实创业环境中理解“好产品”是如何打磨出来的"
    "你将获得· 与一线 AI 创业团队并肩作战的实战经历，你的分析和方案会被真正听到、采纳"
    "· 完整参与一个 AI 产品从 0 到 1 的早期阶段，这份经历远比大厂螺丝钉更丰满"
    "· 一份扎实的产品实习证明，表现优异者提供转正机会"
    "· 和一群懂 AI、有热情的伙伴共同成长，把你的热爱变成履历上最硬的项目")
JBD_702E928D_INPUT = JBD_702E928D_BODY + "\n\n 李先生"

# --- Bug C：平台技能标签粘在 JD 开头 ---

# ab241aa3a8fc1f8f 杭州衡民无际科技「ai产品经理」：技能标签三行粘在正文前
JBD_AB241AA3_BODY = "\n".join([
    "1、全链路系统架构：统筹从视觉识别、精密称重、自动分拣到光谱检测的软硬一体化产品设计，"
    "构建“一物一码”的数字化标准体系。",
    "2、AI技术产品化落地：将计算机视觉与⾼光谱分析技术转化为具体的中药材品质鉴定、溯源及评级功能，"
    "确保检测精度与⾃动化效率。",
    "3、双端场景解决⽅案：针对C端与B端设计差异化的服务与交互系统。",
    "4、数据资产与标准共建：主导数字护照体系的建立，推动ISO国际标准及行业团体标准的制定，"
    "实现数据资产化。",
    "5、商业模式闭环设计：定义产品权限与收益逻辑，构建产学研⽤的商业生态闭环。",
])
JBD_AB241AA3_TAGS = ["视觉类AI", "机器学习类AI", "智能医疗"]
JBD_AB241AA3_INPUT = "职位描述\n" + "\n".join(JBD_AB241AA3_TAGS) + "\n" + JBD_AB241AA3_BODY

# 0cf93290fe03340d 杭州叽里咕咕创意科技「ai产品经理」：单个技能标签行
JBD_0CF93290_BODY = "\n".join([
    "岗位职责：",
    "1、负责AI产品的规划与设计，结合用户需求和技术创新推动产品落地；",
    "2、协调研发、算法、设计等团队，推进产品开发全流程，确保项目高效执行；",
    "3、持续跟踪产品表现与⽤户体验，基于数据反馈驱动产品优化与迭代；",
    "4、关注⼈⼯智能领域发展趋势，挖掘新技术在业务场景中的应用潜力；",
    "任职要求：",
    "1、对⼈工智能技术有深入理解，熟悉常见AI模型与应用场景；",
    "2、具备较强的用户洞察力和产品设计能力，能独立完成需求分析与方案输出；",
    "3、良好的跨团队沟通与协作能力，能够有效推动多方协同工作；",
    "4、具备较强的学习能力与创新意识，能在快速变化的技术环境中持续成长；",
])
JBD_0CF93290_TAGS = ["C端产品"]
JBD_0CF93290_INPUT = "职位描述\nC端产品\n" + JBD_0CF93290_BODY

# 6bcff8742cf1d857 众联成业科技「AI产品经理实习生」：**同时**有技能标签头和招聘者尾巴
JBD_6BCFF874_BODY = (
    "岗位职责1.协助产品经理聚焦AI智能售后、AIAgent等核心产品方向，开展用户需求分析与业务场景拆解，"
    "将业务需求转化为可落地的方案，跟进产品迭代全流程。2.协助开展客户AI场景调研与信息梳理，"
    "输出包含AI交互逻辑、模型能⼒边界、功能规则的产品需求⽂档，配合完成AI功能原型设计；"
    "同步调研AI⾏业竞品动态与大模型能力，为产品规划提供参考依据。3.对接客户AI功能相关的需求咨询与"
    "使⽤问题，精准梳理汇总客户反馈的AI场景诉求、模型效果问题，沉淀典型AI落地场景与客户痛点，"
    "为产品优化提供有效输入。4.参与产品的测试、上线、优化等工作，并对产品进行不断改进和优化。"
    "任职要求1.对ToB产品、大模型应用、AIAgent方向有浓厚兴趣，有志于长期深耕AI产品领域，"
    "具备坚定的职业发展方向。2.熟练操作Word、Excel、PPT等Office办公软件，"
    "可借助主流AI工具高效完成文档处理、数据整理与内容输出。3.熟练使用Axure、MindManager等产品工具，"
    "了解主流大模型产品的基础逻辑，具备基础prompt设计与优化能力者优先。4.具备较强的沟通协作能力与"
    "快速学习能力，能快速理解AI相关技术概念，抗压能力强，可主动推进问题解决。"
    "5.英语六级及海外留学经历优先；有AI产品相关实习、项目实践经历者优先。")
JBD_6BCFF874_TAGS = ["B端产品"]
JBD_6BCFF874_INPUT = ("职位描述\nB端产品\n\n" + JBD_6BCFF874_BODY
                       + "\n\n 夏女士\n\n 在线\n\n 众联成业科技")


# ===========================================================================
# A. 头部被截断
# ===========================================================================

class JdHeadTruncationTests(unittest.TestCase):
    """正文里再次出现「职位描述」时，不能把标记之前的内容切掉。"""

    def setUp(self):
        self.module = load_scraper()

    def test_marker_in_middle_keeps_head_paragraph(self):
        """81a080c6c9a9187e：交付 400 字 / 线上 619 字，丢了开头 219 字。"""
        jd = JBD_81A080C6_LIVE
        fields = self.module.extract_detail_fields({"jd": jd, "page_text": jd})
        out = fields["jd"]
        self.assertTrue(out.startswith("研究领域："), out[:40])
        self.assertIn("随着大模型及Agent技术的持续演进", out)
        self.assertIn("探索小规模可验证的技术路线", out)
        self.assertIn("1、大模型对抗攻击", out)
        self.assertIn("6、至少6个月的全职工作。", out)
        # 一字都不能少（线上 619 字，交付不能是 400 字）
        self.assertEqual(len(out), len(jd))
        # 交付的 JD 不能以孤立的「：」/「】」开头
        self.assertNotIn(out[0], "：】」、。，,;；)")

    def test_marker_in_middle_does_not_lose_bracket_header(self):
        """86947a8aaece8c1b：【英语不可作为工作语言勿投】那一行不能丢。"""
        jd = JBD_86947A8A_LIVE
        fields = self.module.extract_detail_fields({"jd": jd, "page_text": jd})
        out = fields["jd"]
        self.assertTrue(out.startswith("【英语不可作为工作语言勿投】"), out[:40])
        self.assertIn("【职位描述】", out)
        self.assertIn("8.有设计、内容、用户研究、产品运营或 AI 工具经验者优先。", out)
        self.assertEqual(len(out), len(jd))

    def test_company_intro_before_marker_is_kept(self):
        """2f453051efb099c9：【公司介绍】整段不能丢。"""
        jd = JBD_2F453051_LIVE
        fields = self.module.extract_detail_fields({"jd": jd, "page_text": jd})
        out = fields["jd"]
        self.assertTrue(out.startswith("【公司介绍】"), out[:40])
        self.assertIn("2024 年成立以来", out)
        self.assertIn("3、持续推进Agent Context Engineering", out)
        self.assertEqual(len(out), len(jd))

    def test_marker_at_head_is_dropped_without_leaving_separator(self):
        """ecad3d515b84fbfc：正文第一行就是「职位描述：」→ 去掉该行，
        但不能留下孤立的「：」，正文其余部分一字不少。"""
        jd = JBD_ECAD3D51_LIVE
        fields = self.module.extract_detail_fields({"jd": jd, "page_text": jd})
        out = fields["jd"]
        self.assertTrue(out.startswith("1、深度参与千问办公"), out[:40])
        self.assertNotIn(out[0], "：】」、。，,;；)")
        self.assertNotIn("职位描述", out)
        self.assertIn("5、协助撰写产品文档、PRD 及功能说明", out)
        self.assertIn("有数据分析习惯。", out)
        self.assertEqual(len(out), len(jd) - len("职位描述：\n"))

    def test_page_header_marker_still_cuts_chrome(self):
        """页面兜底：标记前是页面框架（标题/导航）→ 仍然要在标记处切。"""
        body = "负责 AI 产品规划、需求分析和跨团队项目推进。\n" * 8
        page_text = ("AI产品经理\n200-300元/天\n杭州·余杭区\n职位描述\n" + body)
        fields = self.module.extract_detail_fields({"jd": "", "page_text": page_text})
        out = fields["jd"]
        self.assertTrue(out.startswith("负责 AI 产品规划"), out[:40])
        self.assertNotIn("AI产品经理", out)
        self.assertNotIn("杭州·余杭区", out)

    def test_leading_noise_line_does_not_hide_the_header(self):
        """标记前隔着 UI 噪声行（微信扫码分享 举报）时，仍要在标记处切。"""
        body = "负责 AI 产品规划、需求分析和跨团队项目推进。\n" * 8
        page_text = ("微信扫码分享 举报\n职位描述\n" + body)
        fields = self.module.extract_detail_fields({"jd": page_text, "page_text": page_text})
        out = fields["jd"]
        self.assertTrue(out.startswith("负责 AI 产品规划"), out[:40])
        self.assertNotIn("微信扫码分享", out)
        self.assertNotIn("职位描述", out)


# ===========================================================================
# B. 尾部混进招聘者卡片
# ===========================================================================

class JdRecruiterTailTests(unittest.TestCase):
    """招聘者卡片（姓名/活跃度/公司·职位）不能留在 JD 里。"""

    def setUp(self):
        self.module = load_scraper()

    def _jd(self, text):
        return self.module.extract_detail_fields({"jd": text, "page_text": text})["jd"]

    def test_name_activity_company_position_card_is_cut(self):
        """6efe2d54a695e086：尾部多出「陈先生 / 在线 / 杭州北橙星文化传媒·总经理」。"""
        out = self._jd(JBD_6EFE2D54_INPUT)
        self.assertEqual(out, JBD_6EFE2D54_BODY)
        self.assertNotIn("陈先生", out)
        self.assertNotIn("总经理", out)

    def test_name_activity_company_card_is_cut(self):
        """4aae2f83bb36044d：尾部多出「孙女士 / 在线 / 杭州梵域智能科技」。"""
        out = self._jd(JBD_4AAE2F83_INPUT)
        self.assertEqual(out, JBD_4AAE2F83_BODY)
        self.assertNotIn("孙女士", out)
        self.assertNotIn("杭州梵域智能科技", out)

    def test_name_company_hr_card_is_cut(self):
        """df40fc5c077e68d3：尾部多出「江女士 / 上海阿卡比科技·HR」。"""
        out = self._jd(JBD_DF40FC5C_INPUT)
        self.assertEqual(out, JBD_DF40FC5C_BODY)
        self.assertNotIn("江女士", out)
        self.assertNotIn("·HR", out)

    def test_name_only_card_is_cut(self):
        """23d1e7d7dfa57cc0：尾部只剩「史女士」一行。"""
        out = self._jd(JBD_23D1E7D7_INPUT)
        self.assertEqual(out, JBD_23D1E7D7_BODY)
        self.assertNotIn("史女士", out)

    def test_name_only_card_is_cut_long_body(self):
        """702e928df2e72b9e：尾部只剩「李先生」一行。"""
        out = self._jd(JBD_702E928D_INPUT)
        self.assertEqual(out, JBD_702E928D_BODY)
        self.assertNotIn("李先生", out)

    def test_active_status_is_read_from_the_card(self):
        """卡片里的活跃度要留下（boss_active_status），但不能留在 JD 里。"""
        fields = self.module.extract_detail_fields(
            {"jd": JBD_6EFE2D54_INPUT, "page_text": JBD_6EFE2D54_INPUT})
        self.assertEqual(fields["boss_active_status"], "在线")
        self.assertNotIn("在线", fields["jd"])

    def test_body_talking_about_online_is_not_cut(self):
        """正文里合法的「XX 在线」表达不能被误伤（不能只做关键词黑名单）。"""
        body = ("面试方式：微信沟通，工作时间 9:00-18:00，HR 全程在线跟进。\n"
                "我们希望你能主动同步进度，保持在线沟通效率。\n" * 6)
        out = self._jd(body)
        self.assertIn("HR 全程在线跟进", out)
        self.assertIn("保持在线沟通效率", out)

    def test_body_ending_with_a_name_line_is_kept(self):
        """正文最后一行是「李先生」但**不是**卡片（前面没有活跃行/公司行）→ 保留。"""
        body = ("岗位职责：负责 AI 产品规划、需求分析和跨团队项目推进，"
                "有产品从 0 到 1 的落地经验者优先。\n" * 6 + "李先生")
        out = self._jd(body)
        self.assertIn("李先生", out)

    def test_body_with_hr_keyword_is_kept(self):
        """正文里出现「HR」字样不能被当成招聘者卡片删掉。"""
        body = ("岗位要求：与 HR 团队协作完成需求梳理；"
                "有跨部门沟通经验者优先，面试由 HR 组织。\n" * 8)
        out = self._jd(body)
        self.assertIn("HR 团队协作", out)
        self.assertIn("面试由 HR 组织", out)


# ===========================================================================
# C. 平台技能标签粘在 JD 开头
# ===========================================================================

class JdSkillTagHeadTests(unittest.TestCase):
    """页面容器兜底取正文时，平台技能标签会粘在 JD 最前面。"""

    def setUp(self):
        self.module = load_scraper()

    def _jd(self, text, tags):
        return self.module.extract_detail_fields(
            {"jd": text, "page_text": text, "tags": tags})["jd"]

    def test_skill_tag_lines_are_stripped(self):
        """ab241aa3a8fc1f8f：JD 开头粘着三行技能标签。"""
        out = self._jd(JBD_AB241AA3_INPUT, JBD_AB241AA3_TAGS)
        self.assertTrue(out.startswith("1、全链路系统架构"), out[:40])
        self.assertNotIn("视觉类AI", out)
        self.assertNotIn("机器学习类AI", out)
        self.assertNotIn("智能医疗", out)
        self.assertIn("5、商业模式闭环设计", out)

    def test_single_skill_tag_line_is_stripped(self):
        """0cf93290fe03340d：JD 开头粘着「C端产品」。"""
        out = self._jd(JBD_0CF93290_INPUT, JBD_0CF93290_TAGS)
        self.assertTrue(out.startswith("岗位职责："), out[:40])
        self.assertNotIn("C端产品", out)
        self.assertIn("4、具备较强的学习能力与创新意识", out)

    def test_skill_tag_and_recruiter_tail_together(self):
        """6bcff8742cf1d857：技能标签头 + 招聘者尾巴同时出现。"""
        out = self._jd(JBD_6BCFF874_INPUT, JBD_6BCFF874_TAGS)
        self.assertTrue(out.startswith("岗位职责1.协助产品经理"), out[:40])
        self.assertNotIn("B端产品", out)
        self.assertNotIn("夏女士", out)
        self.assertNotIn("众联成业科技", out)
        self.assertIn("5.英语六级及海外留学经历优先", out)

    def test_body_line_equal_to_a_tag_is_kept_when_not_leading(self):
        """正文中间出现与标签同名的行不能被删（只清开头那一段）。"""
        body = ("岗位职责：负责 AI 产品规划。\nC端产品\n任职要求：三年以上经验。\n" * 6)
        out = self._jd(body, ["C端产品"])
        self.assertIn("C端产品", out)


# ===========================================================================
# 垃圾尾口径：只有一处（语义层），实现侧不许抄第二份
# ===========================================================================

class JunkTailSingleSourceTests(unittest.TestCase):
    """垃圾尾（页面模块文案）的判定口径只有 ``scripts/semantics.py`` 一处。

    验收器 ``scripts/acceptance.py`` 已改为委托 ``check_invariants``；实现侧
    （``strip_jd_noise``）如果再抄一份标记表，两边就会漂移——2026-10-22 审计
    实测就是这样漂的（验收器多认 6 个标记，实现侧一个都不认）。

    这里做**行为级**守卫：语义层会红的所有标记，实现侧都得清掉。
    """

    def setUp(self):
        self.module = load_scraper()
        self.semantics = load_module("semantics", SEMANTICS_PATH)

    def _semantic_flags(self, text):
        invariants = self.semantics.check_invariants([{"jd": text}])
        info = invariants.get("no_junk_tail") or {}
        return not info.get("ok", True)

    def _semantic_markers(self):
        """从语义层里取出它会红的标记集合。

        优先用模块级常量；没有就从 ``check_invariants`` 的源码里取那份正则
        （语义层当前把正则写在函数体内）。两条路都取不到 → 报错，让维护者
        同步更新这条守卫，而不是让守卫悄悄失效。
        """
        regex = getattr(self.semantics, "JUNK_TAIL_RE", None)
        if regex is None:
            source = inspect.getsource(self.semantics.check_invariants)
            match = re.search(r'junk\s*=\s*re\.compile\(\s*r?"([^"]+)"', source)
            if not match:
                self.fail(
                    "语义层 check_invariants 的垃圾尾正则既不是模块常量，"
                    "也没在函数体内找到——请同步更新本守卫")
            regex = re.compile(match.group(1))
        return {part for part in regex.pattern.split("|") if part}

    def test_implementation_covers_every_marker_the_semantic_layer_flags(self):
        markers = self._semantic_markers()
        self.assertTrue(markers, "语义层没给出任何垃圾尾标记")
        body = "负责 AI 产品规划、需求分析和跨团队项目推进。\n" * 8
        for marker in sorted(markers):
            with self.subTest(marker=marker):
                self.assertTrue(self._semantic_flags(marker),
                                f"语义层不再认这个标记：{marker}")
                text = body + marker + "\n"
                out = self.module.extract_detail_fields(
                    {"jd": text, "page_text": text})["jd"]
                self.assertNotIn(marker, out,
                                 f"实现侧没清掉语义层会红的垃圾尾标记：{marker}")
                self.assertIn("负责 AI 产品规划", out)

    def test_implementation_does_not_keep_a_second_marker_table(self):
        """实现侧不许再抄一份垃圾尾标记表（抄了就会漂）。"""
        source = inspect.getsource(self.module)
        for literal in ("薪资详情", "社保类型", "提成方式", "刚刚活跃", "今日活跃"):
            self.assertNotIn(
                f'"{literal}"', source,
                f"实现侧又抄了一份垃圾尾标记（{literal}）——"
                "判定口径应委托 scripts/semantics.py 的 check_invariants")

    def test_page_module_line_inside_a_sentence_is_kept(self):
        """正文句子顺口提到「提成方式」不能被整行删掉（子串命中 ≠ 页面模块）。"""
        body = "我们的提成方式是月结，另享有绩效奖金。" + "\n"
        text = body * 8
        out = self.module.extract_detail_fields(
            {"jd": text, "page_text": text})["jd"]
        self.assertIn("我们的提成方式是月结", out)


if __name__ == "__main__":
    unittest.main()
