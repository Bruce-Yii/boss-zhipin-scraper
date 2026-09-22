# 贡献指南

感谢你对 boss-zhipin-scraper 的兴趣！无论是提 Issue、修 Bug 还是加功能，都欢迎。

## 行为准则

请保持友善、尊重。技术讨论对事不对人，不接受任何人身攻击或骚扰言论。

## 在贡献之前

- **先开 Issue 再写代码**：修 Bug 或加新功能前，请先在 [Issues](../../issues) 里搜索是否已有人提过；没有的话新开一个，简要说明你打算做什么，避免和别人重复劳动或方向跑偏。
- **一个 PR 只做一件事**：混合多个改动的 PR 很难 review，请拆开。

## 开发环境

```bash
git clone https://github.com/eatmoreduck/boss-zhipin-scraper.git
cd boss-zhipin-scraper
pip install -r requirements.txt          # 或 uv sync
python3 -m unittest tests.test_chrome_setup   # 跑测试，确保全绿
```

要求 Python 3.12+，运行依赖 `requests`、`websocket-client`（抓取）与 `pandas`、`matplotlib`（`boss-summary` 摘要/图表）。

## 代码规范

- **风格**：遵循 [PEP 8](https://peps.python.org/pep-0008/)，用 4 空格缩进、UTF-8、LF 换行。
- **异常处理**：不要用 bare `except:`，必须捕获具体异常类型（`requests.ConnectionError`、`json.JSONDecodeError` 等），项目现有的代码就是这么做的，请保持一致。
- **模块边界**：核心编排、CDP 会话与 CLI 仍在 `scripts/boss_cdp_raw.py`。已抽出的**稳定纯逻辑**模块为 `scripts/ratelimit.py`（令牌桶/自适应限速）与 `scripts/export_contract.py`（契约/脱敏/口径一/原子写）；新增代码优先放主文件，确有必要再按同一标准（纯逻辑、无 CDP 依赖、由主文件 re-export）增模块，不要随手建文件。
- **注释**：复杂逻辑要写注释（参考 `human_scroll` 的做法）；公开函数补 docstring。

## 测试要求

- 修了 Bug 或加功能，**必须补测试**。测试在 `tests/test_chrome_setup.py`，用标准库 `unittest`，通过 mock 掉 `requests`/`websocket`，**不需要真实 Chrome 或网络**。
- 提 PR 前本地先跑通：

  ```bash
  python3 -m unittest tests.test_chrome_setup
  ```

- 涉及版本号改动，会触发 `VersionConsistencyTests`，确保 `scripts/boss_cdp_raw.py`、`pyproject.toml`、`SKILL.md`、`README.md` 四处版本一致。

## 提交信息（Commit Message）

使用 [Conventional Commits](https://www.conventionalcommits.org/) 格式，参考现有提交历史：

```
<type>: <简短描述，中文或英文均可>

feat: 新功能        例: feat: 详情页加过程日志
fix: 修 Bug         例: fix bug salary garbled characters
optimize: 优化      例: optimize(risk-control): 优化详情页进入方式
docs: 文档          例: docs: 更新 README 参数说明
refactor: 重构      例: refactor: API 路径提取为常量
test: 测试          例: test: 补城市码去重校验
chore: 杂项         例: chore: 升级依赖
```

## Pull Request 流程

1. Fork 仓库，从 `master` 拉一个新分支（`git checkout -b fix/city-code-typo`）。
2. 改代码 → 补测试 → 本地跑通。
3. 如果改了用户可见行为，更新 `README.md`；如果是有意义的变更，在 `CHANGELOG.md` 顶部加一条。
4. 提交 PR，描述里写清楚：改了什么、为什么改、怎么测试的。
5. 等待 review，有反馈就改，保持同一个 PR（不要关掉重开）。

## 关于合规

本项目通过复用用户**本人已登录的浏览器**抓取公开可见的职位数据，用于个人求职分析。提交代码时请不要加入任何大规模、无节制请求、或绕过平台安全校验的逻辑——这类改动不会被接受。请遵守目标网站的条款，对自己使用本工具的行为负责。

### 能力声明（做什么 / 不做什么）

- **做**：用你本人的登录态、低频率抓取公开职位列表与 JD，导出 JSON/CSV 供个人求职分析；风控/验证码命中即停（可选告警）。
- **不做**：自动投递/打招呼、自动解验证码、伪造/自研 token 或环境、多账号并发、用代理/IP 轮换规避风控、把数据用于转售或大规模采集。

### 批量上限（硬性）

- 页数 ≤ `MAX_PAGES`（10）；单次全局请求预算 ≤ `MAX_API_REQUESTS`（500）；详情并发默认 1，`--concurrency` 仅由指令显式放开，节律走 `BurstThrottle`（**不得调高**）。
- 风控/验证码命中 → **立即终止，不重试、不绕过、不换 IP/账号**；详情命中即全停并进入冷却。

### 个人信息

- 只采集**公开职位信息**（岗位/JD/公司/薪资等）；招聘者姓名、联系方式等个人身份信息不采集、不落盘、不导出。

### 拒收的 PR（明文红线）

以下改动**一律不接受**，直接关闭：

1. 绕过或削弱平台安全机制（验证码、风控、登录校验等）；
2. 提高默认请求频率/并发，或去掉限速与预算帽；
3. 绕过人工确认（自动解验证码、静默重试风控等）；
4. 采集或落盘个人身份信息、凭据（cookie/token 等）；
5. 引入大规模、无节制抓取能力；
6. 引入代理池 / IP 轮换规避风控（实测代理会触发 `code 7/37`；IP 被封＝该账号全部会话作废、换 IP 无效只能重新登录——本项目**坚持不用代理**）。

### 审计

关键风险事件（风控/验证码全停、登录失效、熔断冷却）会写入本地审计日志 `~/.boss-zhipin-scraper/risk_events.jsonl`（JSONL、仓库外、不含凭据），便于事后复盘与对外可解释。

## 有问题？

- Bug / 功能建议 → [Issues](../../issues)
- 不确定怎么改 → 先开 Issue 讨论

再次感谢你的贡献！
