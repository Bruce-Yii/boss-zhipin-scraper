# Changelog

## v2.15.2 (2026-09-23)

### 修复（关键：详情卡在第 5 条的根因）
- **`DETAIL_API_TAB_BUDGET` 5 → 4（off-by-one）**：轮换条件是 `hits >= BUDGET`，预算 5 时**第 5 次请求仍复用旧 tab**。2026-09-23 实机复测：当前配额为 **4**（第 5 次即 `code 37 您的环境存在异常`，且日志无轮换）→ 必挂。改预算为 **4**（第 5 次前主动轮换）。**这解释了此前"0.13/0.33/0.44 一律在第 5 条触 37"——与速率无关，是 tab 配额越界**（`AGENTS.md` 架构注记本就写 4，代码与文档漂移）。
- 测试：轮换用例改为按预算动态推算 tab 数；新增"预算 ≤ 4"守门。

## v2.15.1 (2026-09-23)

### 修复（实机复核结论）
- **移除已失效字段 `publish_time`**：实机复核确认新 SPA **已无 `div.info-publis` 节点**（专题 §1.5 结论证实），P4d 加的 `publish_time`（相对发布时间）**恒为空** → 从 `EXTRACT_DETAIL_JS` / `extract_detail_fields` / `build_detail_record` / 导出中移除；README（中英）同步删除该字段。**`page_update_date`（页面更新时间）实机仍在，保留**。
- 测试：删除 publish_time 断言，新增"`info-publis` 不得回归"守卫。

## v2.15.0 (2026-09-23)

### 新增
- **右面板 JD 通道（`--detail-channel panel`）**（《DOM 与速度控制专题》§1.4 / 上游 #84）：不跳详情页，**复用停靠的真实搜索页**——点岗位卡片 → 读右侧面板 JD，**零新增请求**（同页内点击）；逐条串行（忽略 `--concurrency`）；`--detail-channel auto|api|dom|panel` 可选。新增 `_scrape_one_detail_via_panel` / `_scrape_details_via_panel` / `CLICK_CARD_JS` / `PANEL_READY_TIMEOUT`。
- `--detail-channel dom` 可强制 DOM 通道（清空 security_map）。
- 测试 +5（`PanelJdTests` + CLI `--detail-channel`）。

## v2.14.2 (2026-09-23)

### 健壮性（code9 限流退避 · 《DOM 与速度控制专题》§2.2-6 / 清单#10）
- **code 9（限流）指数退避**：详情 API 通道遇 `code 9 rate_limited` 不再直接停手，改为**指数退避重试**（`min(60, 10×2^n)` = 10→20→40→60s，最多 3 次；对标同行 boss-cli）；退避耗尽才交上层按 category 处置。新增 `rate_limit_backoff()` 与常量 `RATE_LIMIT_BACKOFF_*`/`RATE_LIMIT_MAX_RETRIES`。
- 测试 +3（`Code9BackoffTests`：退避序列 / 限流后重试成功 / 耗尽返回）。

## v2.14.1 (2026-09-22)

### 健壮性 / 排障（《DOM 与速度控制专题》小项）
- **失败截图**：新增 `--debug-screenshots`（默认关闭）。DOM 详情在 `risk_timeout` / `login_required` / `invalid_detail` 时用 CDP `Page.captureScreenshot` 存图到 `~/.boss-zhipin-scraper/debug/`（`BOSS_DEBUG_DIR` 可覆盖；仓库外、best-effort）（专题 §3.6 / 清单#4）。
- **CDP 三级端口探测**：新增 `probe_cdp_port` / `detect_cdp_port`（`9222/9229/19222`）；`--check` 在首选端口不可达时探测候选端口并提示 `--cdp-port N`（专题 §3.2 / 清单#3）。
- **后台 tab 滚动派发事件**：被动捕获翻页改用 `SCROLL_BOTTOM_JS`（滚到底后 `dispatchEvent(new Event('scroll'))`）——后台/hidden tab 的原生滚动会被 defer，不派发事件则不触发无限滚动加载（专题 §1.6/§1.7）。
- 测试 +6（`TopicSmallItemsTests` + CLI `--debug-screenshots`）。

## v2.14.0 (2026-09-22)

### 性能（DOM 详情降级通道提速 · 《DOM 与速度控制专题》落地）
- **就绪等待替代固定 sleep**：DOM 详情原路径为"导航后固定 `sleep 5-10s` + 3-7 次滚动（各 0.8-5s）+ 页间 10-25s" → 实测 **~38s/条**（同行独立详情页锚点 ~9.5s/条，慢 ~4×）。改为**轮询 JD 区就绪即返回**（新 `DETAIL_READY_JS`，上限 12s）+ **轻量滚动（1-2 次 / 0.3-0.9s）** + **页间 4-9s**（新 `DETAIL_DOM_GAP_SECONDS`）→ 预期 **~8-14s/条**。
- **选择器兜底链**：JD 区 `…/.job-detail-body/.job-sec-text`；技能 `.job-keyword-list span/li`（专题 §1.1/§1.8 新版共识）。
- **弹窗遮罩清理**：`div.dialog-wrap/.boss-layer/.boss-popup`（新 `REMOVE_DIALOG_JS`，Snseam 手法），防遮罩挡 JD。
- **HR 活跃 ACT_RE 全系文案兜底**（专题 §1.5 / 清单#8）：footer 与 `.boss-active-time` 都缺时，按 `刚刚/今日/N日内/本周/N周内/N月内/半年前活跃` 兜底。
- 测试 +6（`DomSpeedupTests`），356 全绿。

> 说明：DOM 是**降级通道**（详情 API 命中风控时用）；提速数字为按移除的固定等待推算，实测待环境恢复后端到端复核。

## v2.13.1 (2026-09-22)

### 修复 / 健壮性（列表判停）
- **重复页指纹停**：本页 job 集合与之前某页完全相同（新版列表 `page=N` 无效 → 翻页失效）时**提前结束**，不再空翻（源自同行 BossHunter 手法）。新增纯函数 `page_fingerprint`。
- **连续空页风控双确认**：连续 2 页无数据时**先探测页面是否真有风控/验证码**；命中才 `EXPORT_FAIL risk_blocked`，未命中按"无数据"停止——**不判风控、不写冷却、不告警**（防误停 + 防冷却污染，源自同行 Ccelia 教训）。
- 测试 +3（`page_fingerprint` / 空页双确认两分支 / 重复页停），350 全绿。

## v2.13.0 (2026-09-22)

### 新增
- **多关键词 CLI（§4.1 / 上游 issue #75）**：`--keyword "Java 后端,Java 风控"` 支持逗号分隔多关键词——**依次抓取**各关键词（复用单关键词链路，保留各关键词中间文件作断点续抓），按 `job_id` **自动合并**为一份列表（并合并 `security_map`），再走统一详情/导出/摘要；关键词间自动等待 8–15s 防风控。单关键词行为完全不变。
- 测试 +3（`MultiKeywordTests`：`split_keywords` / `merge_list_data`）

## v2.12.1 (2026-09-22)

### 工程 / CI
- **对齐上游 #81（适配移植）**：`ci.yml` 增最小权限 `permissions: contents: read` 与各 job `timeout-minutes`（防挂死占 runner）。**不采纳**上游 #81 的 `ci.yml` 重写（其面向 3.10/3.13 + uv、无 ruff/覆盖率/契约校验，弱于现有 CI）与 `pullfrog.yml`（第三方 AI 审查机器人，需 secrets，非必需）

## v2.12.0 (2026-09-22)

### 新增
- **移植上游 #55：列表通道双模式 `--list-mode xhr|passive`**（MIT 同源上游，采用其思路与实现结构）：
  - `passive`：导航真实搜索页 + 滚动触发无限滚动，用 CDP `Network` 域**旁听页面自身**的 `joblist.json` 响应（零注入 XHR），消除"注入请求特征触发的 `code 37`"
  - 复用既有 `prefetched_pages` 通道接入串行主循环 → 契约 v2（`observed_jobs`/`exhausted`）、`security_map`、渐进原子写、风控熔断**全部不变**
  - 与 `--pages-parallel` 互斥（passive 自动回串行）；未捕获/异常自动**回退串行 XHR**（稳健）
  - 默认 `xhr`（保持既有速度与行为）；`passive` 待端到端验证后再评估是否改默认
  - 新增原语：`CDPSession.events` 事件缓冲 + `drain_events()`、`NetworkJoblistCapture`、`map_api_job`/`map_api_jobs`（Python 字段映射，与注入 JS 模板 1:1）
- 测试 +11（`PassiveCaptureTests` + CLI `--list-mode`）

## v2.11.1 (2026-09-22)

### 文档 / 工程
- **对齐上游零风险项**（与上游 `eatmoreduck/boss-zhipin-scraper` 对齐，见提交 `f0428f3`/`2bc40f5`/`16cc992`/`848fbb4`/`f7260e9`）：
  - 新增 GitHub Issue 模板（`bug_report.yml` / `feature_request.yml` / `question.yml`，直接采用上游 #77）
  - `CONTRIBUTING.md` 新增「Issue 标签约定」（上游 #59）；`AGENTS.md` 补「issue 分诊打标」
  - README（中英）新增「方式 4：skills.sh 一键安装」（上游 #82）；Star History 链接更新为上游新版

## v2.11.0 (2026-09-22)

### 新增
- **§1.3 `--foreground-capture` 逃生口**：列表 / 登录探测 / 详情 DOM 的自动化页面默认仍后台（避免抢焦点、避免 `document.hidden` 触发 BOSS visibility 反爬，issue #18）；该开关为 Chrome 在 Linux/Xvfb 等环境"后台 Target 捕获不到搜索响应"（上游 #67/#68）提供逃生口。`wait_for_login` 本就前台，不受影响。测试 +3

### 文档
- README / `SKILL.md` 补充 `--foreground-capture`

## v2.10.2 (2026-09-22)

### 修复
- **§1.2 `wait_for_login` 瞬态异常裸崩**（上游 #79 同源）：`TimeoutError` 是 `OSError` 子类而非 `RuntimeError`，原先 `except RuntimeError` 会让 CDP 事件洪流下的瞬态超时**直接带 traceback 退出** `--setup-chrome`。改为捕获 `_cdp_exception_types()` 并计入 transient 重试（超过 `LOGIN_PROBE_MAX_TRANSIENT_ERRORS` 才停）。测试 +1

## v2.10.1 (2026-09-22)

### 文档
- **§1.9「不用代理」红线入文档**：`CONTRIBUTING.md` 拒收类 PR 增列"引入代理池 / IP 轮换规避风控"（实测代理致 `code 7/37`；IP 被封＝账号全部会话作废、换 IP 无效只能重新登录），能力声明「不做」同步；`AGENTS.md`「合规与审计」红线补充"不用代理池/换 IP"。新增 `ComplianceRedlineTests` 守卫（防红线文档被无声删除）

## v2.10.0 (2026-09-22)

### 新增
- **JD 清洗增强（P4e-2）**：DOM 抽取逐行剥离页面 UI 噪声行（"举报/微信扫码分享/去APP/小程序/立即沟通"等，**仅整行噪声才删**，正文含同名片段不误删）；`EXTRACT_DETAIL_JS` 明确以 `innerText` 规避 `<style>`/`display:none` 诱饵文本。测试 +3

### 文档
- README（中英）工作原理节补充 JD 清洗说明

## v2.9.0 (2026-09-22)

### 新增
- **合规红线文档化 + 风险事件审计（P4e）**：
  - 新增 `scripts/audit.py`（稳定纯逻辑，主文件 re-export）：风险事件 append-only JSONL 审计（`~/.boss-zhipin-scraper/risk_events.jsonl`，**仓库外/不进 git**）；UTC 秒级时间戳、5MB 滚动、**best-effort**（不阻塞主流程）、写入前 `_scrub_secrets` 脱敏
  - `send_alert()`（风控/验证码全停、登录失效）与 `mark_cdp_cooldown()`（熔断冷却）接入审计；`BOSS_AUDIT_PATH` 可覆盖路径
  - `CONTRIBUTING.md` 补齐合规工程化：能力声明（做/不做）、批量上限、个人信息边界、**拒收类 PR 明文红线**（绕过安全机制/提频/绕过人工确认/采凭据）、审计说明
  - `AGENTS.md` 增加「合规与审计」节（新增风控/登录/熔断分支须同步落审计）
- 测试 +5（`tests/test_chrome_setup.py::AuditTests`）

### 文档
- README（中英）告警推送节补充本地审计日志说明

## v2.8.0 (2026-09-22)

### 新增
- **P4d 时间字段三件套**：
  - **详情 API 富化（零额外请求）**：`DETAIL_API_JS` 增取 `brandComInfo.activeTime` → 详情记录新增 `brand_active_time`（公司级活跃时间）；复用既有每岗一次详情请求，**不为时间字段单独拉详情**（同行因"列表时间需拉详情"被秒封，故天然满足配额/错峰/失败降级）
  - **DOM 保底**：`EXTRACT_DETAIL_JS` 增取 `div.info-publis>p`（`publish_time` 相对发布时间）与 `.boss-active-time`（HR 活跃）；`extract_detail_fields` 在 recruiter 卡无活跃行时用 `.boss-active-time` 兜底
  - **僵尸岗过滤（opt-in）**：新增 `--filter-inactive`（默认关闭），保守规则仅匹配「周/月/年前活跃」——实测「本周活跃」「2周内活跃」不误杀；命中从导出剔除并在 meta 记 `inactive_filtered`
- 测试 +8

### 文档
- README（中英）/ `SKILL.md` 补充新字段与 `--filter-inactive`

## v2.7.0 (2026-09-22)

### 新增
- **SQLite 增量存储 + 详情断点续抓（P4c-2）**：新增 `--db [PATH]`（可选，默认 `~/.boss-zhipin-scraper/boss.db`，**仓库外、不进 git**；零新依赖，仅标准库 `sqlite3`）。WAL 模式 + `job_id` 唯一键增量 upsert（`first_seen_at` 首次写入后不变、`updated_at` 每次覆盖）；列表/详情与 JSON/CSV **并存不替换**。库中已有详情作为跨 run 续抓种子（换输出文件也能续、免重抓），并保证这些岗位仍带 JD 进入本 run 导出（避免"跳过即丢 JD"）。抽出稳定纯逻辑模块 `scripts/db_store.py`（主文件 re-export，模块边界同 `ratelimit`/`export_contract`）；入库前统一脱敏（cookie/token/securityId/BOSS 内部标识绝不落库，红线不变）。`--batch` 暂未接入（会明确提示本次忽略 `--db`）。测试 +11

### 文档
- README（中英）/ `SKILL.md` 补充 `--db` 参数与 SQLite 增量层说明

## v2.6.0 (2026-09-22)

### 性能（实测定界）
- **列表并行抓页**（新增 `--pages-parallel N`，默认 3；库层默认 1 不改调用方）：多 tab 并发开、同发搜索 XHR，列表阶段从串行 ~55s（含 12-22s 页间等待）降到约 6s；任一页失败自动回退串行
- **详情 pace 15s → 1s/worker**：实测 E3/E4——多 tab 轮换下间隔 **0.5s 连续 40 次仍 code 0、无验证码**，取 1.0s 保守值（2× 余量）；旧值 15s 无实测依据、过保守约 15×
- **每 tab 详情预算 4 → 5**：E3 复核同一 tab 第 6 次才 `code 37`（即可用 5 次）
- **dock tab 等待保留 4-8s（实测否决下调）**：E6 单次零等待下 XHR 虽 OK，但**真实高频轮换**（等待 0.5-1.5s + pace 1s + 并发 3）会触发 `code 37 您的环境存在异常` 真风控（换 tab 清不掉）→ 保持 4-8s，控制"每 tab 一次搜索页加载"速率
- **实测记录**：E5 并行 3 页 0.4s；E1/E2 详情 API 不带 `securityId` 返回 `code 17`（确认必须带，列表↔详情耦合不可去）
- 端到端实测（南京 × 3 页 + 详情并发 3）：整格 **~80 秒**（原约 5.6 分钟）；测试 +2

### 风控（P4a · 同行研究落地）
- **code 全表 + `code 37` 二分**（`classify_boss_code`）：`token_expired`（会话/令牌过期）才刷新会话后重试一次；`env_risk / account_risk / security_block` **换 tab 无用 → 停手 + 冷却**。新增 9/17/19/31/35/36/38/121/122 归类，终结"换 tab 清不掉"的无效重试
- 环境风控命中即进入冷却（复用 `mark_cdp_cooldown`），防"停手后立即重开再触"
- **P4c-1 预算硬帽**：新增 `--max-seconds N`（详情阶段墙钟上限，默认 0=不限）；超限**优雅停**并保留已抓（与既有 `--max-details` / `MAX_API_REQUESTS` 共同构成预算帽）
- **API 通道改 burst-aware 串行节律**（`BurstThrottle`）：请求时刻**全局串行** + 高斯 1.5–3.0s + 5% 长暂停 2–5s + burst 惩罚（15s≥3 / 45s≥6），全局约 **0.44 req/s**（全行安全区）；`DETAIL_API_PACE_SECONDS` 1.0→2.25。替换旧式 `concurrency/PACE`（并发 3 ≈ 3 req/s，踩线触 `code 37`）。**取舍：慢一点换稳、产出完整**
- 新增风控处置 runbook（`docs/projects/boss-zhipin-scraper/boss-zhipin-scraper-风控处置-runbook.md`）
- 测试 +3

### 变更
- **收窄 Python 支持到 3.12**（个人自用）：`requires-python >=3.12`；CI 矩阵去除 3.10；`coverage[toml]` extra 不再需要（3.12 自带 `tomllib`）；ruff `target-version → py312`；文档/徽章同步 3.12+

### 工程 / CI
- **CI 固定 ruff 版本**（`requirements-dev.txt` 钉 `ruff==0.16.4`），避免规则漂移
- **CI 覆盖率门禁**：`coverage report --fail-under=70`（当前约 74%）
- **CI 打包冒烟 job**：`python -m build` → 装 wheel → `boss-scraper --version` / `boss-summary --help`（锁住 hatchling 入口与依赖自洽）
- **ruff 规则扩展**：`E,F → E,F,I,B,UP`（+isort/bugbear/pyupgrade），并修复报告项（B904 `raise ... from None`、B007 未用循环变量）
- 新增 `.pre-commit-config.yaml`（ruff + 基础文件检查）；`pyproject` 增 `[tool.coverage]` 与 `dev` extra
- **版本一致性测试扩展到 README.en.md**（原仅四处）

## v2.5.0 (2026-09-22)

### 新增
- **显式双通道策略 + securityId 受限 sidecar**（2026-09-22，用户拍板 B）：详情抓取在导出 meta 显式标记 `detail_channel`（`api`/`dom`/`mixed`）；`--input` 续抓优先从受限 sidecar 复用 `securityId` 走 API 快通道，未命中则走 DOM 慢通道并**明确告警**。sidecar 严格约束：`~/.boss-zhipin-scraper/.session/`（仓库外）、`chmod 600`、**60 分钟 TTL**、run 正常结束即删、启动清理过期残留；**绝不进导出/日志/git**；**登录 cookie 绝不落盘**（红线不变）。测试 +2

### 重构
- **抽出两个稳定纯逻辑模块**：`scripts/ratelimit.py`（`TokenBucket`/`AdaptiveRateLimiter`）与 `scripts/export_contract.py`（契约/脱敏/口径一/原子写）；主文件顶部兼容 shim 做 re-export，导入面与既有测试不变。`CONTRIBUTING.md`「单文件原则」改写为「模块边界」（编排/CDP/CLI 仍在主文件）。测试 +2（模块 re-export）

### 修复
- **`incr_request` 加锁**：并发详情路径下每任务调用，加锁避免计数漏加/超发（2026-09-22 审计）。测试 +1
- **收窄 Chrome `--remote-allow-origins`**：由 `*` 收窄到本机 `localhost/127.0.0.1:<port>`（安全）
- **复核结论**：`record_counts.duplicate` 的"逐次写盘重复重发量"语义为**刻意设计**（由 `test_flush_jobs_accumulates_record_counts_across_writes` 锁定），审计曾疑其失真，**经复核维持不变**

### 版本
- `2.4.0 → 2.5.0`（四处同步）

## v2.4.0 (2026-09-22)

### 修复
- **限速器并发正确性（高，风控结构性根因）**：`TokenBucket.acquire` 原在锁外 `sleep` 后不再复核令牌——并发下 N 个 worker 同睡同醒、各自扣减 → **突发超速**（本轮实测约 1.75× 名义速率）。改为锁内判定/扣减、锁外等待后**循环复核**；`AdaptiveRateLimiter` 的"连续坏窗口暂停"改为单飞（新增 `_pause_lock`），消除"N 线程同时长睡"的暂停风暴。新增**并发回归测试**（容量 1 / rate=10 / 4 线程，断言授予时刻串行化、无突发放行）。测试 +1，290 全绿 + ruff 全绿
- **串行详情 API 通道间隔达标**：默认 `--concurrency 1` 的 API 通道间隔改为 `≥ DETAIL_API_PACE_SECONDS(15s)`（原 `10-25s` 随机可能低于硬线）；DOM 通道维持 `10-25s`。同批修正既有的 3 个 TokenBucket 单测（原先误把时钟 mock 成 `time.time`，实现用的是 `monotonic`，属"靠真实时间碰巧通过"）。
- **并发详情城市码透传（一致性）**：调用 `_scrape_details_parallel` 时补齐 `city_code`（此前仅传城市名 → 并行详情 API 的 `city` 参数为空；实测未致失败，但属潜在隐患）。
- **`--input` 只读（数据安全）**：修复 `--input` 模式下详情风控回调 `_note_detail_risk_blocked` 会把**用户输入文件**当输出追加 `warnings` 写坏的问题。
- **脱敏补全**：`_scrub_secrets` 覆盖 `securityId`/`security_id`（纵深防御；该值本就仅内存传递、不落导出）。

### 文档 / 依赖
- **依赖自洽**：`pyproject.toml` `dependencies` 补 `pandas`/`matplotlib`（`job_summary.py` 顶层导入、`boss-summary` 入口依赖；此前缺失会导致安装后 `ModuleNotFoundError`），与 `requirements.txt` / `uv.lock` 对齐。
- **README 中英双语**：导出契约标题与示例 `format_version` 由 `1` 更正为 `2`。
- **SKILL.md 重写**：默认端口 `9222 → 45222`（9222 在 BOSS 安全 JS 扫描名单内，照旧跑会踩坑）；补齐 `--batch/--status/--verify/--list-results/--archive/--max-jobs/--concurrency/--max-concurrent/--retry-job/--keep-without-jd/--input/--industry/--list-cities` 等参数；JSON 示例对齐契约 v2；工作原理补"详情 API 通道"；依赖说明补 pandas/matplotlib；platforms 补 windows。
- **版本**：`2.3.0 → 2.4.0`（脚本/pyproject/SKILL/README 四处同步）。

## v2.3.0 (2026-08-11)

### 新增
- **`--status` 状态总览（接管用）**（2026-09-22）：一条命令**离线**输出——① **运行态**：CDP 端口连通、Chrome profile、互斥锁（空闲/持有/熔断）、熔断冷却/恢复期；② **缓存态**：结果目录（列表/详情/pending/归档 文件数 + 占用）与**最新列表文件 meta 摘要**（keyword/city/jobs/jd 覆盖率）；③ **技能/入口**：项目与工作区文档指向 + `git HEAD`。**不发 BOSS 请求**（登录态仍需 `--check` 探测）。面向"换窗口无需复杂交接"：三类状态的真相全部落在磁盘且一条命令可问清。测试 +2（离线状态汇总 / CLI 互斥），289 全绿 + ruff 全绿
- **口径一：jd 并入导出并剔除无 JD 岗位**（2026-09-22，用户拍板"没 JD 的岗位毫无意义"）：详情抓完后把 `jd` 直接并入 `boss_jobs_*.json` 每条 job，并**剔除无 JD 的岗位**（下游无需再筛）；meta 记录 `jd_coverage{with_jd,total_before,dropped_no_jd}` + `dropped_no_jd`（被剔除 job_id 清单，可追溯）+ warnings 标注；`--keep-without-jd` 可保留并仅标注。单命令与 `--batch` 路径一致应用。**实测**：8 条详情 → 导出 8 条（每条含 jd）、剔除 22 条、`validate_export v2.0.0 ok=True`。测试 +3，287 全绿 + ruff 全绿
- **详情 API "每 tab 配额"实证与自动轮换**（2026-09-22，重要修复）：实测**同一 tab 连续约 4-5 次详情 API 请求后返回 code 37，换新 tab 立即重置**（A tab 4 条耗尽 → B tab 立即再拿 5 条，实证）。此前"第 5-6 条必挂"被误判为风控 → 现改为：① 按 `DETAIL_API_TAB_BUDGET=4` **主动轮换 tab**（串行与并发共享池均支持，轮换=关旧开新+导航一次，hits 归零）；② 仍遇风控码时**换 tab 重试一次**，再失败才判定真风控并全停。**实测 12/12 条详情全成功（此前 5 条即挂）**；测试 +2（预算轮换 / 风控换 tab 重试），284 全绿 + ruff 全绿
- **`--batch` 默认抓完整（审计修复，2026-09-22）**：批量模式此前**只出列表**（忽略列表阶段 `security_map`），与"默认抓完整"设计不一致——是批量重抓的静默陷阱。现在列表抓完即接详情（详情走 API 通道、securityId 内存传递、详情文件由列表文件同名推导）；`--no-detail` 或任务级 `detail:false` 可仅列表；命中详情风控（验证码/风控码）→ 全停并提前结束批量（不硬闯，等人工处理后重跑，断点续抓自动补齐）。测试 +3（默认详情/--no-detail/任务级覆盖），282 全绿 + ruff 全绿
- **并发详情 API 通道优化 + 审计修复**（2026-09-22）：① **共享 tab 池**——并发模式预建 `concurrency` 个停靠 tab（每 worker 一个、只导航一次），替代此前"每岗自建 tab 且用岗位标题导航搜索页"（消除每格 N 次无关搜索请求，速度与风控双改善）；② **限速基线修正**——详情 API 单次约 1s、无整页渲染等待，限速器成为唯一刹车，旧基线 `concurrency*0.5/秒`（为 DOM 渲染路径设计）对详情接口过快（并发 3 → 1.5 次/秒）；改为"每 worker 至少 `DETAIL_API_PACE_SECONDS=15s`"（并发 N → 全局约 N/15 次/秒，与串行 10-25s 间隔同量级），DOM 路径基线不变；③ **回退导航修正**——API 通道自建 tab 的停靠页改用真实抓取关键词（此前误用岗位标题）。测试 +3（共享池/API 基线/DOM 基线），279 全绿 + ruff 全绿
- **详情 API 通道（详情抓取主路径改造）**（2026-08-14）：`/wapi/zpgeek/job/detail.json?jobId=&securityId=&city=` 实测 code 0——每岗 1 次轻量 XHR 替代详情页整页 DOM 渲染（JD 秒回 vs 渲染 10s+；实测 5 条 597/714/587 字完整 JD）。**设计要点**：securityId 由列表阶段同进程内存收集（`scrape_list` 返回 `security_map`，不落导出文件——红线保持），`--input` 老文件无 securityId 自动回退 DOM 渲染；串行模式复用 1 个停靠 tab（只导航一次）；失败分类：风控码→risk_timeout（全停机制实测正常：EXPORT_FAIL+告警+warnings）、短 JD→invalid_detail 不进 pending、网络→cdp_session 进 pending；**可选字段**：`job_status_desc`、`brand_introduce`（公司介绍）、`brand_stage_name/scale_name/industry_name`（早期含 address/经纬度/invalid_status/boss_certificated，后于 1289344 按用户拍板精简去掉）——全部可选，契约零影响（format_version=2 不变）。**已知退化点**：page_update_date 仅 DOM 路径有（API 通道留空，可选字段）。测试 +4（URL 构建/解析成功/失败分类/security_map 收集），276 全绿 + ruff 全绿
- **anonymous 匿名岗标记提取**（2026-08-13，用户拍板）：列表 API `anonymous`（0/1，招聘方隐藏公司名的匿名岗位——求职透明度信号）加为可选字段（`!== undefined` 保留 0 值）；契约零影响；测试断言并入市场字段测试，272 全绿 + ruff 全绿
- **摘要层销售岗过滤**（2026-08-13，用户拍板）：BOSS 搜索为模糊匹配，搜"AI产品经理"会混入"AI产品销售经理"等非目标岗（42 格实测 5044 条中 title 级 80 条 ≈1.6%，部分格如南京×AI产品运营 8%）——job_summary 分析时自动剔除：title 强特征（销售/BD/客户经理/渠道经理/SDR 等）或 JD 强特征（提成/业绩指标/回款/签单等）；产品岗 JD 仅含"销售"单字不误伤（如"提升销售转化"）；剔除数在摘要标注 `已剔除 N 条疑似销售岗`。分析层行为，**导出契约零影响**；测试 +3，272 全绿 + ruff 全绿
- **exhausted 页底标志实施（B 联调 T2 契约 v2 前置，规格侧定案授权）**（2026-08-13）：scrape_list 每页判定 `len(jobs) < PAGE_SIZE(=30) → exhausted=True`（`--pages 1` 单页 <30 同理；满页未到底 false；空页走风控分支不参与）；两处 flush（中间页/最终页）meta 顶层加 `exhausted: bool`——规格侧抽样感知下架判定依据（全量观测格才判下架）。**format_version 切换（1→2）与校验器 2.0.0 同步待规格侧契约 v2 落档后一次实施**（当前字段先产出，旧校验器忽略多余字段无兼容问题）。测试 +2（短页 true/满页 false），269 全绿 + ruff 全绿
- **市场信息字段提取**（2026-08-13，探测 29 字段集对照盘点）：列表 API 新增 5 个可选字段——`job_valid_status`（BOSS 官方在招状态，可交叉校准 B 增量 last_seen 下架推断：同格状态=失效可直证下架、重见且=1 可证观测缺口/复活）、`icon_flags`/`icon_word`（"急"/"新"平台标签，"新"≈新发职位近似信号）、`proxy_job`/`proxy_type`（代招标记：猎头/外包）、`job_type`（岗位类型编码）。契约零影响（可选字段，format_version=1 不变）；测试 +1（模板字段断言），267 全绿 + ruff 全绿
- **页面更新时间提取（page_update_date）**（2026-08-13）：详情页 DOM 存在 `页面更新时间：YYYY-MM-DD`（BOSS 唯一岗位侧日期，招聘方最后编辑岗位时间；平台不公开发布日期）——详情抓取时提取为可选字段 `page_update_date`（缺失留空），用于区分"岗位侧更新时间"与"我方抓取时间"（scraped_at）；列表 API 探测实证无任何时间字段（29 字段 timeKeys=0）。契约零影响（可选字段，format_version=1 不变）；测试 +2（提取/透传），266 全绿 + ruff 全绿
- **告警推送补全（登录失效接入）**（2026-08-13）：登录状态探测失败（UNAUTHENTICATED/RESTRICTED/RESPONSE_ERROR）退出前调用 `send_alert`，推送 `EXPORT_FAIL reason=login_failed status=<状态> city=... keyword=...`（与规格侧告警语义对齐，运行指示 reason 枚举含 login_failed）；触发点齐备：EXPORT_FAIL risk_blocked ×4 + 验证码全停 + 登录失效。README 中英补「告警推送」小节（.env 配置、静默旁路说明）；测试 +1（三状态 subTest 断言标题/文本/city），264 全绿 + ruff 全绿
- **分析产品化（升级方向 G 启动，pandas + matplotlib 引入）**：①**统计口径修正**（第四轮调研）——中位数取两中位均值（偶数样本此前取上中位，如 [30,45]→45 修正为 37.5）、薪资区间改 **P10/P90 分位**（极值在小样本不稳定，替代 min/max）、样本量警示（可解析 <30 条时摘要头部输出警示行）②**N薪解析** `parse_salary_annual`（"20-40K·15薪"→年薪 300-600K，无月数按 12 折算——15薪 vs 13薪按月度排名不公平）③**图表生成** `generate_charts`：薪资中位分布直方图 + 经验档位×薪资中位条形图（matplotlib Agg 无头 PNG，中文字体 Microsoft YaHei 实测可用），默认输出到结果目录 charts/，摘要尾部 Markdown 引用；CLI 新增 `--charts-dir`/`--no-charts` ④依赖新增 pandas+matplotlib（requirements 更新，CI 自动覆盖）
- **第三轮中价值落地**（2026-08-13）：①**run 级血缘字段**——导出 meta 补 `run_id`(uuid4)/`scraper_version`/`started_at`/`ended_at`/`record_counts`（new/duplicate/quarantine 跨次累积，渐进写盘正确），"这次运行是谁、什么代码、跑了多久、产了几条"可追溯 ②**键冲突检测**——同 job_id 不同 payload 记 `key_conflict` quarantine 并警告（merge 旧版本优先 + 冲突显式暴露，不再静默覆盖；sanitize 后比较，跨 run 续抓不误报）③**预算分账**——`incr_request(kind)` 探测/列表/详情独立计数（总量上限语义不变）④**冷却恢复分级**——熔断冷却文件升级两行（冷却截止+恢复期截止 120s），`check_cdp_recovery` 恢复期内详情限速减半渐变回升；冷却过期不再删文件（恢复期信息保留，恢复期结束才清理）⑤**平台漂移信号**——详情失败 ≥5 且 CDP 会话失败占比 >50% 时输出漂移警告行（列表正常但详情异常=平台侧变化，勿反复重试）⑥**CLI 契约测试**——新增 `tests/test_cli_contract.py`（--version 格式/--help 7 分组/未知参数 exit 2/冲突命令 exit 2/--archive 非整数 exit 2，subprocess 锁定 CLI 表面）
- **第三轮最佳实践落地**（4 路并发子代理调研，2026-08-13）：①**CDP 会话健壮性**——应用层心跳保活（后台线程 HTTP 探活，NAT/代理静默断连时标记连接死亡，防僵尸连接）+ 断线快速失败（`_dead` 标记与 WebSocket 异常立即抛 ConnectionError，不再挂 30s 超时）+ **渲染进程崩溃识别**（`Inspector.detached`/`Target.targetCrashed` 事件立即抛 `TargetCrashedError`——OOM 时 pending evaluate 永久挂起是社区已知 bug）②**CDP 端口改固定高位 45222**（BOSS 安全 JS 实证只扫描 9222/9223/9229 检测 CDP；跨进程一致）③**未知非零风控码一律归 RESTRICTED**（降速语义，不再误判 RESPONSE_ERROR 静默放弃；删除 message 关键字兜底死代码）④**凭证日志脱敏** `_scrub_secrets`（cookie/token/__zp_stoken__ key=value 形态替换为 \*\*\*，catch-all 错误消息强制脱敏）⑤**CLI 误用语义修正**——动作型命令互斥组（--check/--verify/--archive/--setup-chrome/--stop-chrome 等同时给直接 exit 2）、--archive 非整数与城市无法解析改 `parser.error()`（exit 2，与"1=运行期错误"区分）⑥**Windows subprocess 显式 UTF-8**（3 处 text=True 补 encoding/errors，PEP 597）⑦**logging.basicConfig 移入 main()**（import/测试不再动 root logger）⑧**CI 并发取消**（连推时旧 run 自动取消）
- **CLI 工程化**（最佳实践调研第二批）：①argparse 参数按 7 组分组（搜索/筛选/输出/详情/工具/Chrome/通用），`--help` 从一堵墙变为可读结构，并显示各参数默认值；②**退出码固化**：0=成功 / 1=运行期错误（登录失败/风控/未预期异常——入口 catch-all 输出干净错误消息，无 traceback）/ 2=CLI 误用（argparse 默认），已写入 README；③原子写（tmp+os.replace）补 `flush+fsync`（`_atomic_write_json`/锁文件/冷却标记三处，Windows 用 `os.fsync(f.fileno())`——实测该平台 TextIOWrapper 无 fsync 属性），断电不丢数据；④启动自动清扫残留 `.tmp` 文件（只删超过 300s 的，防误删并发进程正在写的 tmp）；⑤**入口 schema 校验**：写盘前校验契约必填字段（job_id/title/location/job_link/company_name），缺失即从导出剔除并记入 `meta.quarantine`（job_id + 原因），防页面结构漂移产出脏数据进下游——已实测不破坏消费端契约校验（vendor v1.0.0 ok=True）；⑥`-v/--verbose` 升级为可叠加计数（`-vv`），新增 `-q/--quiet`（日志降到 WARNING，结果行/EXPORT 行仍输出 stdout，quiet 优先）
- **并发安全加固**（最佳实践调研第一批落地）：①锁文件 RMW 竞态修复——`O_CREAT|O_EXCL` guard 文件对读-改-写做进程间互斥（多进程并发 acquire 不再丢 pid、并发上限不被突破）②TokenBucket/AdaptiveRateLimiter 计时改 `time.monotonic()`（NTP 回拨不导致桶爆满/窗口误判）③风控码表补 35/36/38（boss-jd-scraper 实测 BOSS 常用码）④凭证自愈重试加指数退避（`uniform(6,10)*2^(N-1)`，AWS full jitter 思想）⑤连续 2 页无数据按风控静默降级处理（EXPORT_FAIL 停止，不再静默跳过）⑥CDP 熔断冷却期（300s，防"熔断→立即重启→再熔断"循环，`--reset-lock` 前拒绝自动重开）⑦失败分类：解析类失败（invalid_detail）不进 pending（重试浪费且掩盖结构漂移信号），网络类（cdp_session）照常重试
- **互斥锁升级为"并发上限可配"**（ai-pm-job-intel 规格 §3.6 修订）：锁文件从单 pid 升级为「pid 列表 + 最大并发数」（`~/.boss-zhipin-scraper/scrape.lock`，首行上限、余行持有 pid）；新增 `--max-concurrent N`（**默认 1**，现状行为不变，超上限仍 `lock_held` 拒启动）；**熔断广播**：任一并发任务遇 code 37/验证码 → 锁文件置 `risk` 标志，其余任务页间分片检查立即全停（不降并发续跑），挂起等人工，`--reset-lock` 清除后重开；并发 >1 时页间隔自动拉长 12-22s → 20-30s；SKILL.md 守则同步（并发只准指令显式开启 + 风控全停）
- **双端契约一致校验**（与 ai-pm-job-intel 消费端对齐）：消费端校验器按 SHA 固化为 vendor 副本（`tests/fixtures/consumer_validator/`，含 v1.0.0 版本号），CI 新增 `contract-check` job 对契约样例 fixture 跑 `validate_export.py`，断言退出码 0 且输出含 `v1.0.0`（版本漂移即红，触发双端对齐）；本地回归测试同步覆盖。契约样例 fixture 重出：剔除 `security_id`/`lid`/`encrypt_*` 内部标识（新版 `tests/fixtures/sample_export_v1.json`，30 条实测通过）
- **导出契约 v1**（ai-pm-job-intel 规格 §3.2）：导出 JSON 顶层加 `format_version: 1`；meta 补 `page_count`/`warnings`（API 空数据、风控等异常留痕）
- jobs 字段契约化：新增 `company_name`（与 boss_name 同值，规格必填）、`experience`/`education` 独立字段（原合并于 tags）、`skills` 改为数组
- **AS-8 结构化结果行**：抓取完成输出 `EXPORT_OK jobs=N city=X keyword=Y path=Z`，风控中断输出 `EXPORT_FAIL reason=...`，供下游 30 秒判断可信度
- **NFR-3 合规**：列表 API 尝试上限 3→2（最多 1 次自动重试）
- **NFR-6 安全**：导出前过滤敏感字段（cookie/token/账号等，防外部数据混入凭据）
- job_summary 新增**薪资×经验交叉表**（各经验档位的岗位数/薪资中位数，参考开源招聘分析 dreamhole 实践）与**高薪岗位榜单**（按薪资上限 TOP N：岗位/薪资/公司/地区），补齐"筛选值得投的岗位"环节
- job_summary 新增**薪资行情统计**（可解析条数/中位月薪/均值/区间，支持 K 与日薪折算）与**公司规模/融资阶段分布**；提示词新增"薪酬预期建议"维度
- job_summary 提示词**转交给 agent 做语义分析**：移除 JD 高频词（语义弱替代、污染高），提示词引用完整列表/详情数据文件路径，引导 agent 读取文件做深度调研；脚本只保留可靠的聚合统计与结构化技能标签
- JD 动态高频词过滤扩展：需要/落地/具备/负责等纯功能词不再冒充技能词（产品/设计等方向词保留）
- 详情抓取阶段进度汇报：串行与并发在逐条打印之外，每 `max(10, ceil(总量×5%))` 条输出一行汇总（含百分比与成功/失败数），完成必有最终汇总；总量 ≤200 固定每 10 条一报
- 抓取结束统计：耗时、平均秒/条、成功/失败原因分类（cdp_session/invalid_detail 等）
- 断点续抓提示：详情抓完有失败时提示"重跑刚才的命令即可自动补齐"（输出路径不变自动加载 pending）
- 新增 `--list-results`：列出结果目录历史文件（分类/时间/大小）
- 新增 `--archive [KEEP]`：归档历史结果文件（每类型保留最新 KEEP 个，其余移入 `archive/`；pending 活动文件不归档）
- 新增 `--batch CONFIG.json`：批量列表抓取（JSON 数组任务配置：keyword/city/pages/sleep/筛选），任务间自动等待防风控
- 新增 `--max-jobs N` 列表条数上限：抓够即停不再翻页（BOSS 每页 30 条，实际条数可能略超；不设则按 `--pages` 抓满）
- 登录探测会话内缓存：同一进程 10 分钟内的重复登录检测（`--check`/抓取前）直接复用上次结果，避免重复开 tab、导航与请求
- 新增 `--verify` 命令：校验已抓取结果文件完整性（列表/详情可解析性、job_id/title 必填、重复 job_id、JD 过短、列表-详情覆盖率），只读不联网，不依赖 Chrome
- 新增 `--retry-job JOB_ID`（可重复指定）：强制重试指定详情，无视 pending 重试上限，未在 pending 文件里的也会重抓
- 并发详情抓取：新增 `--concurrency N` 参数（默认 1=串行，行为不变；2-3 推荐）。线程池 worker 各自独立 CDP 会话；**全局限速令牌桶** + **错误率自适应降速**（窗口失败率 >30% 自动降半，连续坏窗口暂停 60s，健康后恢复——Scrapy AutoThrottle 思想）；worker 只取数、主线程统一合并写盘与 pending；登录墙/连续会话失败触发全局停止（Event 广播）；单任务异常不阻塞其他 worker。实测：并发 2 抓 10 条约 3 分钟，成功率为串行同水平（不引入额外失败）
- 页面级风控/验证码检测与人工介入：列表页与详情页自动探测滑块验证、安全验证页、登录墙（四重判据），命中时明确提示并等待人工处理（默认 120s），超时自动跳过并记录
- 凭证自愈：列表 API 无数据时刷新页面重新完成挑战后重试（最多 3 次）；详情页提取为空时自动刷新重试一次
- 历史 job_id 预加载：详情抓取启动时读取已有结果文件，已抓过的详情直接跳过（省请求、降低风控触发概率）
- 断点续跑：抓取失败的详情 job_id 自动记录到 `<输出>.pending.json`，下次运行自动重试，成功后自动清理
- 登录校验轮换探测：`--check`/抓取前的登录探测用多组关键词/城市轮换，单次"恰好 0 结果"不再误判为未登录；未登录与风控命中立即返回
- 新增 `-v/--verbose` 参数输出 DEBUG 日志
- 新增 ruff 配置与 GitHub Actions CI（ubuntu + windows × Python 3.10/3.12：lint + 全量单测 + 语法检查）
- 详情/列表结果新增独立字段 `boss_active_status`（如「今日活跃」「在线」）：列表兼容 `activeTimeDesc` 与 `bossOnline`（仅在线时映射为「在线」）；详情页从招聘者卡片解析更细粒度状态并优先保留；JD 正文仍剔除该行，不混入描述
- 新增 `--stop-chrome` 命令：抓取/分析完成后关闭 BOSS 专用 CDP Chrome（按 user-data-dir 精准匹配隔离 profile，不碰主 Chrome）；抓取命令新增 `--close-chrome` 选项，正常结束后自动收尾（默认关闭，异常退出不触发以保留登录态）。复用已有 `stop_cdp_chrome` 的安全匹配逻辑，补齐进程关闭/收尾链路的单元测试。（#26）
- 城市码表外置为 `data/city_codes.json`（全量 300+ 城市，覆盖一二三四五线），新增 `--list-cities [关键词]` 命令查看支持的城市；`resolve_city` 查询链改为「本地静态码表 → 运行时拉 BOSS 接口 → 9 位裸码兜底」。城市码表打进 wheel，`pip install` 用户也可用。（#24）

### 修复
- pending 升级为「job_id → 重试次数」映射（兼容旧纯字符串格式）：自动重试上限 3 次，超出后放弃——短 JD 等永久失败的岗位不再每次抓取都反复重试浪费请求
- 并发详情抓取改为**有界提交窗口**（在飞任务 ≤ 并发×2，完成一个补提交一个）：不再一次性提交全部任务，内存有界、熔断/登录墙后停止即时生效，千级任务安全（此前全量提交，熔断后剩余任务仍会逐个执行）
- 并发详情抓取可靠性补齐：每完成 `write_every`（默认 5）条即原子渐进写盘 + pending 落盘，中断最多丢 5 条（此前并发模式仅在全部完成后写盘一次，中断会丢本次全部成功数据）
- 并发模式逐条进度打印（`[并发 完成数/总数] 标题 ✓/✗原因`）；串行模式恢复"加载页面 / 模拟滚动"详细打印（`--concurrency 1` 行为与旧版一致）
- 修正全局限速注释：令牌桶提供的是弱错峰（错开同时导航的瞬时突发），实际请求间隔主要由每条详情固有的加载/滚动等待决定，失败率升高时由自适应降速兜底
- 并发层主线程循环移除多余加锁（results/pending 仅主线程访问）
- Windows 兼容：`main()` 入口将 stdout/stderr 重配为 UTF-8，修复 Windows GBK 控制台遇到 emoji（✅❌⚠️ 等）输出直接 `UnicodeEncodeError` 崩溃的问题（实测此前 73 个单测中 8 个因此失败）
- JSON 落盘改为原子写入（临时文件 + `os.replace`）：进程中断不再留下半截 JSON 覆盖旧数据；`flush_jobs`、详情页写入与 `--merge` 详情落盘统一走 `_atomic_write_json`
- `--check` 的 CDP 连通检查不再把任意 CDP 服务误报为「Chrome」，改为输出实际服务标识
- 清理死代码与重复实现：删除无调用方的 `append_json`；`flush_jobs`/`merge_jobs`/`merge_details_from_lists` 的读-去重-写收敛到 `merge_unique`；`parse_jobs_eval_value` 与 `parse_api_jobs_eval_value` 合并（smoke test 改用后者，行为不变）
- 测试平台适配：Chrome 进程查询的 mock 输出改为按平台生成（Windows 分支解析 PowerShell JSON、POSIX 分支解析 ps 文本），修复 Windows 上 3 个既有测试失败；`test_help_does_not_require_cdp_runtime_dependencies` 显式指定 UTF-8 解码；新增 `merge_unique` / `_atomic_write_json` / `flush_jobs` 单元测试（全量 92 个测试通过）
- 城市解析先执行本地及在线码表的正反向映射，再接受未收录的 9 位裸城市码；未知城市名现在会在抓取前明确报错退出。在线城市接口同时校验业务 `code`，不再把 `code: 35` 等风控响应静默当作空码表
- 登录探测识别 BOSS 风控码 `code: 37`「您的环境存在异常」为限制状态（RESTRICTED），并对未知风控码按 message 关键字（环境存在异常、访问频繁、安全校验等）兜底识别；避免已登录但被风控/限流的用户被误判为「登录探测响应异常」而无法继续。（#33）
- 登录探测改为区分可用、未登录、限制、空结果和响应异常；每轮仅请求一次并采用有上限的退避等待，`code: 31` 等明确限制会立即停止。探测请求现已纳入全局请求预算，CLI 不再把风控或异常统一提示为未登录。（#31）
- 登录检查、列表/详情抓取和 smoke test 的临时标签页统一在后台创建，仅人工登录页置前，避免自动流程抢占前台焦点（#28）
- 详情页 JD 改为只提取“职位描述”区，并在登录墙、导航页或过短正文出现时拒绝写入，不再把整页 `body`、招聘者信息、公司介绍和推荐职位当作 JD
- 同步 BOSS 当前 `city.json` / `condition.json` 映射，修正城市码以及薪资、经验、学历筛选枚举漂移，并在内置城市表未命中时自动加载 BOSS `cityGroup.json` 支持更多城市中文名
- `scrape_details` 最终保存改用 `os.path.dirname(path) or "."`，`--detail-output` 传不带目录的裸文件名时不再抛 `FileNotFoundError`（与循环内及其它写文件处保持一致）
- 修正城市码：天津 `101030100`、沈阳 `101070100`（原均误用 `101060100`）
- `require_runtime_dependencies` 缺失依赖时同时提示 uv 和 pip 安装方式
- `--merge` 现在会合并旧详情并落盘到 `--detail-output`（之前只合并列表，详情丢失）
- API URL filter 改用 `urlencode`（原字符串拼接，filter 值含特殊字符会出错）

### 变更
- 平台支持声明改为 macOS + Linux（Windows 代码分支保留但未经实测，不再声称支持，避免过度承诺）
- `pyproject.toml` 删除空的 `[csv]` extra（csv 是标准库）
- SKILL.md 脚本路径解析改用 Python `os.path.realpath`（macOS 自带 `readlink` 无 `-f`）

### 新增
- `scripts/job_summary.py` 抓取后摘要脚本：读取已有 JSON，输出岗位聚合摘要和求职材料优化提示词
- `boss-summary` 命令行入口，便于打包安装后直接运行摘要脚本
- 抓取后摘要测试：覆盖 JSON 加载、聚合维度、提示词输出和项目边界
- 版本号一致性测试：校验脚本、pyproject.toml、SKILL.md、README.md 四处版本同步
- CONTRIBUTING.md 贡献指南

## v2.0.0 (2026-06)

### 新功能
- `--check` 环境检查（CDP 连通性、依赖、登录态）
- `--setup-chrome` 一键启动 Chrome CDP（持久隔离 profile）
- `--copy-login-state` 手动导入主 Chrome 的 Local State + Cookie 相关文件到隔离 profile
- `--reset-chrome-profile` 重建 BOSS 专用 Chrome profile
- `--setup-chrome` 默认等待 BOSS 登录完成，并确认接口返回明文薪资
- `--no-wait-login` / `--login-timeout` 控制 setup 登录等待
- 默认抓取结果保存到 `~/.boss-zhipin-scraper/job-result`
- 未传 `--city` 时默认搜索上海
- `--format csv` 同时导出列表 CSV 和详情 CSV
- `--merge` 合并多次抓取结果（去重）
- `--cdp-port` 自定义 CDP 端口（默认 9222）
- `--smoke-test` 用真实 Chrome/CDP 跑一次搜索 API smoke test，不写结果文件
- `--allow-dom-fallback` 显式允许 API 失败时降级 DOM 提取
- `--version` 查看版本号
- 登录态检测：未登录时给出明确提示
- 分析报告技术词动态提取（不再硬编码）
- 进度显示：`[2/3 页, 45/90 条]`

### 改进
- CDP WebSocket 消息过滤 + 超时重试（不再无限卡死）
- 详情页写入去重（中断重跑不重复）
- 请求频率保护（最多 10 页，全局 500 次上限）
- 清除所有 bare except，改为具体异常类型
- API 路径提取为常量，方便维护
- DOM fallback 标记为 deprecated
- DOM fallback 默认关闭，避免把字体反爬后的薪资写进结果
- API 错误行不再被当成职位数据处理
- 详情输出保留 `job_id`、`job_link` 和 `salary_source`
- 详情页访问会带上列表 API 返回的 `securityId` / `lid` 上下文
- `--input ... --analysis --no-detail` 会从 `--detail-output`、同目录同时间戳详情文件、默认结果目录最新详情文件中加载详情
- 登录态检测改为多关键词、多城市 probe，但仍要求接口返回明文薪资
- Linux / Windows 平台支持（Chrome 路径 + 隔离 profile）
- pyproject.toml 版本锁定依赖

### 安全
- 默认不软链接、不复制主 Chrome profile；首次启动也不自动导入主 Chrome 登录态，避免影响 Gmail/GitHub 等主浏览器登录态
- API URL 可配置（`API_JOB_LIST_PATH` 常量）

## v1.0.0 (2026-06)

### 初始版本
- Chrome CDP 抓取 BOSS直聘职位列表
- API 明文薪资（绕过字体反爬）
- 详情页 JD 抓取 + 技能标签提取
- 增量写入（异常退出不丢数据）
- 分析报告（薪资分布、经验要求、简历建议）
- 多维筛选（规模、融资、薪资、经验、学历、行业）
