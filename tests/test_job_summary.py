import contextlib
import importlib.util
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


# 同 tests/test_chrome_setup.py：Windows GBK 控制台无法编码 emoji，统一重配 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


ROOT_PATH = pathlib.Path(__file__).resolve().parents[1]
SUMMARY_SCRIPT_PATH = ROOT_PATH / "scripts" / "job_summary.py"


def load_summary_module():
    sys.modules.setdefault("websocket", mock.Mock())
    sys.modules.setdefault("requests", mock.Mock())
    spec = importlib.util.spec_from_file_location("job_summary", SUMMARY_SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class JobSummaryTests(unittest.TestCase):
    def test_module_exists_without_resume_file_scoring(self):
        self.assertTrue(SUMMARY_SCRIPT_PATH.exists())
        text = SUMMARY_SCRIPT_PATH.read_text(encoding="utf-8")
        for forbidden in ("pdfplumber", "parse_resume", "score_resume", "--resume"):
            self.assertNotIn(forbidden, text)

    def test_load_jobs_file_supports_scraper_output_shape(self):
        module = load_summary_module()
        payload = {
            "keyword": "AI Agent",
            "city": "上海",
            "jobs": [
                {
                    "title": "AI Agent工程师",
                    "salary": "30-60K",
                    "location": "上海·浦东新区",
                    "tags": "3-5年 | 本科 | Python",
                    "boss_name": "甲公司",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "boss_jobs_20260625_1200.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            jobs, metadata = module.load_jobs_file(str(path))

        self.assertEqual(len(jobs), 1)
        self.assertEqual(metadata["keyword"], "AI Agent")
        self.assertEqual(metadata["city"], "上海")

    def test_build_summary_counts_market_dimensions(self):
        module = load_summary_module()
        jobs = [
            {
                "job_id": "job-a",
                "title": "AI Agent工程师",
                "salary": "30-60K",
                "location": "上海·浦东新区",
                "tags": "3-5年 | 本科 | Python | LLM",
                "job_labels": ["AIGC"],
                "boss_name": "甲公司",
            },
            {
                "job_id": "job-b",
                "title": "LLM应用工程师",
                "salary": "30-60K",
                "location": "上海·浦东新区",
                "tags": "3-5年 | 本科 | RAG",
                "skills": "LangChain | RAG",
                "boss_name": "甲公司",
            },
            {
                "job_id": "job-c",
                "title": "AI平台工程师",
                "salary": "40-70K",
                "location": "上海·徐汇区",
                "tags": "5-10年 | 硕士 | Python",
                "boss_name": "乙公司",
            },
        ]
        details = [
            {"job_id": "job-a", "skill_tags": ["Python", "LLM"], "jd": "负责 Python LLM Agent RAG 应用开发"},
            {"job_id": "job-b", "skill_tags": ["RAG"], "jd": "建设 LLM RAG 检索增强生成系统"},
        ]

        summary = module.build_summary(jobs, details, search_keyword="AI Agent", top=5)

        self.assertEqual(summary["total_jobs"], 3)
        self.assertEqual(summary["total_details"], 2)
        self.assertEqual(summary["salary_ranges"][0], ("30-60K", 2))
        self.assertIn(("3-5年", 2), summary["experience"])
        self.assertIn(("本科", 2), summary["degrees"])
        self.assertIn(("浦东新区", 2), summary["districts"])
        self.assertIn(("甲公司", 2), summary["companies"])
        self.assertIn(("Python", 3), summary["skill_tags"])
        self.assertIn(("RAG", 3), summary["skill_tags"])
        self.assertIn(("LangChain", 1), summary["skill_tags"])
        self.assertIn(("AIGC", 1), summary["skill_tags"])
        self.assertTrue(any(term == "LLM" for term, _ in summary["jd_terms"]))

    def test_build_summary_uses_list_skills_without_details(self):
        module = load_summary_module()
        jobs = [
            {
                "title": "AI Agent工程师",
                "salary": "30-60K",
                "location": "上海·浦东新区",
                "tags": "3-5年 | 本科",
                "skills": "Python | LLM | RAG",
                "boss_name": "甲公司",
            }
        ]

        summary = module.build_summary(jobs, details=[], search_keyword="AI Agent")

        self.assertIn(("Python", 1), summary["skill_tags"])
        self.assertIn(("LLM", 1), summary["skill_tags"])
        self.assertIn(("RAG", 1), summary["skill_tags"])
        self.assertEqual(summary["jd_terms"], [])

    def test_build_summary_filters_details_to_current_jobs(self):
        module = load_summary_module()
        jobs = [
            {
                "job_id": "current",
                "title": "AI Agent工程师",
                "salary": "30-60K",
                "location": "上海·浦东新区",
                "tags": "3-5年 | 本科",
                "skills": "Python",
                "boss_name": "甲公司",
            }
        ]
        details = [
            {"job_id": "current", "skill_tags": ["Python"], "jd": "Python LLM Agent"},
            {"job_id": "other", "skill_tags": ["Rust"], "jd": "Rust Go"},
        ]

        summary = module.build_summary(jobs, details, search_keyword="AI Agent")

        self.assertEqual(summary["total_details"], 1)
        self.assertIn(("Python", 2), summary["skill_tags"])
        self.assertNotIn(("Rust", 1), summary["skill_tags"])

    def test_jd_terms_use_word_boundaries_for_english_terms(self):
        module = load_summary_module()
        details = [
            {"skill_tags": [], "jd": "负责 Django 和 AIGC 平台建设"},
        ]

        summary = module.build_summary([], details, search_keyword="Go AI")
        terms = {term for term, _ in summary["jd_terms"]}

        self.assertNotIn("Go", terms)
        self.assertNotIn("AI", terms)

    def test_jd_noise_terms_are_filtered(self):
        """JD 正文夹带的页面噪音（安全声明/工商信息/推荐栏/地名）应被过滤，只留真实技能词。

        回归:真实数据里 JD 高频词一度全是噪音（职位描述/直聘严禁用人/上海/工商信息等），
        污染了摘要和提示词。job_summary 层维护黑名单剔除它们。
        """
        module = load_summary_module()
        details = [
            {
                "skill_tags": [],
                "jd": (
                    "职位描述\n熟练使用 Python 和 LLM，熟悉 RAG\n"
                    "BOSS 安全提示：直聘严禁用人单位和招聘者用户做出任何损害求职者合法权益\n"
                    "工商信息 公司名称 法定代表人 注册资金\n"
                    "精选职位 城市招聘 推荐公司\n"
                    "工作地点：上海"
                ),
            },
        ]
        summary = module.build_summary([], details, search_keyword="Go AI")
        terms = {term for term, _ in summary["jd_terms"]}

        self.assertNotIn("职位描述", terms)
        self.assertNotIn("安全提示", terms)
        self.assertNotIn("工商信息", terms)
        self.assertNotIn("上海", terms)
        self.assertIn("Python", terms)
        self.assertIn("LLM", terms)
        self.assertIn("RAG", terms)

    def test_jd_function_words_are_filtered_as_noise(self):
        """JD 动态高频词中的纯功能词（需要/落地/具备等）不应冒充技能词。

        回归:真实数据（上海 AI 209 条）JD 高频词混入「需要(12)/落地(11)」等
        非技能词，稀释了摘要的技术含量。产品/设计等岗位方向词保留（有信息量）。
        """
        module = load_summary_module()
        # 英文/标点打断让「需要/落地/具备/负责/产品/设计」成为独立 2 字块
        # （与真实 JD 中 AI 产品 等英文打断后的切片一致）
        details = [
            {"skill_tags": [], "jd": "需要 X，落地 Y，具备 Z，负责 AI 产品，设计 AI"},
            {"skill_tags": [], "jd": "需要 M，落地 N，具备 W，负责 AI 产品，设计 AI"},
        ]
        summary = module.build_summary([], details, search_keyword="")
        terms = {term for term, _ in summary["jd_terms"]}

        self.assertNotIn("需要", terms)
        self.assertNotIn("落地", terms)
        self.assertNotIn("具备", terms)
        self.assertNotIn("负责", terms)
        self.assertIn("产品", terms, "岗位方向词应保留")
        self.assertIn("设计", terms)
        # 页面噪音应被全部过滤
        for noise in ("职位描述", "安全提示", "直聘严禁用人", "工商信息",
                      "公司名称", "法定代表人", "注册资金", "精选职位",
                      "城市招聘", "推荐公司", "上海"):
            self.assertNotIn(noise, terms, f"噪音词 {noise} 不应出现在 JD 高频词中")

    def test_explicit_details_path_does_not_fallback_to_latest(self):
        module = load_summary_module()
        with tempfile.TemporaryDirectory() as tmp:
            result_dir = pathlib.Path(tmp)
            list_path = result_dir / "boss_jobs_20260625_1200.json"
            latest_detail = result_dir / "boss_details_20260625_1300.json"
            missing_detail = result_dir / "missing_details.json"
            list_path.write_text('{"jobs":[]}', encoding="utf-8")
            latest_detail.write_text('[{"job_id":"wrong"}]', encoding="utf-8")

            with self.assertRaises(FileNotFoundError):
                module.load_details_for_input(
                    str(list_path),
                    detail_path=str(missing_detail),
                    result_dir=str(result_dir),
                )

    def test_output_mode_flags_are_mutually_exclusive(self):
        module = load_summary_module()

        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                module.build_arg_parser().parse_args(["--summary-only", "--prompt-only"])

    def test_top_must_be_positive(self):
        module = load_summary_module()

        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                module.build_arg_parser().parse_args(["--top", "0"])

    def test_main_reports_bad_input_without_traceback(self):
        module = load_summary_module()
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "boss_jobs_bad.json"
            path.write_text("{bad json", encoding="utf-8")

            with contextlib.redirect_stdout(io.StringIO()) as stdout:
                code = module.main(["--input", str(path)])

        self.assertEqual(code, 1)
        self.assertIn("无法加载输入文件", stdout.getvalue())

    def test_build_prompt_uses_aggregate_context_without_scores(self):
        module = load_summary_module()
        summary = {
            "keyword": "AI Agent",
            "city": "上海",
            "total_jobs": 3,
            "total_details": 2,
            "salary_ranges": [("30-60K", 2)],
            "salary_market": {"parsed": 3, "unparsed": 0, "median_k": 30,
                              "mean_k": 35, "low_k": 20, "high_k": 60},
            "experience": [("3-5年", 2)],
            "degrees": [("本科", 2)],
            "districts": [("浦东新区", 2)],
            "companies": [("甲公司", 2)],
            "skill_tags": [("Python", 3), ("RAG", 2)],
            "jd_terms": [("LLM", 2), ("Agent", 1)],
        }

        prompt = module.build_prompt(summary)

        self.assertIn("岗位市场摘要", prompt)
        self.assertIn("Python", prompt)
        self.assertIn("RAG", prompt)
        self.assertNotIn("LLM", prompt, "JD 高频词是语义弱替代，应交给 agent 读完整 JD")
        self.assertIn("不要虚构经历", prompt)
        self.assertNotIn("匹配分", prompt)
        self.assertNotIn("分数", prompt)

    def test_build_prompt_mentions_full_data_files_for_agent(self):
        module = load_summary_module()
        summary = {
            "keyword": "AI Agent", "city": "上海",
            "total_jobs": 3, "total_details": 2,
            "salary_ranges": [], "salary_market": {"parsed": 0, "unparsed": 3},
            "experience": [], "degrees": [], "districts": [],
            "companies": [], "skill_tags": [], "jd_terms": [],
        }

        prompt = module.build_prompt(
            summary,
            jobs_path=r"C:\x\boss_jobs_ai.json",
            details_path=r"C:\x\boss_details_ai.json",
        )
        self.assertIn("boss_jobs_ai.json", prompt)
        self.assertIn("boss_details_ai.json", prompt)
        self.assertIn("完整", prompt)

        bare = module.build_prompt(summary)
        self.assertNotIn("数据文件", bare, "无路径时不应出现数据文件行")

    def test_summary_script_is_documented_and_packaged(self):
        readme = (ROOT_PATH / "README.md").read_text(encoding="utf-8")
        changelog = (ROOT_PATH / "CHANGELOG.md").read_text(encoding="utf-8")
        skill = (ROOT_PATH / "SKILL.md").read_text(encoding="utf-8")
        pyproject = (ROOT_PATH / "pyproject.toml").read_text(encoding="utf-8")

        for document in (readme, changelog, skill):
            self.assertIn("job_summary.py", document)
            self.assertIn("提示词", document)
        self.assertIn("cp boss-zhipin-scraper/scripts/job_summary.py", skill)
        self.assertIn('boss-summary = "scripts.job_summary:main"', pyproject)


    def test_parse_salary_monthly_supports_k_and_daily_formats(self):
        module = load_summary_module()
        self.assertEqual(module.parse_salary_monthly("30-60K"), (30, 60))
        self.assertEqual(module.parse_salary_monthly("20-40K·15薪"), (20, 40))
        self.assertEqual(module.parse_salary_monthly("25-45K·13薪"), (25, 45))
        self.assertIsNone(module.parse_salary_monthly("未标注"))
        self.assertIsNone(module.parse_salary_monthly("面议"))
        self.assertIsNone(module.parse_salary_monthly(""))
        self.assertEqual(module.parse_salary_monthly("350-500元/天"), (7.7, 11.0),
                         "日薪按 22 工作日折算为月薪")

    def test_salary_stats_computes_market_rates(self):
        module = load_summary_module()
        jobs = [
            {"salary": "30-60K"},     # mid 45
            {"salary": "20-40K·15薪"}, # mid 30
            {"salary": "10-20K"},     # mid 15
            {"salary": "未标注"},      # unparsed
        ]
        stats = module.salary_stats(jobs)
        self.assertEqual(stats["parsed"], 3)
        self.assertEqual(stats["unparsed"], 1)
        self.assertEqual(stats["median_k"], 30, "中位月薪 30K")
        self.assertEqual(stats["mean_k"], 30, "均值 (45+30+15)/3 = 30")
        self.assertEqual(stats["low_k"], 12, "P10 分位（lows=[10,20,30] 线性外推）")
        self.assertEqual(stats["high_k"], 56, "P90 分位（highs=[30,40,60] 线性外推）")

    def test_salary_stats_handles_all_unparsed(self):
        module = load_summary_module()
        stats = module.salary_stats([{"salary": "未标注"}, {"salary": ""}])
        self.assertEqual(stats["parsed"], 0)
        self.assertEqual(stats["unparsed"], 2)
        self.assertIsNone(stats["median_k"])

    def test_build_summary_adds_market_and_company_dimensions(self):
        module = load_summary_module()
        jobs = [
            {"job_id": "a", "title": "T", "salary": "30-60K",
             "boss_name": "甲公司", "company_scale": "1000-9999人",
             "company_stage": "已上市"},
            {"job_id": "b", "title": "T", "salary": "20-40K",
             "boss_name": "乙公司", "company_scale": "100-499人",
             "company_stage": "B轮"},
            {"job_id": "c", "title": "T", "salary": "20-40K",
             "boss_name": "丙公司", "company_scale": "100-499人",
             "company_stage": "B轮"},
        ]
        summary = module.build_summary(jobs, search_keyword="AI", top=5)
        self.assertEqual(summary["salary_market"]["median_k"], 30, "中位 (30,30,45)")
        self.assertEqual(summary["salary_market"]["mean_k"], 35, "均值 (45+30+30)/3")
        self.assertEqual(summary["salary_market"]["parsed"], 3)
        self.assertIn(("100-499人", 2), summary["company_scales"])
        self.assertIn(("已上市", 1), summary["company_stages"])
        self.assertIn(("B轮", 2), summary["company_stages"])

    def test_format_summary_includes_market_and_company_lines(self):
        module = load_summary_module()
        jobs = [
            {"title": "T", "salary": "30-60K", "boss_name": "甲公司",
             "company_scale": "1000-9999人", "company_stage": "已上市"},
        ]
        summary = module.build_summary(jobs, search_keyword="AI", city="上海", top=3)
        text = module.format_summary(summary)
        self.assertIn("薪资行情", text)
        self.assertIn("中位", text)
        self.assertIn("30", text)
        self.assertIn("公司规模", text)
        self.assertIn("1000-9999人", text)
        self.assertIn("融资阶段", text)
        self.assertIn("已上市", text)


    def test_experience_bucket_normalizes_tags(self):
        module = load_summary_module()
        self.assertEqual(module.experience_bucket("应届"), "应届/在校")
        self.assertEqual(module.experience_bucket("在校生"), "应届/在校")
        self.assertEqual(module.experience_bucket("1年以内"), "1-3年")
        self.assertEqual(module.experience_bucket("1-3年"), "1-3年")
        self.assertEqual(module.experience_bucket("3-5年"), "3-5年")
        self.assertEqual(module.experience_bucket("5-10年"), "5-10年")
        self.assertEqual(module.experience_bucket("10年以上"), "10年以上")
        self.assertEqual(module.experience_bucket("经验不限"), "经验不限")
        self.assertEqual(module.experience_bucket("不限经验"), "经验不限")
        self.assertEqual(module.experience_bucket("乱七八糟"), "未标注")

    def test_salary_by_experience_groups_medians(self):
        module = load_summary_module()
        jobs = [
            {"title": "a", "salary": "30-60K", "tags": "3-5年 | 本科"},
            {"title": "b", "salary": "20-40K", "tags": "3-5年 | 本科"},
            {"title": "c", "salary": "10-20K", "tags": "1-3年 | 大专"},
            {"title": "d", "salary": "未标注", "tags": "3-5年"},
            {"title": "e", "salary": "25-45K", "tags": ""},
        ]
        by_exp = module.salary_by_experience(jobs)
        self.assertEqual(by_exp["3-5年"]["count"], 3, "a/b/d 三条均带 3-5年")
        self.assertEqual(by_exp["3-5年"]["median_k"], 37.5,
                         "解析值 [30,45] 两中位均值（原上中位 45 已修正）")
        self.assertEqual(by_exp["3-5年"]["unparsed"], 1, "d 薪资未标注")
        self.assertEqual(by_exp["1-3年"]["median_k"], 15)
        self.assertEqual(by_exp["未标注"]["count"], 1, "无经验标签的归未标注")
        self.assertEqual(by_exp["未标注"]["median_k"], 35)
        self.assertEqual(by_exp["3-5年"]["unparsed"], 1, "薪资未标注计入组内 unparsed")

    def test_top_salary_jobs_ranks_by_upper_bound(self):
        module = load_summary_module()
        jobs = [
            {"title": "高薪A", "salary": "100-200K", "boss_name": "甲",
             "location": "上海·浦东"},
            {"title": "中薪B", "salary": "30-60K", "boss_name": "乙",
             "location": "上海·徐汇"},
            {"title": "低薪C", "salary": "10-20K", "boss_name": "丙",
             "location": "上海·静安"},
        ]
        top = module.top_salary_jobs(jobs, 2)
        self.assertEqual([t["title"] for t in top], ["高薪A", "中薪B"])
        self.assertEqual(top[0]["salary"], "100-200K")

    def test_build_summary_adds_screening_dimensions(self):
        module = load_summary_module()
        jobs = [
            {"title": "a", "salary": "30-60K", "tags": "3-5年 | 本科",
             "boss_name": "甲", "location": "上海·浦东"},
            {"title": "b", "salary": "20-40K", "tags": "3-5年",
             "boss_name": "乙", "location": "上海·徐汇"},
        ]
        summary = module.build_summary(jobs, search_keyword="AI", top=3)
        self.assertIn("3-5年", summary["salary_by_experience"])
        self.assertEqual(summary["top_salary"][0]["title"], "a")

    def test_format_summary_includes_screening_lines(self):
        module = load_summary_module()
        jobs = [
            {"title": "a", "salary": "30-60K", "tags": "3-5年",
             "boss_name": "甲", "location": "上海·浦东"},
        ]
        summary = module.build_summary(jobs, search_keyword="AI", city="上海", top=3)
        text = module.format_summary(summary)
        self.assertIn("经验薪资", text)
        self.assertIn("高薪岗位", text)
        self.assertIn("30-60K", text)


class SalaryAnalysisUpgradeTests(unittest.TestCase):
    """分析产品化（pandas+matplotlib 引入）：口径修正 / N薪解析 / 样本警示 / 图表。"""

    def test_salary_stats_even_samples_median_is_mid_mean(self):
        """中位数取法修正：偶数样本取两中位均值（mid=[15,25,35,45] → 30）。"""
        module = load_summary_module()
        jobs = [{"salary": f"{lo}-{hi}K"}
                for lo, hi in [(10, 20), (20, 30), (30, 40), (40, 50)]]
        stats = module.salary_stats(jobs)
        self.assertEqual(stats["median_k"], 30.0, "偶数样本中位数应为两中位均值")

    def test_salary_stats_reports_p10_p90_range(self):
        """区间改 P10/P90 分位（极值在小样本不稳定），不再用 min/max。"""
        module = load_summary_module()
        jobs = [{"salary": f"{lo}-{hi}K"}
                for lo, hi in [(10, 20), (20, 30), (30, 40), (40, 50)]]
        stats = module.salary_stats(jobs)
        self.assertLessEqual(stats["low_k"], 25.0)
        self.assertGreaterEqual(stats["high_k"], 25.0)
        self.assertLess(stats["low_k"], min(
            (lo + hi) / 2 for lo, hi in [(10, 20), (20, 30), (30, 40), (40, 50)]),
            "P10 应低于最小值（分位外推）")
        self.assertGreater(stats["high_k"], max(
            (lo + hi) / 2 for lo, hi in [(10, 20), (20, 30), (30, 40), (40, 50)]),
            "P90 应高于最大值（分位外推）")

    def test_salary_by_experience_even_median_fixed(self):
        """经验档位偶数样本中位数修正：[30,45] → 37.5（此前取上中位 45）。"""
        module = load_summary_module()
        jobs = [
            {"title": "a", "salary": "30-60K", "tags": "3-5年"},
            {"title": "b", "salary": "20-40K", "tags": "3-5年"},
        ]
        by_exp = module.salary_by_experience(jobs)
        self.assertEqual(by_exp["3-5年"]["median_k"], 37.5)

    def test_parse_salary_annual_converts_by_months(self):
        """N薪解析：20-40K·15薪 → 年薪 300-600K（月薪×15）。"""
        module = load_summary_module()
        annual = module.parse_salary_annual("20-40K·15薪")
        self.assertEqual(annual, (300.0, 600.0))
        fallback = module.parse_salary_annual("20-40K")
        self.assertEqual(fallback, (240.0, 480.0), "无月数信息按 12 个月折算")

    def test_build_summary_adds_sample_warning_for_small_n(self):
        """样本量警示：可解析薪资 <30 时 summary 带警示，≥30 无警示。"""
        module = load_summary_module()
        small = [{"salary": "20-40K"} for _ in range(10)]
        big = [{"salary": "20-40K"} for _ in range(30)]
        s_small = module.build_summary(small, search_keyword="AI")
        s_big = module.build_summary(big, search_keyword="AI")
        self.assertTrue(s_small.get("sample_warning"),
                        "小样本应带样本量警示")
        self.assertFalse(s_big.get("sample_warning"),
                         "样本充足不应带警示")

    def test_format_summary_renders_sample_warning(self):
        """format_summary 头部输出样本警示行。"""
        module = load_summary_module()
        jobs = [{"title": "T", "salary": "20-40K"} for _ in range(5)]
        summary = module.build_summary(jobs, search_keyword="AI", city="上海")
        text = module.format_summary(summary)
        self.assertIn("样本", text)
        self.assertIn("警示", text)

    def test_generate_charts_produces_pngs(self):
        """matplotlib 图表：薪资直方图 + 经验×薪资条形图生成非空 PNG。"""
        module = load_summary_module()
        jobs = [
            {"title": f"T{i}", "salary": f"{lo}-{hi}K", "tags": "3-5年"}
            for i, (lo, hi) in enumerate([(20, 40), (30, 60), (10, 20),
                                          (25, 45), (15, 30)], start=1)
        ]
        with tempfile.TemporaryDirectory() as tmp:
            charts = module.generate_charts(jobs, tmp)
            self.assertEqual(len(charts), 2, "应生成 2 张图")
            for path in charts:
                self.assertTrue(os.path.exists(path), f"图表缺失: {path}")
                self.assertGreater(os.path.getsize(path), 0, f"图表为空: {path}")
                self.assertTrue(path.endswith(".png"))


if __name__ == "__main__":
    unittest.main()
