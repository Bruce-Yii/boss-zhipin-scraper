---
name: boss-zhipin-scraper
description: "Scrape BOSS直聘 (job listing site) via Chrome CDP. Searches jobs by keyword/city/filters, fetches JD details, outputs structured JSON/CSV with plaintext salary, and can summarize scraped results into a job-market prompt. Use when user wants to search/analyze jobs on BOSS直聘 or zhipin.com."
version: 2.15.2
author: eatmoreduck
license: MIT
platforms: [macos, linux, windows]
metadata:
  hermes:
    tags: [scraper, jobs, career, cdp, chrome, zhipin, boss直聘]
---

# BOSS直聘职位抓取工具 v2.15

通过 Chrome CDP 协议抓取 BOSS直聘 (zhipin.com) 职位数据，输出结构化 JSON/CSV（含明文薪资），并可对已抓取结果生成聚合摘要和求职材料优化提示词。

## 前置条件

- Chrome 浏览器已安装
- Python 3.12+
- 用户已登录 zhipin.com（或愿意手动登录）

## 脚本位置

本 skill 的脚本在 skill 目录下：

- `scripts/boss_cdp_raw.py`：抓取主脚本
- `scripts/job_summary.py`：抓取后摘要和提示词脚本

**运行任何命令前，必须先确定脚本的绝对路径。**

用以下方式找到脚本（macOS 自带的 `readlink` 不支持 `-f`，用 Python 解析路径更通用）：

```bash
# 方法 1：已知 skill 安装目录（推荐，macOS/Linux 通用）
SKILL_DIR="$(python3 -c "import os,sys;print(os.path.dirname(os.path.realpath(sys.argv[1])))" "$0")"
SCRIPT_PATH="$SKILL_DIR/scripts/boss_cdp_raw.py"
SUMMARY_PATH="$SKILL_DIR/scripts/job_summary.py"

# 方法 2：搜索 hermes skills 目录
SCRIPT_PATH=$(find ~/.hermes/skills -name "boss_cdp_raw.py" -type f 2>/dev/null | head -1)
SUMMARY_PATH=$(find ~/.hermes/skills -name "job_summary.py" -type f 2>/dev/null | head -1)
```

如果找不到脚本，说明 skill 未正确安装，需要重新安装。

## 依赖安装（首次使用必须执行）

脚本依赖 `websocket-client`、`requests`（抓取）与 `pandas`、`matplotlib`（摘要与图表，`job_summary.py` 顶层导入）。在用户项目的 venv 中安装：

```bash
uv add websocket-client requests pandas matplotlib
# 或
pip install websocket-client requests pandas matplotlib
```

## 自动化流程

当用户要求搜索/抓取 BOSS直聘 职位时，**严格按以下顺序执行**：

### 第 1 步：检查环境

```bash
python3 "$SCRIPT_PATH" --check --cdp-port 45222
```

检查三项：Python 依赖 → CDP 连通性 → 登录态。

- **全部通过** → 跳到第 3 步
- **CDP 不通** → 继续第 2 步
- **依赖缺失** → 先装依赖（见上方依赖安装），再重新 --check
- **未登录** → 告诉用户打开 Chrome 登录 zhipin.com，然后重新 --check

### 第 2 步：启动 Chrome CDP（仅在 --check CDP 不通时）

```bash
python3 "$SCRIPT_PATH" --setup-chrome --cdp-port 45222
```

这会自动完成：
1. 创建或复用持久隔离 Chrome profile
   - `~/.boss-zhipin-scraper/chrome-profile`
2. 只关闭使用该隔离 profile 的旧 BOSS CDP Chrome，不关闭用户主 Chrome
3. 以 CDP 模式启动 Chrome（`--remote-debugging-port=45222`）
4. 等待 CDP 端口就绪（最多 30 秒）
5. 打开 BOSS 登录页并等待登录完成，直到搜索接口返回明文 `salaryDesc`

默认不复制主 Chrome 的 Cookie、密码、历史记录或扩展；首次启动和后续重复启动都只是创建或复用该专用 profile。首次使用时告诉用户：请在弹出的 BOSS 专用 Chrome 浏览器中访问 zhipin.com 并登录。脚本会等待登录完成并确认接口能返回明文薪资。该专用 profile 是持久目录，机器重启后登录态仍保留，重复运行 `--setup-chrome` 不会清空它。

仅当用户明确要求从主 Chrome 手动导入 BOSS 登录态时，可使用：

```bash
python3 "$SCRIPT_PATH" --setup-chrome --copy-login-state --cdp-port 45222
```

`--copy-login-state` 每次运行都会覆盖隔离 profile 内对应的 Cookie 相关文件；日常启动不要加这个参数。它只复制 `Local State` 和 `Default/Cookies*`、`Default/Network/Cookies*` 这类 Cookie 数据库相关文件，不复制密码库或完整 profile。不要默认使用该参数，也不要告诉用户首次启动会自动导入主 Chrome 登录态。

等用户确认后，重新运行 `--check` 验证。

### 第 3 步：运行抓取

```bash
# 基础搜索
python3 "$SCRIPT_PATH" --keyword "关键词" --city 城市 --pages 3 --output ~/.boss-zhipin-scraper/job-result/jobs.json

# 带 CSV 输出
python3 "$SCRIPT_PATH" --keyword "关键词" --city 城市 --pages 3 --format csv --output ~/.boss-zhipin-scraper/job-result/jobs.json

# 带详情 + 分析报告
python3 "$SCRIPT_PATH" --keyword "关键词" --city 城市 --pages 3 --detail --max-details 8 --analysis --format csv --output ~/.boss-zhipin-scraper/job-result/jobs.json

# 抓取后摘要 + 求职材料优化提示词（默认读取最新抓取结果）
python3 "$SUMMARY_PATH" --top 15

# 真实浏览器/API smoke test（不写结果文件）
python3 "$SCRIPT_PATH" --smoke-test --cdp-port 45222

# 合并多次抓取（去重）
python3 "$SCRIPT_PATH" --keyword "关键词" --city 北京 --pages 3 --merge ~/.boss-zhipin-scraper/job-result/jobs.json --output ~/.boss-zhipin-scraper/job-result/jobs_merged.json
```

默认输出到 `~/.boss-zhipin-scraper/job-result/` 目录，`--format csv` 会给列表和详情都额外生成 `.csv` 文件。`--smoke-test` 只验证真实 Chrome/CDP 能否拿到 API 明文薪资，不写结果文件。

抓取结束后专用 Chrome 不会自动关闭（默认保留登录态，方便连跑多条）。确认不再使用时收尾：

```bash
# 关闭 BOSS 专用 Chrome（只关隔离 profile，不碰主 Chrome）
python3 "$SCRIPT_PATH" --stop-chrome --cdp-port 45222

# 或：让本次抓取正常结束就自动关闭
python3 "$SCRIPT_PATH" --keyword "关键词" --city 城市 --pages 3 --close-chrome
```

`--stop-chrome` 按 `--user-data-dir` 精准匹配，绝不按端口/进程名 kill，因此不会误伤用户主 Chrome。`--close-chrome` 默认关闭，且只在抓取成功路径触发，异常/登录失败不关闭以保留登录态。

摘要脚本只读取 `boss_jobs_*.json` 和 `boss_details_*.json`，不读取本地简历文件，不引入 PDF 依赖，也不给个人与岗位做分数判断。需要指定文件时使用：

```bash
python3 "$SUMMARY_PATH" \
  --input ~/.boss-zhipin-scraper/job-result/boss_jobs_20260625_1200.json \
  --details ~/.boss-zhipin-scraper/job-result/boss_details_20260625_1200.json \
  --top 15
```

## 参数速查

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--keyword` | AI Agent | 搜索关键词；支持逗号分隔多关键词（依次抓取并按 job_id 自动合并，如 "Java 后端,Java 风控"） |
| `--city` | 上海 | 城市名（中文）或 9 位代码；没传时默认上海，无法识别的城市名会报错退出 |
| `--pages` | 3 | 抓取页数（上限 10，每页 30 条） |
| `--pages-parallel N` | 3 | 并行抓页数（多 tab 同发搜索 XHR，列表阶段 ~50s→~6s；0/1=关闭回退串行） |
| `--foreground-capture` | 关闭 | 列表/登录探测/详情 DOM 改用前台 Target（默认后台；Chrome 在 Linux/Xvfb 下后台捕获不到搜索响应时用，上游 #67/#68） |
| `--list-mode xhr/passive` | xhr | 列表通道：xhr=注入 XHR（默认，快）；passive=Network 域被动捕获页面自身响应（零注入请求，消除 code 37，上游 #55；与 --pages-parallel 互斥） |
| `--max-jobs N` | 全部 | 列表条数上限，抓够即停 |
| `--industry` | - | 行业代码（见下方筛选参数） |
| `--input FILE` | - | 从已有 JSON 读取（跳过列表抓取；此时无 securityId，详情退化为 DOM 渲染） |
| `--list-cities [关键词]` | - | 打印支持的城市列表（可选关键词过滤） |
| `--output` | ~/.boss-zhipin-scraper/job-result/... | 列表输出路径 |
| `--detail-output` | ~/.boss-zhipin-scraper/job-result/... | 详情输出路径 |
| `--format` | json | 输出格式: json / csv；csv 同时导出列表和详情 CSV |
| `--detail` | 开启（默认） | 抓取详情（默认走详情 API 通道） |
| `--no-detail` | - | 不抓取详情页（关闭默认行为） |
| `--max-details` | 全部 | 详情页数量上限 |
| `--analysis` | 关闭 | 输出分析报告 |
| `--concurrency N` | 1 | 详情抓取并发度（2-3 推荐；越高成功率越低，含全局限速与错误率自适应降速） |
| `--detail-channel` | auto | 详情通道：auto/api/dom/panel（panel=复用搜索页点卡片读右面板 JD，零新增请求、串行；上游 #84） |
| `--max-concurrent N` | 1 | 并发抓取任务数上限（规格硬防线；仅指令显式开启，任一任务风控即全停） |
| `--keep-without-jd` | 关闭 | 保留无 JD 岗位（默认口径一：详情抓完后剔除无 JD 岗位） |
| `--retry-job JOB_ID` | - | 强制重试指定 job_id（可重复指定） |
| `--allow-dom-fallback` | 关闭 | API 无数据时允许降级 DOM 提取；默认关闭，薪资可能不可信 |
| `--filter-inactive` | 关闭 | 按 HR 活跃度剔除长期未活跃岗位（仅匹配「周/月/年前活跃」，不误杀本周/本月活跃） |
| `--merge FILE` | - | 合并已有 JSON（按 job_id 去重） |
| `--db [PATH]` | 关闭 | 启用 SQLite 增量存储（WAL）+ 跨 run 详情断点续抓；不带值用 `~/.boss-zhipin-scraper/boss.db`（仓库外，不进 git）；JSON/CSV 导出照旧 |
| `--cdp-port` | 45222 | CDP 端口 |
| `--setup-chrome` | 关闭 | 一键启动 Chrome CDP（持久隔离 profile） |
| `--copy-login-state` | 关闭 | 手动导入主 Chrome 的 Local State + Cookie 相关文件到隔离 profile；默认、首次启动、重复启动都不复制 |
| `--reset-chrome-profile` | 关闭 | 重建 BOSS 专用 profile，会清除此专用浏览器登录态 |
| `--no-wait-login` | 关闭 | `--setup-chrome` 启动后不等待 BOSS 登录完成 |
| `--login-timeout` | 300 | `--setup-chrome` 等待登录完成的秒数 |
| `--stop-chrome` | 关闭 | 关闭 BOSS 专用 CDP Chrome（按隔离 profile 精准匹配，不碰主 Chrome） |
| `--close-chrome` | 关闭 | 抓取正常结束后自动关闭专用 Chrome（默认不关；异常退出不触发，保留登录态） |
| `--check` | 关闭 | 环境检查 |
| `--smoke-test` | 关闭 | 真实 Chrome/CDP 搜索 API smoke test，不写结果文件 |
| `--status` | 关闭 | 状态总览（运行态+缓存态，接管用；不发请求） |
| `--verify` | 关闭 | 校验结果文件完整性（只校验不抓取） |
| `--list-results` | 关闭 | 列出结果目录中的历史抓取结果文件 |
| `--archive [KEEP]` | 1 | 归档历史结果：每类保留最新 KEEP 个，其余移入 archive/ |
| `--batch CONFIG.json` | - | 批量抓取（默认列表+详情；任务级 `detail:false` 或 `--no-detail` 可仅列表） |
| `--version` | - | 查看版本号 |
| `--debug-screenshots` | 关闭 | 失败时把页面截图存到 `~/.boss-zhipin-scraper/debug/`（排障用） |

### 筛选参数

| 参数 | 值 |
|------|-----|
| `--scale` | 301=0-20人 302=20-99 303=100-499 304=500-999 305=1000-9999 306=10000+ |
| `--salary` | 402=3K以下 403=3-5K 404=5-10K 405=10-20K 406=20-50K 407=50K+ |
| `--experience` | 108=在校生 102=应届生 101=经验不限 103=1年内 104=1-3年 105=3-5年 106=5-10年 107=10年+ |
| `--degree` | 209=初中及以下 208=中专/中技 206=高中 202=大专 203=本科 204=硕士 205=博士 |

### 城市代码

全国 100010000 | 北京 101010100 | 上海 101020100 | 广州 101280100 | 深圳 101280600 | 杭州 101210100 | 成都 101270100 | 武汉 101200100 | 南京 101190100 | 厦门 101230200

## 输出格式

### JSON

```json
{
  "format_version": 2,
  "keyword": "AI Agent",
  "city": "上海",
  "page_count": 3,
  "total": 90,
  "job_count": 89,
  "exhausted": true,
  "warnings": [],
  "jd_coverage": {"with_jd": 89, "total_before": 90, "dropped_no_jd": 1},
  "dropped_no_jd": ["d86a000c3d5d50e3"],
  "jobs": [
    {
      "job_id": "c4420e8bce3a6e25",
      "title": "AI Agent工程师",
      "salary": "30-60K·15薪",
      "salary_source": "api",
      "location": "上海·闵行区·虹桥",
      "tags": "5-10年 | 本科",
      "experience": "5-10年",
      "education": "本科",
      "boss_name": "SHEIN",
      "company_name": "SHEIN",
      "boss_title": "招聘者",
      "boss_active_status": "刚刚活跃",
      "company_scale": "10000人以上",
      "company_stage": "D轮及以上",
      "company_industry": "电子商务",
      "skills": ["Java", "Spring", "AI"],
      "job_link": "https://www.zhipin.com/job_detail/xxx.html",
      "company_link": "https://www.zhipin.com/gongsi/xxx.html",
      "welfare": "节日福利 | 零食下午茶 | 定期体检",
      "jd": "……（详情抓取后并入；默认口径一会剔除无 jd 岗位）"
    }
  ]
}
```

### CSV

`--format csv` 时自动在同目录生成 `.csv` 文件：列表 CSV 跟随 `--output`，详情 CSV 跟随 `--detail-output` 或默认详情 JSON 路径。CSV 使用 UTF-8 BOM 编码，Excel 直接打开无乱码。

## 工作原理

1. 通过 Chrome DevTools Protocol (CDP) 连接到已打开的 Chrome 浏览器
2. **列表**：在 BOSS直聘页面内注入 JS，用同步 XHR 调用 `/wapi/zpgeek/search/joblist.json` API，返回明文 `salaryDesc`（如 `30-60K·15薪`），绕过前端字体反爬
3. **详情（默认 API 通道）**：调用 `/wapi/zpgeek/job/detail.json`（每岗 1 次轻量 XHR 取完整 JD）；`securityId` 由列表阶段同进程内存传递、**不落导出文件**（红线）；每 tab 约 4 次配额，程序按预算自动轮换 tab；`--input` 老文件无 `securityId` 时自动回退 DOM 渲染
4. 默认禁用 DOM fallback，避免把字体反爬后的薪资写入结果；只有显式 `--allow-dom-fallback` 才降级
5. 每页 30 条，每页抓完立即写入文件，异常退出不丢数据
6. 按 `job_id`（job_link 的 MD5 哈希前 16 位）去重
7. 默认口径一：详情抓完后把 `jd` 并入导出并剔除无 JD 岗位（`--keep-without-jd` 可保留）

## 数据安全策略

`--setup-chrome` 默认使用持久隔离 profile，不软链接、不读取、不复制主 Chrome profile。首次启动和后续重复启动都只会创建或复用 `~/.boss-zhipin-scraper/chrome-profile`，不会清空其中的 BOSS 登录态。setup 会等待登录完成，并用多组关键词/城市 probe，要求搜索接口返回明文薪资；如果一直拿不到 `salaryDesc`，不要继续抓取并把 DOM 薪资当成可信数据。这样 CDP 只暴露 BOSS 专用浏览器里的数据，不影响用户主 Chrome、Gmail、GitHub 等账号。

`--input ... --analysis --no-detail` 会优先加载 `--detail-output`，其次加载与输入列表同目录、同时间戳的 `boss_details_*.json`，最后查找 `~/.boss-zhipin-scraper/job-result` 下最新详情文件。

需要清空 BOSS 专用浏览器登录态时使用：

```bash
python3 "$SCRIPT_PATH" --setup-chrome --reset-chrome-profile --cdp-port 45222
```

## 常见问题

1. **--check CDP 不通** → 运行 `--setup-chrome`
2. **--check 未登录** → 在专用 Chrome 中访问 zhipin.com 登录，或重新运行 `--setup-chrome`
3. **薪资空白** → 通常是未登录、登录态失效或接口未返回 `salaryDesc`；先重新登录，不要优先做字体解密或 DOM fallback
4. **抓取中断** → 重新运行即可，增量写入 + 自动去重
5. **端口占用** → `--cdp-port 45223` 换端口
6. **Chrome 启动失败** → `--cdp-port 45223` 换端口，或用 `--reset-chrome-profile` 重建专用 profile

## 注意事项

- 仅用于个人求职研究
- 单次最多 10 页（300 条），防封号
- 翻页间隔 12-22 秒随机延迟（并发 >1 时自动拉长到 20-30 秒），3 页约 1 分钟
- 详情页每条 10-25 秒，10 条约 3-5 分钟
- **并发守则（ai-pm-job-intel 规格 §3.6）**：默认并发上限 1（互斥锁硬防线）；`--max-concurrent N`（2-3）仅指令显式开启；任一任务遇风控 → 全停挂起等人工，确认后 `--reset-lock` 重开
- BOSS直聘可能更新 API 路径，失效时需更新脚本中 `API_JOB_LIST_PATH` 常量

## 安装本 Skill

本 Skill 需手动安装到 Hermes skills 目录（`hermes skills install` 因网络问题可能失败）：

```bash
# 推荐：curl 一键安装
mkdir -p ~/.hermes/skills/data-science/boss-zhipin-scraper/scripts && \
curl -sL https://raw.githubusercontent.com/eatmoreduck/boss-zhipin-scraper/master/SKILL.md \
  -o ~/.hermes/skills/data-science/boss-zhipin-scraper/SKILL.md && \
curl -sL https://raw.githubusercontent.com/eatmoreduck/boss-zhipin-scraper/master/scripts/boss_cdp_raw.py \
  -o ~/.hermes/skills/data-science/boss-zhipin-scraper/scripts/boss_cdp_raw.py && \
curl -sL https://raw.githubusercontent.com/eatmoreduck/boss-zhipin-scraper/master/scripts/job_summary.py \
  -o ~/.hermes/skills/data-science/boss-zhipin-scraper/scripts/job_summary.py && \
mkdir -p ~/.hermes/skills/data-science/boss-zhipin-scraper/data && \
curl -sL https://raw.githubusercontent.com/eatmoreduck/boss-zhipin-scraper/master/data/city_codes.json \
  -o ~/.hermes/skills/data-science/boss-zhipin-scraper/data/city_codes.json
```

或克隆后手动复制：

```bash
git clone https://github.com/eatmoreduck/boss-zhipin-scraper.git
mkdir -p ~/.hermes/skills/data-science/boss-zhipin-scraper/scripts
cp boss-zhipin-scraper/SKILL.md ~/.hermes/skills/data-science/boss-zhipin-scraper/
cp boss-zhipin-scraper/scripts/boss_cdp_raw.py ~/.hermes/skills/data-science/boss-zhipin-scraper/scripts/
cp boss-zhipin-scraper/scripts/job_summary.py ~/.hermes/skills/data-science/boss-zhipin-scraper/scripts/
mkdir -p ~/.hermes/skills/data-science/boss-zhipin-scraper/data
cp boss-zhipin-scraper/data/city_codes.json ~/.hermes/skills/data-science/boss-zhipin-scraper/data/
```
