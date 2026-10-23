"""岗位数据模型（scripts/job_model.py）的契约测试。

这个文件是**防复发**的核心：它把"平台给了字段，我们必须说清它是什么、被谁用"变成
CI 里会红的检查。2026-10-22 之前的现状是：平台 29 个字段里有 10 个（34%）被采集却
零下游消费——猎头代发标注、实习/校招标注、岗位更新时间、活跃度、角标、行业、福利…
全是同一类病，而代码不报错、测试不红、CI 全绿。

现在：
- 新增字段若既没有消费方、也没写豁免理由 → ``spec_issues()`` 报错 → 测试失败
- 声明了消费方但源码里查无引用 → ``audit_unused_fields()`` 报错 → 测试失败
- 记录级语义（发布主体/招聘类型/活跃三态/标题键/薪资解析）**只有模型一处定义**，
  其它模块不得再自己实现一遍
"""

import importlib.util
import pathlib
import re
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class JobModelContractTests(unittest.TestCase):

    def setUp(self):
        self.model = load_module("job_model", SCRIPTS / "job_model.py")

    # ---------- 契约本身 ----------
    def test_every_field_declares_semantics(self):
        """每个字段都要说清：来源、类型、语义。"""
        issues = self.model.spec_issues()
        self.assertEqual(issues, [], f"字段契约有问题：{issues}")

    def test_no_collected_field_without_consumer_or_exemption(self):
        """**采集了却没有消费方、又没写豁免理由 → 失败**（这就是"抄了不用"）。"""
        collected_without_use = []
        for spec in self.model.FIELD_SPECS:
            if not spec.is_platform_field:
                continue
            if not spec.used_by and not spec.exempt_reason and not spec.delivery_only:
                collected_without_use.append(spec.name)
        self.assertEqual(
            collected_without_use, [],
            "这些平台字段被采集却既无消费方也无豁免理由："
            f"{collected_without_use}（要么用起来，要么写明为什么不消费）")

    def test_delivery_only_fields_must_say_why(self):
        """声明"仅交付给用户"的字段必须写明理由（不能沉默地只抄不解释）。"""
        for spec in self.model.FIELD_SPECS:
            if spec.delivery_only:
                self.assertTrue(
                    spec.notes, f"{spec.name} 标了 delivery_only 但没写理由")

    def test_declared_consumers_really_reference_the_field(self):
        """声明的消费方必须在源码里真的引用该字段（防"声明与实现不一致"）。"""
        missing = self.model.audit_unused_fields(str(ROOT))
        self.assertEqual(missing, [], f"声明了消费方但源码查无引用：{missing}")

    def test_platform_fields_are_not_silently_dropped(self):
        """列表 API 的原始字段要么进模型，要么写明豁免理由。"""
        known = {spec.name for spec in self.model.FIELD_SPECS}
        for name in ("proxy_job", "proxy_type", "job_type", "job_valid_status",
                     "icon_flags", "icon_word", "company_industry", "welfare",
                     "boss_title", "boss_active_status", "anonymous"):
            self.assertIn(name, known, f"{name} 应在模型契约里声明")

    # ---------- 记录级语义：唯一判定点 ----------
    def test_post_type_uses_platform_marks(self):
        self.assertEqual(self.model.classify_post_type({"proxy_job": "1"}), "agency")
        self.assertEqual(self.model.classify_post_type({"proxy_type": "4"}), "agency")
        self.assertEqual(
            self.model.classify_post_type({"boss_title": "猎头顾问"}), "agency")
        self.assertEqual(
            self.model.classify_post_type({"company_name": "某大型互联网公司"}),
            "anonymous")
        self.assertEqual(
            self.model.classify_post_type({"company_name": "闪念科技"}), "direct")

    def test_recruit_type_uses_platform_job_type(self):
        self.assertEqual(self.model.classify_recruit_type({"job_type": "4"}), "intern")
        self.assertEqual(self.model.classify_recruit_type({"job_type": "5"}), "campus")
        self.assertEqual(self.model.classify_recruit_type({}), "social")

    def test_active_state_three_states(self):
        self.assertEqual(self.model.active_state(""), "unknown")
        self.assertEqual(self.model.active_state("刚刚活跃"), "active")
        self.assertEqual(self.model.active_state("半年前活跃"), "inactive")

    def test_normalize_title_folds_case_space_and_punctuation(self):
        self.assertEqual(self.model.normalize_title("AI产品经理"),
                         self.model.normalize_title("ai 产品经理"))
        self.assertEqual(self.model.normalize_title("AI产品经理（Agent方向）"),
                         self.model.normalize_title("AI产品经理(Agent方向)"))

    def test_salary_parsing_single_definition(self):
        self.assertEqual(self.model.parse_salary_monthly("20-40K"), (20.0, 40.0))
        self.assertEqual(self.model.parse_salary_monthly("300-500元/天"), (6.6, 11.0))
        self.assertIsNone(self.model.parse_salary_monthly("面议"))
        self.assertEqual(self.model.parse_salary_annual("20-40K·15薪"), (300.0, 600.0))

    def test_job_id_is_generated_in_one_place(self):
        import hashlib
        expected = hashlib.md5(b"https://x/a.html").hexdigest()[:16]
        self.assertEqual(self.model.compute_job_id("https://x/a.html"), expected)

    # ---------- 记录构造与合并 ----------
    def test_from_list_payload_shape_matches_js_template(self):
        """两条列表通道（注入 XHR / 被动捕获）产出**同形状** → 字段集必须一致。"""
        raw = {
            "jobName": "AI产品经理", "salaryDesc": "20-40K", "cityName": "杭州",
            "areaDistrict": "余杭区", "businessDistrict": "仓前",
            "jobExperience": "3-5年", "jobDegree": "本科", "brandName": "闪念科技",
            "bossTitle": "HR", "activeTimeDesc": "今日活跃", "anonymous": 0,
            "jobValidStatus": 1, "iconFlagList": ["急"], "iconWord": "急",
            "proxyJob": "", "proxyType": "", "jobType": "",
            "brandScaleName": "100-499人", "brandStageName": "A轮",
            "brandIndustry": "互联网", "jobLabels": ["五险一金"], "skills": ["RAG"],
            "securityId": "sid", "lid": "lid1", "encryptJobId": "ejid",
            "encryptBossId": "ebid", "encryptBrandId": "ebrid",
            "welfareList": ["五险一金"],
        }
        row = self.model.JobRecord.from_list_payload(raw).to_dict()
        js_keys = {
            "title", "salary", "salary_source", "location", "tags", "boss_name",
            "company_name", "boss_title", "boss_active_status", "anonymous",
            "job_valid_status", "icon_flags", "icon_word", "proxy_job", "proxy_type",
            "job_type", "experience", "education", "company_scale", "company_stage",
            "company_industry", "job_labels", "skills", "security_id", "lid",
            "encrypt_job_id", "encrypt_boss_id", "encrypt_brand_id", "job_link",
            "company_link", "welfare", "link_verified_at",
        }
        self.assertEqual(set(row) - {"job_id"}, js_keys)
        self.assertEqual(row["job_link"],
                         "https://www.zhipin.com/job_detail/ejid.html")
        self.assertEqual(row["company_link"],
                         "https://www.zhipin.com/gongsi/ebrid.html")
        self.assertEqual(row["location"], "杭州·余杭区·仓前")
        self.assertEqual(row["tags"], "3-5年 | 本科")
        self.assertEqual(row["salary_source"], "api")
        self.assertEqual(row["boss_active_status"], "今日活跃")
        self.assertTrue(row["job_id"])

    def test_merge_detail_copies_detail_fields_and_recomputes_derived(self):
        """合并详情：详情侧字段进来 + 派生语义重算（不再手工搬字段）。"""
        job = {"job_id": "j1", "title": "AI产品经理", "company_name": "闪念科技",
               "salary": "20-40K", "proxy_job": "1", "job_type": "4",
               "boss_active_status": "今日活跃", "tags": "3-5年"}
        detail = {"job_id": "j1", "jd": "岗位职责正文" * 10,
                  "page_update_date": "2026-10-22",
                  "skill_tags": ["B端产品"], "boss_active_status": "刚刚活跃",
                  "link_verified_at": "2026-10-22T08:00:00"}
        merged = self.model.JobRecord(job).merge_detail(detail).to_dict()
        self.assertEqual(merged["jd"], "岗位职责正文" * 10)
        self.assertEqual(merged["page_update_date"], "2026-10-22")
        self.assertEqual(merged["skill_tags"], ["B端产品"])
        self.assertEqual(merged["link_verified_at"], "2026-10-22T08:00:00")
        # 派生语义按模型重算
        self.assertEqual(merged["post_type"], "agency")
        self.assertEqual(merged["recruit_type"], "intern")
        self.assertEqual(merged["active_state"], "active")
        self.assertEqual(merged["title_key"], "ai产品经理")

    def test_record_exposes_is_direct_social(self):
        record = self.model.JobRecord({"company_name": "闪念科技", "job_type": ""})
        self.assertTrue(record.is_direct_social)
        record = self.model.JobRecord({"company_name": "闪念科技", "job_type": "4"})
        self.assertFalse(record.is_direct_social)

    def test_list_payload_stamps_link_verified_at(self):
        """P6：列表侧建记录自带 link_verified_at，可解析且带时区。"""
        from datetime import datetime
        raw = {"jobName": "AI产品经理", "encryptJobId": "ejid"}
        row = self.model.JobRecord.from_list_payload(raw).to_dict()
        stamp = row.get("link_verified_at") or ""
        self.assertTrue(stamp, "列表记录必须自带 link_verified_at")
        parsed = datetime.fromisoformat(stamp)
        self.assertIsNotNone(parsed.tzinfo, "必须带时区，P13 才能核对时效")

    def test_list_payload_explicit_empty_keeps_old_shape(self):
        """传空串则不设戳（兼容旧用例/回放）。"""
        raw = {"jobName": "AI产品经理", "encryptJobId": "ejid"}
        row = self.model.JobRecord.from_list_payload(
            raw, link_verified_at="").to_dict()
        self.assertNotIn("link_verified_at", row)

    # ---------- 其它模块不得重复实现记录级语义 ----------
    def test_other_modules_do_not_reimplement_record_semantics(self):
        """记录级语义**只有模型一处定义**（杜绝双轨）。

        允许"转发式包装"（函数体直接调用 job_model），不允许**另写一份实现**。
        """
        delegating = {
            "semantics.py": [
                ("classify_post_type", "classify_recruit_type", "is_agency_like",
                 "normalize_title", "active_state"),
            ],
            "job_summary.py": [
                ("parse_salary_monthly",),
            ],
        }
        for filename, names in delegating.items():
            source = (SCRIPTS / filename).read_text(encoding="utf-8")
            for name in names:
                match = re.search(rf"^def {name}\(", source, re.MULTILINE)
                if match is None:
                    continue
                # 取函数体（到下一个顶层 def 为止）
                body = source[match.start():]
                next_def = re.search(r"^def ", body[1:], re.MULTILINE)
                if next_def:
                    body = body[:next_def.start() + 1]
                self.assertIn(
                    "_model.", body,
                    f"{filename}.{name}() 必须转发给 job_model（不得自己实现一份）")


if __name__ == "__main__":
    unittest.main()
