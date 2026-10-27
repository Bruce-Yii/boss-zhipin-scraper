# AGENTS.md

指引给未来的 ZCode agent。先读这份，再动代码。

## 换窗口接管（无需复杂交接）

状态分三类，**真相全在磁盘、可一条命令问清**：

1. **技能**（怎么跑）：本文件 + `SKILL.md` + `README.md`；工作区文档 `docs/projects/boss-zhipin-scraper/`（架构与链路地图 / 待办总表 / 任务记录）
2. **运行态**（此刻能不能跑）：`python scripts/boss_cdp_raw.py --status` → CDP 端口/Chrome profile/互斥锁/熔断冷却；登录态用 `--check` 探测
3. **缓存态**（攒下的数据）：`~/.boss-zhipin-scraper/job-result/`（列表/详情/pending/archive），文件自带 meta（`format_version`/`mode`/`observed_jobs`/`exhausted`/`jd_coverage`）

接管流程：`--status` 看全局 → `--check` 确认可跑 → `--artifacts --json` 看产物清单（最新列表/详情、每个口径的最新交付物、取代关系）→ 再用 `--verify` 复核数据完整性。

## 执行与汇报约定

- **完整计时**：汇报任务耗时的起点取「用户下达命令 / 任务开始」的时刻，终点取任务完成（含产出校验），给出完整墙钟时长；轮询类长任务另附内部阶段耗时。

## 这是什么
`boss-zhipin-scraper` —— 通过 Chrome CDP（远程调试端口）连接**用户本人已登录的 Chrome**，抓取 BOSS直聘的公开职位数据（列表 + 详情），并可生成求职分析摘要。仅用于个人求职分析，非大规模爬虫（见 `CONTRIBUTING.md` 的合规一节）。

## 目录结构

```
scripts/boss_cdp_raw.py   # 核心：抓取 + CLI 编排（CDP/链路/CLI）
scripts/ratelimit.py      # 稳定纯逻辑：BurstThrottle + 令牌桶 + 自适应限速
scripts/export_contract.py# 稳定纯逻辑：契约/递归脱敏/口径一/原子写
scripts/db_store.py       # 稳定纯逻辑：SQLite schema、只读统计、run lineage
scripts/artifacts.py      # 产物层统一：目录清单、口径、取代关系、时效（--artifacts）
scripts/build_deliverable.py # 交付物重建（文件 / SQLite 原材料库 --from-db）
scripts/audit.py          # 稳定纯逻辑：风险事件审计、递归脱敏、轮转锁
scripts/job_summary.py    # 抓取结果 → Markdown 求职分析摘要
data/city_codes.json      # 全量城市码表（300+ 城市，外置；见下）
tests/test_chrome_setup.py    # unittest，全 mock，不依赖真实 Chrome/网络
tests/test_job_summary.py     # 摘要测试
tests/test_isolation_guard.py  # 受保护运行态隔离守卫
pyproject.toml            # hatchling 打包；入口 boss-scraper / boss-summary / boss-deliverable / boss-artifacts
requirements.txt          # requests + websocket-client + pandas + matplotlib
SKILL.md / README(.en).md / CHANGELOG.md / CONTRIBUTING.md
```

**重要边界：核心编排/CDP/CLI 都在 `scripts/boss_cdp_raw.py`；稳定纯逻辑在 `scripts/ratelimit.py`、`scripts/export_contract.py`、`scripts/db_store.py`、`scripts/audit.py`（主文件顶部 re-export，见 `CONTRIBUTING.md`「模块边界」）。**`docs/` 被 `.gitignore` 忽略，是本地产物，不要提交。**例外**：`data/city_codes.json` 是城市码表数据（非逻辑代码），外置便于用户查看支持哪些城市；改它要同步跑 `tests.test_chrome_setup` 的城市码表防回归测试。

## 环境与命令

- Python **>=3.12**，依赖见 `requirements.txt`（requests + websocket-client + pandas + matplotlib；依赖按需引入，不再设"零依赖"约束）。用项目里的 `.venv`（`source .venv/bin/activate`），别用 pyenv 全局解释器（会缺依赖报错）。
- 包管理用 `uv`（仓库有 `uv.lock`），也可 `pip install -r requirements.txt`。
- 跑测试：`python3 -m unittest tests.test_chrome_setup`（无需 Chrome / 联网，全 mock）。改了 `job_summary` 再加跑 `tests.test_job_summary`。
- **隔离验证**：`python3 -m unittest tests.test_isolation_guard -v` 会在临时 HOME 下跑完整 discovery，并检查受保护的 profile、锁、cooldown、audit、runner trace 未被测试改写；提交前再跑一次。禁止从旧工作树或真实 Chrome 环境跑测试。
- 语法自检：`python3 -m py_compile scripts/boss_cdp_raw.py scripts/ratelimit.py scripts/export_contract.py scripts/db_store.py scripts/audit.py scripts/job_summary.py`。
- 实跑抓取需要先启动带调试端口的 Chrome：`python3 scripts/boss_cdp_raw.py --setup-chrome`（开 `127.0.0.1:45222`，默认端口见 `DEFAULT_CDP_PORT`），登录后在**另一个终端**跑抓取命令。Chrome 关了端口就没了。

## 改代码时的硬规则

1. **版本号同步**：`scripts/boss_cdp_raw.py` 的 `__version__`（第 22 行附近）、`pyproject.toml`、`SKILL.md`、`README.md`、`README.en.md`（以及 `uv.lock` 的本地包元数据）必须同步，否则 `VersionConsistencyTests` 或发布检查会挂。改版本号时一起改。
2. **异常处理**：禁止 bare `except:`，必须捕获具体类型（`requests.ConnectionError`、`json.JSONDecodeError` 等），和现有代码保持一致。
3. **改了用户可见行为 → 更新 `README.md`；有意义变更 → `CHANGELOG.md` 顶部加一条。**
4. **README 双语同步**：`README.md`（中文）和 `README.en.md`（英文）必须保持一致，改了其中一个就要同步另一个。
5. **commit message 用 Conventional Commits**（`feat:` / `fix:` / `docs:` / `optimize:` / `refactor:` 等，见 `CONTRIBUTING.md`）。

## 架构关键点（容易踩坑）

- `scripts/boss_cdp_raw.py` 是一个**长单文件**，包含：`CDPSession` 类（WebSocket 连 CDP）、各种 `EXTRACT_*_JS` 注入脚本、`scrape_jobs`（列表走 `/wapi/...` API）、`scrape_details`（详情走新开 tab 渲染）、`main`（argparse）。城市码表外置到 `data/city_codes.json`，`resolve_city` 查询链为「本地静态码表 → 运行时拉 BOSS 接口 → 原样兜底」。
- **列表页 vs 详情页路径完全不同**：列表页通过页面内 `fetch` 调 BOSS wapi（带 token，不经页面渲染）；详情页通过 `Target.createTarget` 新开 tab → `Page.navigate` → 注入 JS 提取。改其中一条路径时，另一条不受影响。
- **详情抓取主路径 = DOM 通道**（2026-09-23 起：`--detail-channel dom` 默认，整页渲染详情页，document 导航，**唯一有生产证据的形态**；`api`/`encrypt` 详情 API 通道**已封存**——平台对该接口限流并升级为短时账号封禁（tangzhiyao/boss-show-time#42），`html` 直取也已封存——会间歇性返回别的岗位的 JD 并带 `display:none` 诱饵文本）：`securityId` 仍由列表阶段内存收集（`scrape_list` 返回 `security_map`，**不落导出文件**——红线）；`--input` 只读未过期 sidecar。**节律**：DOM 通道用 `DETAIL_DOM_GAP_SECONDS`（4-9s 岗间隔）+ `--detail-rate`（默认 1 次/秒绝对上限，与并发解耦），日预算 700 才是总量约束；若经 `auto` 真走了 API 请求，由 `BurstThrottle(center=2.25)` 全局串行节流（~0.44 req/s），同 tab 成功预算 `DETAIL_API_TAB_BUDGET=4` 第 5 次前换 tab（仅 API 通道适用）。`page_update_date` 只有 DOM 路径能拿到。`--batch` 默认仍抓完整（列表+详情，任务级 `detail:false` 可仅列表）。
- **显式多通道 + securityId 受限 sidecar**（2026-09-22 P1）：详情按配置走 panel/DOM（`api`/`encrypt` 现已封存，见上条）；导出 meta 记录真实 per-job 成功通道 `detail_channel`（`api`/`encrypt`/`dom`/`panel`/`mixed`）及 `detail_channel_counts`，失败尝试不计入统计。live list run 只在真正进入详情窗口时创建 sidecar，并在 `finally` 删除；`--input` 只读既有 sidecar、不取得所有权；sidecar 位于仓库外 `~/.boss-zhipin-scraper/.session/`、权限 `600`、TTL 60 分钟、启动清理。`securityId` 绝不进导出/日志/git，登录 cookie 绝不落盘。
- **run 级锁与冷却**：普通 CLI/smoke 从冷却检查、登录探测、列表到详情/合并/分析/DB 收尾全程持一把 run 锁；batch 自己持一把锁并向任务传 `lock_already_held=True`。冷却在任何 live request 前检查，锁拒绝稳定 exit 1；`--status`/`--verify` 等只读命令不拿 run 锁。
- **CDP target 焦点/可见性不变量**：统一通过 `create_page_session` 创建页面；自动化 target 默认后台打开并在导航前注册 visibility override，避免抢焦点且避免 `document.hidden=true` 触发 BOSS visibility 反爬（issue #18）。只有需要用户操作的 `wait_for_login` 显式传 `background=False`。不要绕过 helper 直接新增 `Target.createTarget`。
- 同一个 Chrome 实例的默认 browser context 下，新开 target **本就共享 cookies**，不要被「新 tab 丢 cookie」的直觉误导。
- `require_runtime_dependencies("requests", "websocket")` 在多个入口前置检查依赖，缺了会提示安装。

## 提交流程

默认分支 `master`，fork/分支工作流：从 `master` 拉新分支（`fix/...`、`feat/...`）→ 改代码补测试 → push → PR。一个 PR 只做一件事。

**先开 issue 再动手**：非平凡的改动（bug 修复、新功能、文档补充）按仓库 `CONTRIBUTING.md` 的规范，先在 Issues 开一条说明「改什么 / 为什么 / 怎么改」，讨论清楚后再起新分支提交。issue 正文要结构化（问题 / 现状 / 根因 / 建议 / 影响），并标注改动范围（哪些逻辑受影响、哪些不动）。

**issue 分诊打标**：处理/关闭 issue 时按 `CONTRIBUTING.md` 的「Issue 标签约定」打标签；`wontfix` / `invalid` 关闭前必须留说明理由的回复。

## 合规与审计（P4e）

- **红线**：不绕过平台安全机制、不调高默认频率/并发、不采集个人身份信息、不落 cookie/token、**不用代理池/换 IP**（代理实测致 `code 7/37`；IP 被封＝该账号全部会话作废、换 IP 无效只能重新登录）。详见 `CONTRIBUTING.md`「关于合规 / 拒收的 PR（明文红线）」。
- **审计**：`send_alert()` 与 `mark_cdp_cooldown()` 会把风险事件写入 `~/.boss-zhipin-scraper/risk_events.jsonl`（append-only JSONL，仓库外、不含凭据；`BOSS_AUDIT_PATH` 可覆盖，供测试）。新增"风控/登录/熔断"分支时请同步落审计（`record_audit_event`）。
