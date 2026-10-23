# BOSS Zhipin Scraper · Job Crawler v2.23.0 (Chrome CDP / Plaintext Salary)

> 🌐 中文文档：[README.md](./README.md)

![Python](https://img.shields.io/badge/python-3.12+-blue.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)
![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux-lightgrey.svg)
![Version](https://img.shields.io/badge/version-2.23.0-orange.svg)

A lightweight **BOSS Zhipin scraper / crawler** (a.k.a. spider) for job listings on [zhipin.com](https://www.zhipin.com). Instead of driving a heavy Selenium/Playwright browser, it connects to your **already-logged-in Chrome** via the Chrome DevTools Protocol (CDP), reuses the real session, and calls the in-page search API directly — bypassing the front-end font-based anti-scraping so you get the **plaintext salary** in every record. Output goes to JSON / CSV, plus an aggregated salary/skill analysis and a copy-paste prompt for polishing your job-application materials. Also ships as a Hermes Agent Skill.

> 📌 **In one sentence**: no Selenium/Playwright — connect to your logged-in Chrome over CDP, hit the search API with the real session, get JSON/CSV with plaintext salaries, plus salary-distribution, skill-frequency stats and a résumé-optimization prompt.

---

## ⚠️ Disclaimer

This project is for **learning and technical research purposes only**. It is intended to explore Chrome DevTools Protocol, front-end anti-scraping mechanisms, and data-collection techniques. Do **not** use it for any purpose that violates the [BOSS Zhipin Terms of Service](https://www.zhipin.com/about/protocol.html) or applicable laws and regulations, including commercial resale, malicious scraping, or any activity that imposes undue load on the target site. Users are solely responsible for the consequences of using this project; the author is not liable for any misuse.

---

## 🚀 30-Second Quick Start

```bash
# 1. Clone + install deps
git clone https://github.com/eatmoreduck/boss-zhipin-scraper.git
cd boss-zhipin-scraper
pip install -r requirements.txt          # or: uv sync

# 2. Launch an isolated Chrome and log in (only once; session persists)
python3 scripts/boss_cdp_raw.py --setup-chrome

# 3. Scrape + analyze
python3 scripts/boss_cdp_raw.py --keyword "AI Agent" --city 上海 --pages 3 --analysis

# Cities nationwide are supported (incl. tier-3/4/5), e.g.:
python3 scripts/boss_cdp_raw.py --keyword "前端" --city 赣州 --pages 3
# List supported cities: --list-cities [keyword]
python3 scripts/boss_cdp_raw.py --list-cities 江

# 4. Generate an aggregated summary + prompt after scraping (reads the latest result)
python3 scripts/job_summary.py
```

Right after scraping you get: salary ranges, experience requirements, top skill keywords, and a job-application optimization prompt. The prompt is based solely on the scraped job data — it never reads your local résumé file and never scores personal-job match.

## ✨ Features

- Plaintext salary (API mode, bypasses font-based obfuscation)
- Boss activity status as a separate field (`boss_active_status`): list maps `bossOnline`→"在线"; detail can provide finer labels like "刚刚活跃"
- Dual JSON / CSV output
- Detail-page JD scraping + skill analysis
- Aggregated summary + copy-paste prompt after scraping
- Incremental writes (no data loss on crash)
- One-shot environment check + persistent isolated Chrome CDP profile
- Multi-dimension filters (scale, funding, salary, experience, degree, industry)
- macOS + Linux support (a Windows code path is reserved but untested — not guaranteed to work)

<details>
<summary>🔍 Why not a Selenium / Playwright crawler?</summary>

- Selenium/Playwright spins up a full instrumented browser — it's heavy, has an obvious fingerprint, and is easily flagged by BOSS Zhipin's risk-control / CAPTCHA.
- This tool connects to your own already-logged-in Chrome (via CDP), reusing a real fingerprint and session, and calls the same legitimate search API the page uses. The `salaryDesc` it returns is already plaintext — no need to parse font-obfuscated DOM salaries.
- The result is more stable than traditional DOM-scraping crawlers and harder to flag as automated traffic.

</details>

## Installation

### Option 1: Clone then install locally (recommended)

Because `hermes skills install` may not reach GitHub directly in some environments, clone the repo first and install locally:

```bash
# 1. Clone the repo
git clone https://github.com/eatmoreduck/boss-zhipin-scraper.git
cd boss-zhipin-scraper

# 2. Copy into the Hermes skills directory
mkdir -p ~/.hermes/skills/data-science/boss-zhipin-scraper/scripts
cp SKILL.md ~/.hermes/skills/data-science/boss-zhipin-scraper/
cp scripts/boss_cdp_raw.py ~/.hermes/skills/data-science/boss-zhipin-scraper/scripts/
cp scripts/job_summary.py ~/.hermes/skills/data-science/boss-zhipin-scraper/scripts/
mkdir -p ~/.hermes/skills/data-science/boss-zhipin-scraper/data
cp data/city_codes.json ~/.hermes/skills/data-science/boss-zhipin-scraper/data/
```

### Option 2: One-line curl install

No need to clone the whole repo — download just the files you need:

```bash
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

### Option 3: `hermes skills install` (requires direct GitHub access)

```bash
hermes skills install https://raw.githubusercontent.com/eatmoreduck/boss-zhipin-scraper/master/SKILL.md --category data-science
```

> Note: this depends on the hermes process being able to reach GitHub directly. If you hit a timeout or connection failure, use Option 1 or 2.

### Option 4: One-line install via skills.sh (Claude Code and other Agent Skills-compatible agents)

```bash
npx skills add eatmoreduck/boss-zhipin-scraper
```

The skill is listed on [skills.sh](https://skills.sh). Any agent that supports the Agent Skills format (Claude Code, Codex, Gemini CLI, Cursor, etc.) can install it with this one command — SKILL.md, the scripts, and the city code table ship with the skill. Pick the target agent when the CLI asks.

### Verify the installation

```bash
# Check that the files exist
ls ~/.hermes/skills/data-science/boss-zhipin-scraper/SKILL.md
ls ~/.hermes/skills/data-science/boss-zhipin-scraper/scripts/boss_cdp_raw.py
ls ~/.hermes/skills/data-science/boss-zhipin-scraper/scripts/job_summary.py
ls ~/.hermes/skills/data-science/boss-zhipin-scraper/data/city_codes.json
```

After installing, just say in a Hermes conversation: "Search BOSS Zhipin for AI Agent jobs in Shanghai."

## Use as a CLI tool

You don't have to install it as a Skill — use it as a plain CLI:

```bash
# 1. Clone + install deps
git clone https://github.com/eatmoreduck/boss-zhipin-scraper.git
cd boss-zhipin-scraper
pip install -r requirements.txt

# 2. Start Chrome CDP
python3 scripts/boss_cdp_raw.py --setup-chrome
# First run won't copy your main Chrome session; log in to zhipin.com in the dedicated BOSS browser that pops up
# setup waits for login to finish and confirms the API returns plaintext salaries

# 3. Check the environment
python3 scripts/boss_cdp_raw.py --check

# Optional: real browser/API smoke test (writes no result files)
python3 scripts/boss_cdp_raw.py --smoke-test

# 4. Scrape
python3 scripts/boss_cdp_raw.py --keyword "AI Agent" --city 上海 --pages 3 --format csv --analysis

# 5. Summary + prompt after scraping
python3 scripts/job_summary.py --top 15

# 6. Rebuild a deliverable (lists + details + old export through the
# production merge path, no re-scraping)
python3 scripts/build_deliverable.py \
  --lists boss_list_A.json boss_list_B.json \
  --details boss_details_A.json boss_details_B.json \
  --old boss_jobs_old.json --target boss_jobs_new.json \
  --keyword "产品实习生,AI产品实习生" --city 杭州 \
  --target-roots "产品实习,AI产品,FDE,AIGC,大模型" \
  --scope "scope note goes here"
```

## Parameters

| Parameter | Description |
|-----------|-------------|
| `--keyword` | Search keyword (default "AI Agent"); comma-separated multi-keyword supported (scraped in turn and merged by job_id, e.g. `"Java 后端,Java 风控"`) |
| `--recruit-type` | Keep only these recruitment types (`social` / `intern` / `campus`), comma-separated; filtered **at the list stage** → those jobs are never fetched, so no detail-page budget is spent |
| `--industry-allow` | Keep only these industries (comma-separated). **No industry filter by default** — measured across 933 records / 90 industries, industry is *not* a relevance signal (every one of 4 restaurant-industry records was a real product-manager job) |
| `--industry-block` | Drop these industries (comma-separated, e.g. `房地产中介/租赁`). Every dropped job is printed with its reason — nothing is dropped silently |
| `--only-relevant-title` | Keep only jobs whose title is a product-manager / AI direction (list-stage filter, dropped jobs are never fetched → no budget spent) |
| `--city` | City (Chinese name or 9-digit code, default Shanghai). **Supports cities nationwide** (300+, incl. tier-3/4/5); city codes auto-sync from BOSS at runtime. See [`data/city_codes.json`](data/city_codes.json), or run `--list-cities`. An unrecognized city name now exits with an error instead of silently producing zero results |
| `--list-cities [keyword]` | Print the supported city list, optional keyword filter, e.g. `--list-cities 江` |
| `--pages` | Number of pages (max 10) |
| `--max-jobs` | List size cap; stops as soon as reached (BOSS returns 30 per page, so actual count may slightly exceed the cap; unset = scrape all `--pages`) |
| `--format` | json / csv; csv also exports list and detail CSVs |
| `--detail` | Scrape detail-page JD (on by default) |
| `--no-detail` | Do not scrape detail pages |
| `--concurrency` | Detail scrape concurrency (default 1 = serial). **Concurrency only decides how many requests are in flight, not the request rate** — the rate is controlled by `--detail-rate`. The API path uses a shared tab pool and is globally serialized by `BurstThrottle(center=2.25)` (target about 0.44 req/s); the DOM path uses the adaptive limiter; panel stays serial |
| `--detail-rate RPS` | Detail page request rate cap (req/s, **absolute — not scaled by concurrency**; default 1.0). The platform caps daily detail-page visits, so going faster yields no extra records and only escalates the risk tier (09-23 at 1 req/s hit a solvable slider captcha; 10-21 at 2.6 req/s hit a timed account restriction) |
| Known-bad ledger | Pages whose extraction failed are fingerprinted and persisted (outside the repo, `~/.boss-zhipin-scraper/known_bad_jobs.json`); a hit is **skipped without a request and without consuming budget**. Each exported record carries `post_type` (direct / agency / anonymous), `active_state` (active / inactive / **unknown**) and `dup_group` (same-content duplicate marker); the export meta carries `post_type_counts` / `duplicate_groups` / `title_groups` / `invariants` (self-check) |
| `--refresh-links EXPORT` | Refresh the export's `job_link` values with freshly pulled list links, matched by job_id (the URL carries a securityId segment and cannot be rebuilt from job_id; list requests only — they do not consume the detail budget), and report how many job_ids are no longer in the list |
| `--daily-detail-budget N` | Daily detail-page budget (request count, **cumulative across runs**, default 700; 0 = unlimited). Counted in `~/.boss-zhipin-scraper/daily_budget.json` (outside the repo); `BOSS_DAILY_BUDGET_PATH` overrides the path. A job already fetched today is **skipped without consuming budget** (ledger), so the daily allowance always buys distinct jobs |
| `--keep-junk` | Keep sales/tech/noise jobs (default is **purge on export**: sales, tech/design and noise jobs are removed; only AI non-tech roles kept; FDE exempt; "product manager with a tech prefix" does not count as a tech job; JD-suspected sales are only warned, not removed; title junk is filtered before detail fetching so it never consumes budget. The removed list goes to `dropped_junk` for audit) |
| `--detail-channel` | Detail channel: `dom` (default; renders the detail page as a document navigation — the only shape with production evidence: 2026-09-23 produced 1005 records/day with it and only hit a manually solvable slider captcha) / `html` (**retired**: fetch the server-rendered detail HTML directly. Measured 2026-10-22: the platform intermittently serves the JD of a **different job** to fetch requests, and the textContent-based extraction drags `display:none` decoy text into the JD; zero throughput benefit under the absolute rate limit) / `auto` (old default: API when securityId is present, else try the encryptJobId API first and fall back to DOM) / `api`·`encrypt` (detail API channel, **retired**: the platform rate-limits that endpoint and escalates it to short-lived account bans — see tangzhiyao/boss-show-time#42) / `panel` (reuse the search page, click a card and read the right panel's JD — zero extra requests, serial; upstream #84 idea). The export meta records the real successful channel `api`/`encrypt`/`dom`/`panel`/`mixed` plus `detail_channel_counts` |
| `--retry-job JOB_ID` | Force-retry a specific detail (repeatable; ignores the pending retry limit, also retries IDs not yet recorded) |
| `--filter-inactive` | Drop long-inactive (zombie) jobs by HR activity (matches only "active N weeks/months/years ago"; does not drop this-week/this-month activity); off by default |
| `--foreground-capture` | Use a foreground Target for list / login probe / detail DOM; for environments where Chrome on Linux/Xvfb cannot capture search responses with a background Target (stays background by default) |
| `--list-mode xhr / passive` | List channel: `xhr` = inject XHR to the wapi (default, fast, pairs with `--pages-parallel`); `passive` = passively capture the page's own joblist responses via the Network domain (zero injected requests, removes the injected-XHR trigger for code 37, upstream #55; mutually exclusive with `--pages-parallel`, falls back to serial) |
| `--analysis` | Analysis report |
| `--merge FILE` | Merge existing data (deduped by job_id) |
| `--input FILE` | Continue from an existing list JSON; prefer an unexpired securityId sidecar, try the encrypt API first in auto mode, then fall back to DOM by policy; the input list is not rewritten |
| `--db [PATH]` | Enable the SQLite incremental store (WAL) + cross-run detail resume; without a value it uses the default DB `~/.boss-zhipin-scraper/boss.db` (outside the repo, never committed); JSON/CSV exports are unchanged |
| `--allow-dom-fallback` | Allow DOM extraction fallback when the API has no data; off by default, salaries may be unreliable |
| `--check` | Environment check (CDP + deps + login state) |
| `--status` | Status overview (runtime + cache, for takeover; **no requests**; login still needs `--check`) |
| `--verify` | Verify scraped result files (list/detail parseability, required fields, JD completeness, coverage; verification only, no scraping, no Chrome needed) |
| `--list-results` | List historical result files in the result dir (kind + time + size) |
| `--archive [KEEP]` | Archive historical results: keep the newest KEEP files per kind (default 1), move the rest into `archive/`; active pending files are never archived |
| `--batch CONFIG.json` | Batch scraping (**list + details by default**): config is a JSON array, one task per element (keyword required; city/pages/sleep/detail/filter fields optional), runs tasks sequentially with anti-risk gaps; `--no-detail` or per-task `detail:false` for list only |
| `--smoke-test` | Run one real Chrome/CDP BOSS search API smoke test, writes no result files |
| `--setup-chrome` | One-shot launch of Chrome CDP (persistent isolated profile) |
| `--copy-login-state` | Manually import the main Chrome's Local State + cookie-related files into the isolated profile (never copied by default, on first run, or on repeated runs) |
| `--reset-chrome-profile` | Rebuild the dedicated BOSS Chrome profile; clears the login state inside this dedicated browser |
| `--no-wait-login` | With `--setup-chrome`, do not wait for login to finish |
| `--login-timeout` | Seconds to wait for login under `--setup-chrome` (default 300) |
| `--stop-chrome` | Close the dedicated BOSS CDP Chrome (matched precisely by the isolated profile; never touches your main Chrome) |
| `--close-chrome` | Auto-close the dedicated Chrome after a scrape finishes normally (off by default; not triggered on errors, so the login state is kept) |
| `-v, --verbose` | Print DEBUG-level logs (stackable: `-vv`; CDP messages, probe details, etc.) |
| `-q, --quiet` | Quiet mode: logs drop to WARNING (result/EXPORT lines still go to stdout) |
| `--debug-screenshots` | Save a page screenshot to `~/.boss-zhipin-scraper/debug/` on failure (for debugging; off by default) |
| `--output` | List output path (default `~/.boss-zhipin-scraper/job-result/`) |
| `--detail-output` | Detail output path (default `~/.boss-zhipin-scraper/job-result/`) |
| `--cdp-port` | CDP port (default 45222 — fixed high port, bypasses the 9222/9223/9229 ports scanned by BOSS security JS) |
| `--scale/--salary/--experience/--degree` | Filters |

### Exit codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Runtime error (login failure / risk block / unexpected exception — clean message, no traceback) |
| 2 | CLI misuse (unknown argument / invalid value / conflicting commands, argparse default) |
| 130 | Interrupted by user Ctrl+C (128+signal convention) |

### Alert Push

On anomalies, alerts are pushed via a Worker endpoint (downstream channels such as Feishu cards are configured by the endpoint side): risk/captcha full-stop (`EXPORT_FAIL reason=risk_blocked`), **login failure** (`EXPORT_FAIL reason=login_failed`), detail captcha full-stop (`warnings: detail_risk_blocked`).

Key risk events (risk/captcha full-stop, login failure, circuit-breaker cooldown) are also written to a local audit log `~/.boss-zhipin-scraper/risk_events.jsonl` (JSONL, outside the repo, no credentials; `BOSS_AUDIT_PATH` overrides the path) for post-mortems and external explainability.

- Configure in the project root `.env` (gitignored, not committed): `ALERT_WEBHOOK_URL=<endpoint URL>`, `ALERT_WEBHOOK_TOKEN=<Bearer token>`
- Silent skip if unconfigured or on network failure (logged only); never blocks the scrape flow

## Run Lifecycle and Coordination

- Normal CLI and smoke-test runs acquire one run lock before entering a live run and hold it from cooldown/login checks through list, detail, merge, analysis, and SQLite finalization. A held lock produces a non-zero exit.
- Cooldown is checked before any live request. A list/detail risk signal starts cooldown; Chrome is not auto-restarted while cooling down. Offline commands such as `--status`, `--verify`, and `--list-results` do not take the run lock.
- `--batch` owns the same single run lock and passes `lock_already_held=True` to each task. Inter-task anti-risk waits remain in place, and a risk signal stops later tasks.
- `securityId` is used only in memory or the restricted sidecar during a live detail window. It never enters JSON/CSV/SQLite, logs, alerts, or Git. Actual successful detail channels and counts are written to export metadata for auditability.

## Export Contract v2

The list JSON provides a stable contract for downstream consumers (e.g. ai-pm-job-intel):

```json
{
  "format_version": 2,
  "keyword": "AI产品经理", "city": "上海",
  "page_count": 5, "job_count": 128, "warnings": ["第3页API未返回数据，已刷新重试"],
  "jobs": [
    {"job_id": "...", "title": "...", "location": "...", "job_link": "...",
     "company_name": "...", "salary": "25-35K", "experience": "3-5年",
     "education": "本科", "skills": ["大模型", "Agent"], "...": "..."}
  ]
}
```

- `format_version` increments on contract changes; `warnings` records scrape anomalies (empty API responses / risk blocks)
- Required fields: `job_id`/`title`/`location`/`job_link`/`company_name`; sensitive fields are stripped before writing (credentials never land in files)
- Optional (list scraping): `exhausted` (bool — whether this run reached the end of the result set: any page returning <30 items → true; `--pages 1` with <30 items → true; full pages without reaching the end → false. Basis for the spec-side sampling-aware de-listing; formally included in contract v2; missing field treated as false), `anonymous` (anonymous-posting flag 0/1 — recruiter hides the company name), `job_valid_status` (BOSS's official in-recruitment status, can calibrate de-listing inference), `icon_flags`/`icon_word` (platform labels such as "urgent"/"new"), `proxy_job`/`proxy_type` (proxy-hiring markers: headhunter/outsourcing), `job_type` (job type code)
- Optional (detail scraping): `page_update_date` (the detail page's "页面更新时间：YYYY-MM-DD" — DOM path only, unavailable via the API channel), `job_status_desc` (job status description), `brand_introduce` (company intro), `brand_stage_name`/`brand_scale_name`/`brand_industry_name` (company semantic dimensions), `brand_active_time` (company-level activity time from the detail API `brandComInfo.activeTime`, no extra request)
- **Detail scraping uses the API channel** (2026-08-14): one lightweight request per job (`/wapi/zpgeek/job/detail.json`) instead of full-page rendering — JD returns in ~0s; `securityId` is passed in-process from the list stage (never written to export files, red line preserved); the per-tab success budget is **4**, so the program rotates before the fifth request; `--input` prefers an unexpired sidecar, and auto mode tries the encrypt API before the policy DOM fallback
- **Mode 1 (default): jd merged into the export, jobs without JD are dropped** (2026-09-22): every job carries `jd` inline; JD-less jobs are removed (meta records `jd_coverage` and `dropped_no_jd`); `--keep-without-jd` keeps them with a marker
- **Explicit multi-channel + actual channel counts** (2026-09-22; encrypt added 2026-09-23): the detail phase records successful-result `detail_channel` values (`api`/`encrypt`/`dom`/`panel`/`mixed`) and `detail_channel_counts` in meta; failed attempts do not count as channel outcomes. Endpoint/parse failures may fall back to DOM, while real risk signals remain fail-closed
- **securityId restricted sidecar** (2026-09-22, red-line exception): a live list run writes it only when entering the detail window under `~/.boss-zhipin-scraper/.session/` (outside the repo), with mode `600` and a 60-minute TTL, then deletes it in the detail-call `finally`; `--input` only reads an existing sidecar and does not own it. It **never enters exports/logs/Git**, and the login cookie is never persisted. See `AGENTS.md`/`CONTRIBUTING.md`
- **Optional SQLite incremental store (P4c-2)**: enable with `--db [PATH]` (default `~/.boss-zhipin-scraper/boss.db`, **outside the repo, never committed**; zero new deps — stdlib `sqlite3`). WAL mode plus a `job_id`-keyed incremental upsert (`first_seen_at` preserved, `updated_at` overwritten), coexisting with JSON/CSV instead of replacing them; stored details seed cross-run resume (works even with a fresh output file, so no re-fetch). Records are sanitized before insert (cookie/token/securityId/internal IDs never persisted). Future schemas are rejected without downgrade writes; stats use a read-only connection, and interrupted runs retain `finished_at=NULL` lineage
- On finish a structured result line is printed: `EXPORT_OK jobs=N city=X keyword=Y path=Z` (or `EXPORT_FAIL reason=...` on risk-blocked abort)

## Post-Scrape Summary & Prompt

`scripts/job_summary.py` only reads the already-scraped `boss_jobs_*.json` and `boss_details_*.json`, computes **reliable aggregate stats** (salary market / salary-by-experience / top-salary ranking / experience / degree / district / company / scale / stage / skill tags) and produces a copy-paste prompt. **Suspected sales jobs are auto-filtered** (non-target roles mixed in by BOSS's fuzzy keyword matching: strong title markers like "sales/BD/account manager", or strong JD markers like "commission/revenue target/collection"; a lone "sales" word in a PM JD is not flagged). Jobs and their matching details are retained as one set for both text statistics and salary/experience charts, with the filtered count annotated. Experience aliases include `在校/应届`/`应届生`, and company labels prefer the contract field `company_name`. It never reads your local résumé file, pulls in no PDF dependency, and never scores a person against a job.

> Design split: the script only computes number-based stats (100% accurate); **semantic analysis is left to the AI agent** — the prompt references the full list/detail data file paths so the agent can read the complete JDs for deep research (skill profile of high-paying jobs, job-type clustering, job-search strategy, etc.).
>
> **Screening stage**: the `salary-by-experience` line answers "what is my experience level worth" (e.g. 3-5 yrs median 25K); the `top-salary` ranking lists the highest-paying jobs with company & district for quick shortlisting.

```bash
# Read the newest boss_jobs_*.json under the default result dir and auto-match the same-timestamp or newest detail file
python3 scripts/job_summary.py

# Specify list and detail files
python3 scripts/job_summary.py \
  --input ~/.boss-zhipin-scraper/job-result/boss_jobs_20260625_1200.json \
  --details ~/.boss-zhipin-scraper/job-result/boss_details_20260625_1200.json \
  --top 15

# Only emit the prompt
python3 scripts/job_summary.py --prompt-only
```

After installing the package you can also use the entry command:

```bash
uv run boss-summary --top 15
```

The summary covers: salary ranges, experience requirements, degree requirements, regional distribution, top companies, skill tags, frequent JD terms. The prompt asks the model to use these stats to fill in résumé keywords, suggest project-story rewrite directions, and produce an interview-prep checklist — while explicitly instructing it not to fabricate experience.

## File Structure

```
boss-zhipin-scraper/
├── SKILL.md              # Hermes Skill definition
├── README.md             # Chinese docs
├── README.en.md          # English docs
├── CHANGELOG.md
├── LICENSE
├── pyproject.toml
├── data/
│   └── city_codes.json   # Full city-code map
├── scripts/
│   ├── boss_cdp_raw.py   # Main scraper + CLI orchestration
│   ├── ratelimit.py      # BurstThrottle / adaptive limiting
│   ├── export_contract.py# Export contract, recursive redaction, atomic writes
│   ├── db_store.py       # SQLite schema / run lineage
│   ├── audit.py          # Risk-event audit log
│   └── job_summary.py    # Post-scrape summary + prompt
└── requirements.txt
```

## How It Works

This is a Chrome-CDP-based BOSS Zhipin crawler. Core flow:

1. Connect to an already-open Chrome via the Chrome DevTools Protocol (CDP)
2. Inject JS inside the BOSS Zhipin page that calls the search API via synchronous XHR
3. The API returns plaintext `salaryDesc`, bypassing the front-end font obfuscation
4. List-stage `securityId` exists only in memory or the restricted sidecar and is never written to exports; details use the `api`/`encrypt`/`dom`/`panel` paths, with actual successful channels recorded in metadata
5. Each page is written to disk immediately, deduped by `job_id`; repeated pages (paging failure) end early, and consecutive empty pages are only treated as risk after the page is confirmed to be a risk/captcha page (avoids false stops)

DOM extraction is not used for the list by default, since DOM salaries may be hit by font-based obfuscation. Only when `--allow-dom-fallback` is explicitly passed will it fall back to DOM when the API returns no data.

For detail pages, the scraper only extracts a section containing the job-description heading, using innerText to avoid `<style>`/hidden decoy text and stripping page-UI noise lines such as "举报"/"微信扫码分享"/"去APP". The DOM fallback is now faster: it returns as soon as the JD section is ready (instead of fixed waits), with selector fallbacks and dialog cleanup — about 38s → ~8–14s per job. Full-page `body` text is diagnostic input for detecting login walls and navigation shells and is never written directly as a JD. If the page contains the login-to-view-full-content marker, the crawl fails explicitly and stops before truncated text, recruiter metadata, company sections, or recommended jobs can be saved as a complete JD.

`--input ... --analysis --no-detail` first loads `--detail-output`, then the `boss_details_*.json` with the same timestamp in the same dir as the input list, and finally the newest detail file under `~/.boss-zhipin-scraper/job-result`.

## Chrome Profile Security Policy

`--setup-chrome` uses a persistent isolated profile by default — it neither symlinks nor copies your main Chrome data. First launch and subsequent launches only create or reuse this dedicated profile:

- `~/.boss-zhipin-scraper/chrome-profile`

Without an explicit `--output` or `--detail-output`, scraping results are saved under:

- `~/.boss-zhipin-scraper/job-result`

On first use you must log in to BOSS Zhipin manually inside this dedicated Chrome. `--setup-chrome` waits for the login to finish and uses the search API to confirm it can get plaintext `salaryDesc` before returning. The session is stored inside the dedicated profile and survives reboots; re-running `--setup-chrome` does not wipe it and does not affect your main Chrome, Gmail, GitHub, or other accounts.

Each login-probe round sends one search request, rotates across keyword/city targets, and backs off from 3 seconds to at most 15 seconds. Probe requests count toward the same 500-request global budget. Logged-out sessions, empty probe samples, API restrictions, and malformed responses are reported separately. A confirmed restriction such as `code: 31` or `code: 37` ("您的环境存在异常" / abnormal environment) stops probing immediately instead of prompting for another login or continuing frequent retries. Unknown risk-control codes are also recognized as restrictions via message keywords (abnormal environment, too-frequent access, security check, etc.), so an authenticated session that is merely rate-limited is no longer misreported as a login failure.

The interactive login page opened by `--setup-chrome` is the only temporary page intentionally brought to the foreground. Temporary tabs used by environment checks, list/detail scraping, and the smoke test run in the background so automation does not repeatedly steal focus. “Background” here only means the tab is not activated; the dedicated Chrome still runs with a visible UI and can be opened manually for inspection.

If you really need to import the BOSS session from your main Chrome, run explicitly:

```bash
python3 scripts/boss_cdp_raw.py --setup-chrome --copy-login-state
```

`--copy-login-state` overwrites the corresponding cookie-related files inside the isolated profile on every run; do not pass this for daily launches. It only copies `Local State` and `Default/Cookies*`, `Default/Network/Cookies*`-style cookie database files — not password stores, history, extensions, or a full profile. To wipe the dedicated browser's login state:

```bash
python3 scripts/boss_cdp_raw.py --setup-chrome --reset-chrome-profile
```

### Tearing down when you're done

After a scrape/analysis finishes, the dedicated Chrome is **not** closed automatically (the login state is kept by default so you can run the next scrape right away). When you're sure you no longer need it, tear it down manually:

```bash
python3 scripts/boss_cdp_raw.py --stop-chrome
```

`--stop-chrome` only closes the Chrome process(es) that belong to the scraper's isolated profile (`--user-data-dir`). It **never** kills by port or process name, so it cannot accidentally take down your main Chrome, Gmail, GitHub, or other signed-in sessions.

If you'd rather have a particular scrape close the dedicated Chrome once it finishes normally, add `--close-chrome`:

```bash
python3 scripts/boss_cdp_raw.py --keyword "AI Agent" --city 上海 --pages 3 --close-chrome
```

`--close-chrome` is off by default, and it only fires on the **success path** of a completed scrape — login failures, crashes, and other early exits leave the Chrome running so the login state is preserved.

## Development and Test Isolation

Tests do not need—and must not use—a real Chrome session. Before committing, run focused tests and then the isolation guard. The guard executes the full discovery suite under a temporary HOME and verifies that profile, run-lock, cooldown, audit, and runner-trace state were not modified:

```bash
python3 -m unittest tests.test_job_summary -v
python3 -m unittest tests.test_isolation_guard -v
python3 -m ruff check scripts tests
python3 -m py_compile scripts/boss_cdp_raw.py scripts/ratelimit.py scripts/export_contract.py scripts/db_store.py scripts/audit.py scripts/job_summary.py
```

Do not run tests from an old worktree or against live runtime state. Run a real login or scrape only after the user explicitly authorizes it.

## 📌 TODO

- [ ] Strengthen the detail-page `Referer` and request fingerprinting to further reduce risk-control triggers

## License

MIT

## Friends

- [LINUX DO](https://linux.do/) — A sincere, friendly, and vibrant tech community. This project endorses and recommends it.

## Star History

[![Star History Chart](https://api.star-history.com/chart?repos=eatmoreduck/boss-zhipin-scraper&type=date&legend=top-left&sealed_token=linAWksW9v7s0YEw83L89xbRzD4QWaJWxKrQHvkJBmx9xwMH8PseUKUQC9QAcRYaBFK1jBA_Mod4Vs8qH9A47spODANKwiVWieL3CxxQ3f9ZLqHYRwzTiA)](https://www.star-history.com/?type=date&repos=eatmoreduck%2Fboss-zhipin-scraper)
