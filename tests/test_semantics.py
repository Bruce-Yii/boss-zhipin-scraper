"""岗位语义层（scripts/semantics.py）与链接刷新能力的单测。

背景（2026-10-22 逐条读 294 条导出后定位的根因）：
- 平台用 proxy_job/proxy_type 标注了"猎头代发"，我们从不语义化使用 → 25 条匿名+
  薪资虚高的代发岗混在结果里（11/25 薪资上限 ≥50K，企业直招岗 0 条）。
- 标题大小写/空格不一致 → 按字符串分组会漏。
- 同一份 JD 被多家猎头/多个发布重复投放（job_id 不同但内容同源）。
- 详情额度（按次数）与列表供给（按页数×组合）之间没有协调 → 结构性缺口。
- 平台 job_detail URL 是短时效凭证（隔天指向别的岗位）→ 需要"取新"与"可刷新"。
"""

import importlib.util
import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class SemanticsTests(unittest.TestCase):

    def setUp(self):
        self.s = load_module("semantics", ROOT / "scripts" / "semantics.py")

    # ---------- 发布主体类型 ----------
    def test_platform_proxy_flag_wins(self):
        """平台自己标的 proxy_job/proxy_type → agency（不靠猜公司名）。"""
        self.assertEqual(self.s.classify_post_type({"proxy_job": "1"}), "agency")
        self.assertEqual(self.s.classify_post_type({"proxy_type": "4"}), "agency")
        self.assertEqual(self.s.classify_post_type({"proxy_job": "", "proxy_type": ""}),
                         "direct")

    def test_anonymous_company_is_flagged(self):
        self.assertEqual(
            self.s.classify_post_type({"company_name": "某大型互联网直播电商上市公司"}),
            "anonymous")
        self.assertEqual(
            self.s.classify_post_type({"company_name": "某知名AI公司"}), "anonymous")
        self.assertEqual(
            self.s.classify_post_type({"company_name": "闪念科技"}), "direct")

    def test_agency_like_covers_both(self):
        self.assertTrue(self.s.is_agency_like({"proxy_job": "1"}))
        self.assertTrue(self.s.is_agency_like({"company_name": "某公司"}))
        self.assertFalse(self.s.is_agency_like({"company_name": "闪念科技"}))

    # ---------- 标题归一化 ----------
    def test_normalize_title_folds_case_and_space(self):
        self.assertEqual(self.s.normalize_title("AI 产品经理"),
                         self.s.normalize_title("ai产品经理"))
        self.assertEqual(self.s.normalize_title("AI产品经理"),
                         self.s.normalize_title("AI  产品  经理"))
        self.assertNotEqual(self.s.normalize_title("AI产品经理"),
                            self.s.normalize_title("AI产品销售经理"))

    # ---------- 内容指纹与重复投放 ----------
    def test_same_jd_from_two_agencies_is_one_group(self):
        """同一份 JD 被两家猎头投放 → 归为一组（重复）。"""
        jd = "岗位职责：负责 AI 产品需求分析与设计，推动版本交付。" * 8
        jobs = [
            {"job_id": "a1", "jd": jd, "salary": "20-30K", "location": "杭州",
             "company_name": "某猎头A"},
            {"job_id": "b2", "jd": jd, "salary": "20-30K", "location": "杭州",
             "company_name": "某猎头B"},
            {"job_id": "c3", "jd": "完全不同的岗位内容" * 20, "salary": "10-15K",
             "location": "上海", "company_name": "某公司"},
        ]
        fingerprint_by_id, sizes = self.s.group_duplicates(jobs)
        self.assertEqual(len(fingerprint_by_id), 3)
        self.assertEqual(sizes[fingerprint_by_id["a1"]], 2)
        self.assertEqual(sizes[fingerprint_by_id["c3"]], 1)

    def test_fingerprint_ignores_whitespace_only_differences(self):
        a = {"job_id": "a", "jd": "岗位职责  负责 AI 产品\n需求分析", "salary": "10-20K",
             "location": "杭州"}
        b = {"job_id": "b", "jd": "岗位职责 负责 AI 产品需求分析", "salary": "10-20K",
             "location": "杭州"}
        self.assertEqual(self.s.content_fingerprint(a), self.s.content_fingerprint(b))

    # ---------- 供给编排 ----------
    def test_supply_plan_scales_with_budget(self):
        needed, expected = self.s.supply_plan(700, 260)
        self.assertEqual(needed, 3)          # ceil(700/260)
        self.assertGreaterEqual(expected, 700)
        self.assertEqual(self.s.supply_plan(0)[0], 0)

    def test_supply_gap_reports_shortfall(self):
        gap, combos = self.s.supply_gap(700, 239)
        self.assertEqual(gap, 461)
        self.assertEqual(combos, 2)           # ceil(461/260)
        self.assertEqual(self.s.supply_gap(700, 900)[0], 0)   # 供给充足

    # ---------- 导出不变量 ----------
    def _clean_job(self, jid="j1"):
        return {
            "job_id": jid, "title": "AI产品经理", "company_name": "闪念科技",
            "salary": "20-30K", "location": "杭州", "job_link": "https://x/a.html",
            # JD 随 jid 变化：否则会被内容指纹判成"同源重复投放"（那是对的）
            "jd": f"岗位职责：负责 AI 产品需求分析与设计，方向 {jid}。" * 12,
            "post_type": "direct", "title_key": "ai产品经理",
            "link_verified_at": "2026-10-22T02:00:00",
        }

    def test_invariants_pass_on_clean_data(self):
        jobs = [self._clean_job(f"j{i}") for i in range(3)]
        result = self.s.check_invariants(jobs)
        failures = self.s.invariant_failures(result)
        self.assertEqual(failures, [], f"干净数据不应有失败项：{failures}")

    def test_invariants_catch_junk_tail_and_missing_type(self):
        job = self._clean_job()
        job["jd"] = job["jd"] + "\n薪资详情\n社保类型：五险一金\n刚刚活跃"
        job.pop("post_type")
        result = self.s.check_invariants([job])
        self.assertFalse(result["no_junk_tail"]["ok"])
        self.assertFalse(result["post_type"]["ok"])
        failures = " ".join(self.s.invariant_failures(result))
        self.assertIn("no_junk_tail", failures)
        self.assertIn("post_type", failures)

    def test_invariants_catch_missing_fields_and_link_stamp(self):
        job = self._clean_job()
        job["salary"] = ""
        job.pop("link_verified_at")
        result = self.s.check_invariants([job])
        self.assertFalse(result["field_salary"]["ok"])
        self.assertFalse(result["link_age"]["ok"])


class FourFixesTests(unittest.TestCase):
    """四项根因修法的行为锁定（2026-10-22）。"""

    def setUp(self):
        self.s = load_module("semantics", ROOT / "scripts" / "semantics.py")

    # ---------- 招聘类型（平台的 jobType）----------
    def test_classify_recruit_type_uses_platform_job_type(self):
        """平台 jobType 的含义是**实证**出来的，不是猜的：

        - jobType=4 → 实习岗：294 条实测里 24/24 是日薪制、24/24 经验要求为空、
          17/24 标题含"实习"
        - jobType=5 → 校招岗：**11/11 经验要求 = "在校/应届"**
        - jobType 空 → 社招岗：259 条全是月薪制、经验要求有值
        - jobType 空但标题自称实习 → 实习（平台近半数列表不打标记，标题为准）
        """
        self.assertEqual(self.s.classify_recruit_type({"job_type": "4"}), "intern")
        self.assertEqual(self.s.classify_recruit_type({"job_type": "5"}), "campus")
        self.assertEqual(self.s.classify_recruit_type(
            {"job_type": "", "title": "AI产品经理"}), "social")
        self.assertEqual(self.s.classify_recruit_type({}), "social")
        self.assertEqual(self.s.classify_recruit_type(None), "social")
        self.assertEqual(self.s.classify_recruit_type(
            {"job_type": "", "title": "AI产品经理实习生"}), "intern")

    # ---------- ① 失败指纹归类 ----------
    def test_classify_extract_failure_fingerprints(self):
        """抽取失败按**页面特征**归类，而不是笼统的 invalid_detail。"""
        self.assertEqual(
            self.s.classify_extract_failure(
                "job description too short after validation: 99 < 120"), "too_short")
        self.assertEqual(
            self.s.classify_extract_failure("职位描述"), "no_section")
        self.assertEqual(
            self.s.classify_extract_failure("", "BOSS正在加载中"), "shell_only")
        self.assertEqual(
            self.s.classify_extract_failure("", "登录查看完整内容"), "login_wall")
        self.assertEqual(
            self.s.classify_extract_failure("", "访问受限 您的账户存在异常行为"),
            "risk_page")
        self.assertEqual(self.s.classify_extract_failure("随便什么"), "unknown")

    # ---------- ② 重复组标记 ----------
    def test_mark_duplicate_groups_flags_same_content(self):
        """平台允许同一岗位多条发布（job_id 不同）→ 内容指纹标出重复组。"""
        jd = "岗位职责：负责 AI 产品需求分析与设计。" * 10
        jobs = [
            {"job_id": "a1", "jd": jd, "salary": "20-30K", "location": "杭州"},
            {"job_id": "b2", "jd": jd, "salary": "20-30K", "location": "杭州"},
            {"job_id": "c3", "jd": "别的岗位内容" * 30, "salary": "10-15K",
             "location": "上海"},
        ]
        self.s.mark_duplicate_groups(jobs)
        self.assertEqual(jobs[0]["dup_group"], jobs[1]["dup_group"])
        self.assertEqual(jobs[0]["dup_size"], 2)
        self.assertTrue(jobs[0]["is_primary"])
        self.assertFalse(jobs[1]["is_primary"])
        self.assertEqual(jobs[2]["dup_group"], "")
        self.assertTrue(jobs[2]["is_primary"])

    # ---------- ③ 标题归一化分组 ----------
    def test_title_groups_merge_case_variants(self):
        counts = self.s.title_group_counts([
            {"title": "AI产品经理"}, {"title": "ai产品经理"},
            {"title": "AI 产品经理"}, {"title": "AI产品经理"},
        ])
        self.assertEqual(counts, {"ai产品经理": 4})

    # ---------- ④ 活跃度三态 ----------
    def test_active_state_distinguishes_unknown_from_inactive(self):
        """**"平台没给"不等于"不活跃"**（实测 14 条两边都没有活跃标签）。"""
        self.assertEqual(self.s.active_state(""), "unknown")
        self.assertEqual(self.s.active_state("刚刚活跃"), "active")
        self.assertEqual(self.s.active_state("在线"), "active")
        self.assertEqual(self.s.active_state("半年前活跃"), "inactive")


class KnownBadPageSkipTests(unittest.TestCase):
    """① 已知坏页：命中台账 → 跳过、**不消耗额度**、不发请求。"""

    def setUp(self):
        self.module = load_module("boss_cdp_raw", ROOT / "scripts" / "boss_cdp_raw.py")

    def test_known_bad_page_is_skipped_without_request(self):
        module = self.module
        ws = mock.Mock()
        with mock.patch.object(module._known_failures, "is_known", return_value=True):
            result = module._scrape_one_detail_via_html(
                {"job_id": "j1", "title": "AI产品经理",
                 "job_link": "https://www.zhipin.com/job_detail/x.html"}, ws, "s")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "known_bad_page")
        ws.eval_js_async.assert_not_called()

    def test_invalid_detail_is_recorded_into_ledger(self):
        """抽取失败 → 沉淀 job_id + 失败指纹（供次日跳过）。"""
        module = self.module
        recorded = {}

        def fake_record(job_id, fingerprint, reason):
            recorded["job_id"] = job_id
            recorded["fingerprint"] = fingerprint
            recorded["reason"] = reason

        with mock.patch.object(module._known_failures, "record",
                               side_effect=fake_record):
            module._record_known_bad_page("j9", "职位描述")
        self.assertEqual(recorded["job_id"], "j9")
        self.assertEqual(recorded["fingerprint"], "no_section")


class DropJunkTests(unittest.TestCase):
    """删库规则（用户 2026-10-23：销售/技术/噪声直接删库，不保留）。"""

    def setUp(self):
        self.s = load_module("semantics", ROOT / "scripts" / "semantics.py")

    def _drop(self, jobs):
        kept, dropped = self.s.drop_junk_jobs(jobs)
        return kept, dropped

    def test_sales_title_dropped(self):
        kept, dropped = self._drop([{"job_id": "a", "title": "AI产品销售经理",
                                     "company_name": "X", "jd": "做AI产品"}])
        self.assertEqual(kept, [])
        self.assertEqual(dropped["sales"], 1)

    def test_sales_jd_dropped(self):
        """JD 明着卖货（提成/业绩/回款）→ 直接删，不再设疑似（issue #157）。
        标题再 AI 也删：只预警等于留垃圾。"""
        kept, dropped = self._drop([{"job_id": "a", "title": "AI产品经理",
                                     "company_name": "X",
                                     "jd": "负责AI产品规划，提成和完成业绩指标，按回款拿佣金"}])
        self.assertEqual(kept, [])
        self.assertEqual(dropped["sales"], 1)
        self.assertEqual(dropped["suspect"], 0)

    def test_title_ai_skips_jd_ai_check(self):
        """标题带 AI（issue #157）：名字没问题，不再拿 JD 查 AI。
        JD 无 AI、非销售、能对上标题 → 留（等 JD 层没有别的毛病）。"""
        kept, dropped = self._drop([{"job_id": "a", "title": "AI产品经理",
                                     "company_name": "X",
                                     "jd": "负责产品规划与需求分析，协调研发按期交付"}])
        self.assertEqual(len(kept), 1)

    def test_sales_jd_without_ai_dropped(self):
        """没 AI 证据+JD 命中销售词 → 直接删，不进疑似（实测产品运营@拙诚润泽）。"""
        kept, dropped = self._drop([{"job_id": "a", "title": "产品运营",
                                     "company_name": "X",
                                     "jd": "负责课程销售，提成和完成业绩指标，按回款拿佣金"}])
        self.assertEqual(kept, [])
        self.assertEqual(dropped["suspect"], 0)

    def test_incidental_ai_not_evidence(self):
        """JD 只有顺口提 AI（对 AI 感兴趣/使用 AI 工具提效）→ 不算证据，删。"""
        kept, _ = self._drop([{"job_id": "a", "title": "新媒体运营",
                               "company_name": "X",
                               "jd": "对 AI 感兴趣，会使用 AI 工具提效，负责新媒体内容更新"}])
        self.assertEqual(kept, [])
        kept, _ = self._drop([{"job_id": "b", "title": "电商网络渠道助理",
                               "company_name": "X",
                               "jd": "可利用 AI 辅助，负责渠道对接"}])
        self.assertEqual(kept, [])

    def test_true_ai_phrase_kept(self):
        """岗位本体的 AI 提法（车端 AI 功能/AI 科技）→ 算证据，留。"""
        kept, _ = self._drop([{"job_id": "a", "title": "产品实习生",
                               "company_name": "X",
                               "jd": "负责车端 AI 功能的产品定义"}])
        self.assertEqual(len(kept), 1)

    def test_outbound_and_engineering_terms_dropped(self):
        """外呼标题算销售，工程标题算技术（2026-10-23 补词）。"""
        kept, dropped = self._drop([{"job_id": "a", "title": "AI外呼专员",
                                     "company_name": "X", "jd": "做AI产品"}])
        self.assertEqual(kept, [])
        self.assertEqual(dropped["sales"], 1)
        kept, dropped = self._drop([{"job_id": "b", "title": "AI应用工程专员",
                                     "company_name": "X", "jd": "做AI产品"}])
        self.assertEqual(kept, [])
        self.assertEqual(dropped["tech"], 1)

    def test_product_jd_with_sales_word_not_hurt(self):
        """产品岗 JD 仅含"销售"单字（如提升销售转化）不误伤——只认强特征词。"""
        kept, dropped = self._drop([{"job_id": "a", "title": "AI产品经理",
                                     "company_name": "X",
                                     "jd": "负责AI产品规划，提升销售转化"}])
        self.assertEqual(len(kept), 1)

    def test_tech_title_dropped(self):
        kept, dropped = self._drop([{"job_id": "a", "title": "AI应用开发工程师",
                                     "company_name": "X",
                                     "jd": "参与AI应用开发"}])
        self.assertEqual(kept, [])

    def test_no_jd_skips_ai_check(self):
        """没 JD 不判无 AI（issue #156）：标题没问题就留，抓到 JD 再判。
        实测智海星河数据产品经理：标题无 AI 字样、技能无 AI，无 JD 时被误删；
        有 JD 后（Agent 业务）本应留下。"""
        kept, dropped = self._drop([{"job_id": "a", "title": "数据产品经理实习生",
                                     "company_name": "智海星河",
                                     "skill_tags": ["Excel", "SQL"]}])
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped["noise"], 0)

    def test_aigc_title_passes_role_gate(self):
        """标题含 AIGC 不用岗名词兜底（issue #155 用户口径：aigc 没问题）：
        AIGC编导这类岗此前连 JD 层都到不了，现在放行到 JD 层。"""
        kept, dropped = self._drop([{"job_id": "a", "title": "AIGC编导实习生",
                                     "company_name": "X"}])
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped["noise"], 0)

    def test_jiejuefangan_role_word_kept_without_jd(self):
        """解决方案是用户点名认可的非技术岗名（issue #179）：
        AI解决方案实习生无 JD 时不再判噪声。"""
        kept, dropped = self._drop([{"job_id": "a", "title": "AI解决方案实习生",
                                     "company_name": "X"}])
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped["noise"], 0)

    def test_jiejuefangan_without_ai_kept(self):
        """解决方案实习生不能因 JD 没写 AI 被删（2026-10-28 用户口径）。"""
        kept, dropped = self._drop([{"job_id": "a", "title": "解决方案实习生",
                                     "company_name": "X",
                                     "jd": "负责客户解决方案落地与交付协调"}])
        self.assertEqual([j["job_id"] for j in kept], ["a"])
        self.assertEqual(dropped["noise"], 0)

    def test_product_marketing_title_still_excluded(self):
        """产品营销/市场类不算产品岗目标（2026-10-28 口径排除项）。"""
        kept, dropped = self._drop([{"job_id": "a", "title": "产品营销实习生",
                                     "company_name": "X",
                                     "jd": "负责美妆产品营销活动策划"}])
        self.assertEqual(kept, [])

    def test_ai_solution_engineer_kept(self):
        """AI 解决方案工程师类，标题带 AI 关键词即放行（issue #181 用户口径：
        解决方案工程师只要有 ai 关键词就可以）——不再按技术岗删。"""
        kept, dropped = self._drop([
            {"job_id": "a", "title": "AI解决方案工程师", "company_name": "X",
             "jd": "负责AI方案在客户侧的落地交付"},
            {"job_id": "b", "title": "大模型解决方案架构师", "company_name": "Y",
             "jd": "负责大模型行业方案设计"}])
        self.assertEqual(len(kept), 2)
        self.assertEqual(dropped["tech"], 0)

    def test_solution_engineer_without_ai_still_dropped(self):
        """豁免只给带 AI 关键词的：无 AI 的解决方案工程师仍按技术岗删。"""
        kept, dropped = self._drop([{"job_id": "a", "title": "解决方案工程师",
                                     "company_name": "X",
                                     "jd": "负责机器视觉方案交付"}])
        self.assertEqual(kept, [])
        self.assertEqual(dropped["tech"], 1)

    def test_role_gate_still_guards_non_ai_and_bare_ai_titles(self):
        """岗名门只给 AIGC 放行：无岗名又无 AIGC 的标题（含裸 AI 前缀）仍判噪声。"""
        kept, dropped = self._drop([{"job_id": "a", "title": "神秘高薪岗位",
                                     "company_name": "X"},
                                    {"job_id": "b", "title": "AI训练师",
                                     "company_name": "Y"}])
        self.assertEqual(kept, [])
        self.assertEqual(dropped["noise"], 2)

    def test_role_gate_pass_does_not_bypass_sales_tech(self):
        """放行不越过销售/技术判定：AIGC剪辑仍删技术，AIGC外呼仍删销售。"""
        kept, dropped = self._drop([{"job_id": "a", "title": "AIGC视频剪辑专员",
                                     "company_name": "X"},
                                    {"job_id": "b", "title": "AIGC外呼专员",
                                     "company_name": "Y"}])
        self.assertEqual(kept, [])
        self.assertEqual(dropped["tech"], 1)
        self.assertEqual(dropped["sales"], 1)

    def test_fde_exception_kept(self):
        """FDE 用户明示保留：标题再技术也留。"""
        kept, dropped = self._drop([
            {"job_id": "a", "title": "FDE 全栈工程师MJ000006",
             "company_name": "靖安科技", "jd": "把需求转化为技术方案"},
            {"job_id": "b", "title": "数据分析（FDE）",
             "company_name": "预策科技", "jd": "业务分析"}])
        self.assertEqual(len(kept), 2)

    def test_pm_core_with_tech_prefix_kept(self):
        """产品经理挂技术方向（算法/测试产品经理）是产品岗，不算技术岗。"""
        kept, dropped = self._drop([
            {"job_id": "a", "title": "AIGC算法产品经理",
             "company_name": "群核科技", "jd": "负责AIGC算法产品规划"},
            {"job_id": "b", "title": "AI芯片测试产品经理",
             "company_name": "Y", "jd": "负责AI芯片测试产品定义"}])
        self.assertEqual(len(kept), 2)

    def test_mismatch_dropped(self):
        """JD 与岗名不符（引流帖）→ 删。"""
        kept, dropped = self._drop([{"job_id": "a", "title": "产品经理",
                                     "company_name": "上海趣泡",
                                     "jd": "看清楚再投简历，谢谢招募小红书运营合伙人"}])
        self.assertEqual(kept, [])
        self.assertEqual(dropped["mismatch"], 1)

    def test_product_title_without_ai_kept(self):
        """产品类标题不再因 JD 没写 AI 被删（2026-10-28 用户口径）。"""
        kept, dropped = self._drop([{"job_id": "a", "title": "软件产品助理",
                                     "company_name": "有信科技",
                                     "jd": "协助产品经理对接，原型设计"}])
        self.assertEqual([j["job_id"] for j in kept], ["a"])
        self.assertEqual(dropped["noise"], 0)

    def test_b_layer_kept(self):
        """B 层：标题普通但 JD 含 AI → 留。"""
        kept, dropped = self._drop([{"job_id": "a", "title": "产品实习生",
                                     "company_name": "每日互动",
                                     "jd": "用大模型做用户增长分析"}])
        self.assertEqual(len(kept), 1)

    def test_skills_ai_kept(self):
        """技能含 AI 也算证据 → 留。"""
        kept, dropped = self._drop([{"job_id": "a", "title": "产品实习生",
                                     "company_name": "国量测控",
                                     "jd": "负责产品需求文档",
                                     "skill_tags": ["AI产品"]}])
        self.assertEqual(len(kept), 1)

    def test_no_role_title_dropped(self):
        """标题连岗名都说不清（纯AI应用/Agent实习生）→ 噪声，删。"""
        kept, dropped = self._drop([{"job_id": "a", "title": "AI应用实习生",
                                     "company_name": "艾麦物流",
                                     "jd": "参与AI项目原型开发"}])
        self.assertEqual(kept, [])
        self.assertEqual(dropped["noise"], 1)

    def test_no_jd_kept(self):
        """无 JD 的记录跳过 JD 相关规则。"""
        kept, dropped = self._drop([{"job_id": "a", "title": "AI产品经理",
                                     "company_name": "X"}])
        self.assertEqual(len(kept), 1)

    def test_dropped_items_auditable(self):
        """丢弃清单带 job_id/title/company/reason，可追溯。"""
        _, dropped = self._drop([{"job_id": "a", "title": "AI产品销售经理",
                                  "company_name": "X", "jd": "做AI产品"}])
        self.assertEqual(len(dropped["items"]), 1)
        self.assertEqual(dropped["items"][0]["job_id"], "a")
        self.assertIn("销售", dropped["items"][0]["reason"])

    def test_nonai_ops_title_dropped_at_list_stage(self):
        """2026-10-27 口径：非 AI 的运营/电商/客服类标题列表阶段直接毙。"""
        kept, dropped = self._drop([
            {"job_id": "a", "title": "新媒体运营实习生", "company_name": "X"},
            {"job_id": "b", "title": "运营专员", "company_name": "Y"},
            {"job_id": "c", "title": "电商运营助理", "company_name": "Z"},
        ])
        self.assertEqual(kept, [])
        self.assertEqual(dropped["noise"], 3)

    def test_ops_title_with_ai_jd_kept(self):
        """运营标题但 JD 是 AI 岗（如 AI 平台用户运营）→ 不删（2026-10-28 修）。"""
        kept, dropped = self._drop([
            {"job_id": "a", "title": "海外用户运营实习生", "company_name": "X",
             "jd": "参与AI语音与创作者平台的海外增长，负责AI产品运营与增长实验"},
            {"job_id": "b", "title": "运营专员", "company_name": "Y",
             "jd": "负责电商店铺日常上下架、客服与售后"},
        ])
        self.assertEqual([j["job_id"] for j in kept], ["a"])
        self.assertEqual(dropped["noise"], 1)

    def test_ai_company_generic_product_role_kept(self):
        """AI 公司的通用产品岗（JD 没写 AI 词）也算 AI 非技术岗（2026-10-28）。"""
        kept, dropped = self._drop([
            {"job_id": "a", "title": "产品实习生", "company_name": "蓝芯机器人",
             "company_industry": "智能硬件", "jd": "协助产品经理整理需求与竞品"},
            {"job_id": "b", "title": "产品实习生", "company_name": "某贸易公司",
             "company_industry": "贸易/进出口", "jd": "协助整理订单与报关资料"},
        ])
        self.assertEqual([j["job_id"] for j in kept], ["a"])
        self.assertEqual(len(dropped["items"]), 1)

    def test_ai_company_non_target_roles_still_dropped(self):
        """AI 公司里的零售/结构/外呼等非目标岗仍删。"""
        kept, dropped = self._drop([
            {"job_id": "a", "title": "零售运营实习生", "company_name": "智能精准学",
             "company_industry": "人工智能", "jd": "门店零售日常运营"},
            {"job_id": "b", "title": "结构助理实习生", "company_name": "梅卡曼德机器人",
             "company_industry": "人工智能", "jd": "协助机械结构设计"},
            {"job_id": "c", "title": "运营实习生（外呼）", "company_name": "中关村科金",
             "company_industry": "人工智能", "jd": "外呼回访客户"},
        ])
        self.assertEqual(kept, [])
        self.assertEqual(len(dropped["items"]), 3)

    def test_robotics_ai_domain_terms_count_as_evidence(self):
        """机器人/具身智能/智能硬件等 AI 领域词也算 AI 证据（2026-10-28 扩词表）。"""
        kept, dropped = self._drop([
            {"job_id": "a", "title": "机器人展厅运营实习生", "company_name": "X",
             "jd": "具身智能未来岛（机器人学校）实习生，负责展厅运营与机器人操控方向"},
        ])
        self.assertEqual([j["job_id"] for j in kept], ["a"])
        self.assertEqual(dropped["noise"], 0)

    def test_sales_jd_compensation_only_keeps_ai_title(self):
        """AI 标题 + JD 只命中薪酬词（提成）→ 不按卖货删（ANSO 误删修复）。"""
        kept, dropped = self._drop([
            {"job_id": "a", "title": "AI原生服务运营师（ANSO）",
             "company_name": "X",
             "jd": "AI Native 服务公司，实习 200-300 元/天，月薪 4-6K + 提成"},
        ])
        self.assertEqual([j["job_id"] for j in kept], ["a"])
        self.assertEqual(dropped["sales"], 0)

    def test_sales_jd_compensation_only_keeps_jd_ai_evidence(self):
        """标题不带 AI、但 JD 有 AI 证据 + 只命中薪酬词（提成）→ 不删。"""
        kept, dropped = self._drop([
            {"job_id": "a", "title": "账号运营实习生", "company_name": "X",
             "jd": "AI 内容公司账号运营，负责 TikTok 矩阵与 AI 合规诊断；薪资含提成"},
        ])
        self.assertEqual([j["job_id"] for j in kept], ["a"])
        self.assertEqual(dropped["sales"], 0)

    def test_sales_jd_action_words_still_dropped_for_ai_title(self):
        """AI 标题 + JD 明着卖货（签单/客户资源）→ 仍删。"""
        kept, dropped = self._drop([
            {"job_id": "a", "title": "AI产品经理", "company_name": "X",
             "jd": "负责AI产品，签单提成，自带客户资源，完成销售目标"},
        ])
        self.assertEqual(kept, [])
        self.assertEqual(dropped["sales"], 1)

    def test_ai_or_product_ops_title_not_dropped_by_new_rule(self):
        """标题带 AI/产品的运营类（非新媒体）走正常判定，不被新规则误杀。"""
        kept, dropped = self._drop([
            {"job_id": "a", "title": "AI电商运营", "company_name": "X"},
            {"job_id": "b", "title": "产品运营实习生", "company_name": "Y"},
        ])
        self.assertEqual(len(kept), 2)
        self.assertEqual(dropped["noise"], 0)


    def test_xinmeiti_always_dropped(self):
        """2026-10-27：新媒体标题无例外，连 AI/产品一起毙。"""
        kept, dropped = self._drop([
            {"job_id": "a", "title": "AI新媒体运营", "company_name": "X"},
            {"job_id": "b", "title": "新媒体产品经理", "company_name": "Y"},
            {"job_id": "c", "title": "新媒体运营实习生", "company_name": "Z"},
        ])
        self.assertEqual(kept, [])
        self.assertEqual(dropped["noise"], 3)

if __name__ == "__main__":
    unittest.main()
