# AGENTS.md

指引给未来的 ZCode agent。先读这份，再动代码。

## 换窗口接管（无需复杂交接）

状态分三类，**真相全在磁盘、可一条命令问清**：

1. **技能**（怎么跑）：本文件 + `SKILL.md` + `README.md`；工作区文档 `docs/projects/boss-zhipin-scraper/`（架构与链路地图 / 待办总表 / 任务记录）
2. **运行态**（此刻能不能跑）：`python scripts/boss_cdp_raw.py --status` → CDP 端口/Chrome profile/互斥锁/熔断冷却；登录态用 `--check` 探测
3. **缓存态**（攒下的数据）：`~/.boss-zhipin-scraper/job-result/`（列表/详情/pending/archive），文件自带 meta（`format_version`/`mode`/`observed_jobs`/`exhausted`/`jd_coverage`）

接管三步：`--status` 看全局 → `--check` 确认可跑 → `--list-results`/`--verify` 看数据是否完整。

## 执行与汇报约定

- **完整计时**：汇报任务耗时的起点取「用户下达命令 / 任务开始」的时刻，终点取任务完成（含产出校验），给出完整墙钟时长；轮询类长任务另附内部阶段耗时。

## 这是什么
`boss-zhipin-scraper` —— 通过 Chrome CDP（远程调试端口）连接**用户本人已登录的 Chrome**，抓取 BOSS直聘的公开职位数据（列表 + 详情），并可生成求职分析摘要。仅用于个人求职分析，非大规模爬虫（见 `CONTRIBUTING.md` 的合规一节）。

## 目录结构

```
scripts/boss_cdp_raw.py   # 核心：抓取 + CLI 主入口（~1900 行，单文件）
scripts/job_summary.py    # 抓取结果 → Markdown 求职分析摘要
data/city_codes.json      # 全量城市码表（300+ 城市，外置；见下）
tests/test_chrome_setup.py    # unittest，全 mock，不依赖真实 Chrome/网络
tests/test_job_summary.py     # 摘要测试
pyproject.toml            # hatchling 打包；入口 boss-scraper / boss-summary
requirements.txt          # 仅 requests + websocket-client
SKILL.md / README(.en).md / CHANGELOG.md / CONTRIBUTING.md
```

**重要边界：核心逻辑都放 `scripts/boss_cdp_raw.py`，不要随手新建文件**（见 `CONTRIBUTING.md`「单文件原则」）。`docs/` 被 `.gitignore` 忽略，是本地产物，不要提交。**例外**：`data/city_codes.json` 是城市码表数据（非逻辑代码），外置便于用户查看支持哪些城市；改它要同步跑 `tests.test_chrome_setup` 的城市码表防回归测试。

## 环境与命令

- Python **>=3.10**，依赖见 `requirements.txt`（requests + websocket-client + pandas + matplotlib；依赖按需引入，不再设"零依赖"约束）。用项目里的 `.venv`（`source .venv/bin/activate`），别用 pyenv 全局解释器（会缺依赖报错）。
- 包管理用 `uv`（仓库有 `uv.lock`），也可 `pip install -r requirements.txt`。
- 跑测试：`python3 -m unittest tests.test_chrome_setup`（无需 Chrome / 联网，全 mock）。改了 `job_summary` 再加跑 `tests.test_job_summary`。
- 语法自检：`python3 -m py_compile scripts/boss_cdp_raw.py`。
- 实跑抓取需要先启动带调试端口的 Chrome：`python3 scripts/boss_cdp_raw.py --setup-chrome`（开 `127.0.0.1:45222`，默认端口见 `DEFAULT_CDP_PORT`），登录后在**另一个终端**跑抓取命令。Chrome 关了端口就没了。

## 改代码时的硬规则

1. **版本号四处一致**：`scripts/boss_cdp_raw.py` 的 `__version__`（第 22 行附近）、`pyproject.toml`、`SKILL.md`、`README.md` 必须同步，否则 `VersionConsistencyTests` 会挂。改版本号时四处一起改。
2. **异常处理**：禁止 bare `except:`，必须捕获具体类型（`requests.ConnectionError`、`json.JSONDecodeError` 等），和现有代码保持一致。
3. **改了用户可见行为 → 更新 `README.md`；有意义变更 → `CHANGELOG.md` 顶部加一条。**
4. **README 双语同步**：`README.md`（中文）和 `README.en.md`（英文）必须保持一致，改了其中一个就要同步另一个。
5. **commit message 用 Conventional Commits**（`feat:` / `fix:` / `docs:` / `optimize:` / `refactor:` 等，见 `CONTRIBUTING.md`）。

## 架构关键点（容易踩坑）

- `scripts/boss_cdp_raw.py` 是一个**长单文件**，包含：`CDPSession` 类（WebSocket 连 CDP）、各种 `EXTRACT_*_JS` 注入脚本、`scrape_jobs`（列表走 `/wapi/...` API）、`scrape_details`（详情走新开 tab 渲染）、`main`（argparse）。城市码表外置到 `data/city_codes.json`，`resolve_city` 查询链为「本地静态码表 → 运行时拉 BOSS 接口 → 原样兜底」。
- **列表页 vs 详情页路径完全不同**：列表页通过页面内 `fetch` 调 BOSS wapi（带 token，不经页面渲染）；详情页通过 `Target.createTarget` 新开 tab → `Page.navigate` → 注入 JS 提取。改其中一条路径时，另一条不受影响。
- **详情抓取主路径 = API 通道**（2026-08-14 起）：`/wapi/zpgeek/job/detail.json?jobId=&securityId=&city=` 每岗 1 次轻量 XHR（JD 秒回）；`securityId` 由列表阶段内存收集（`scrape_list` 返回 `security_map`，**不落导出文件**——红线）；`--input` 老文件无 securityId 自动回退 DOM 渲染（`_scrape_one_detail_via_api` vs `_scrape_one_detail` 分支）。**每 tab 配额 ≈4-5 次**：同一 tab 连续详情后返回 code 37，**换新 tab 立即重置**（2026-09-22 实证）→ 按 `DETAIL_API_TAB_BUDGET=4` **主动轮换 tab**，风控码先换 tab 重试一次再判定真风控；**并发模式用共享 tab 池**（`_scrape_details_parallel` 预建 `concurrency` 个 tab，queue 分发，每 worker 一个）；限速基线 `DETAIL_API_PACE_SECONDS=15s/worker`（API 无渲染等待，限速器是唯一刹车，勿改成"每秒级"基线——会触发风控）。**`page_update_date` 只能用 DOM 路径**：2026-09-22 已实测详情 API 全字段（`dateLikeValues` 空；仅 `bossInfo.activeTimeDesc`=HR 活跃度、`brandComInfo.activeTime`=公司级），无岗位更新时间 → API 通道下该字段恒为空，属已接受的退化点，不要重复探测。`--batch` 同样遵循"默认抓完整"（列表+详情，任务级 `detail:false` 可仅列表）。
- **CDP target 焦点/可见性不变量**：统一通过 `create_page_session` 创建页面；自动化 target 默认后台打开并在导航前注册 visibility override，避免抢焦点且避免 `document.hidden=true` 触发 BOSS visibility 反爬（issue #18）。只有需要用户操作的 `wait_for_login` 显式传 `background=False`。不要绕过 helper 直接新增 `Target.createTarget`。
- 同一个 Chrome 实例的默认 browser context 下，新开 target **本就共享 cookies**，不要被「新 tab 丢 cookie」的直觉误导。
- `require_runtime_dependencies("requests", "websocket")` 在多个入口前置检查依赖，缺了会提示安装。

## 提交流程

默认分支 `master`，fork/分支工作流：从 `master` 拉新分支（`fix/...`、`feat/...`）→ 改代码补测试 → push → PR。一个 PR 只做一件事。

**先开 issue 再动手**：非平凡的改动（bug 修复、新功能、文档补充）按仓库 `CONTRIBUTING.md` 的规范，先在 Issues 开一条说明「改什么 / 为什么 / 怎么改」，讨论清楚后再起新分支提交。issue 正文要结构化（问题 / 现状 / 根因 / 建议 / 影响），并标注改动范围（哪些逻辑受影响、哪些不动）。
