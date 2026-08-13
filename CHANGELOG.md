# Changelog

## v2.3.0 (2026-08-11)

### 新增
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
