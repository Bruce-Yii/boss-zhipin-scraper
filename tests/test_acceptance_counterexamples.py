"""验收检查器的**反例测试**——本次改造的核心交付物（2026-10-22 独立审计）。

为什么它是核心：**没有反例的性质等于没写**。
独立审计构造了 17 条单缺陷记录（重复 job_id、post_type=direct 却带 proxy_job=1、
recruit_type=social 却 job_type=4、job_valid_status=0 却 active_state=active、
link_verified_at 是垃圾串、薪资面议、社招用日薪口径、company 信息全空、
job_count 与实际不符、skill_tags 类型漂移、同公司同岗两条未标重复……），
原版验收器**全部放行**。本文件逐条构造"应该变红"的记录，断言对应性质真的红了；
再构造"应该全绿"的正常记录，断言不红。

命名：``test_cNN_...`` 对应审计清单里的第 N 类缺陷；每条断言都指名它应该由哪条性质
抓住——抓不住就是漏网。
"""

import ast
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:      # 让 `from scripts import ...` 可用
    sys.path.insert(0, str(ROOT))


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _md5_id(link):
    return hashlib.md5(str(link).encode()).hexdigest()[:16]


def _now():
    return datetime.now(UTC).isoformat(timespec="seconds")


def _ago(hours):
    return (datetime.now(UTC) - timedelta(hours=hours)).isoformat(
        timespec="seconds")


_LINK = "https://www.zhipin.com/job_detail/8f4496c59c3260d8.html"
_GOOD_JD = "岗位职责：负责 AI 产品需求分析与设计，推动版本交付与迭代。" * 5
_OTHER_JD = "看清楚再投简历，谢谢【招募小红书运营合伙人｜股权合作】我们正在做内容。" * 6


def _record(**over):
    """一条**应该全绿**的记录（2026-10-22 形态：平台直招社招 AI 产品经理）。"""
    link = over.pop("job_link", _LINK)
    base = {
        "job_id": over.pop("job_id", _md5_id(link)),
        "title": "AI产品经理",
        "title_key": "ai产品经理",
        "company_name": "闪念科技",
        "company_industry": "互联网",
        "company_scale": "100-499人",
        "company_stage": "A轮",
        "company_link": "https://www.zhipin.com/gongsi/f409f37f83a6135b0nV_2d25EA~~.html",
        "salary": "20-30K",
        "salary_source": "api",
        "salary_min": "20000",
        "salary_max": "30000",
        "salary_unit": "month",
        "months_per_year": "12",
        "location": "杭州·余杭区·仓前",
        "job_link": link,
        "jd": _GOOD_JD,
        "post_type": "direct",
        "recruit_type": "social",
        "active_state": "active",
        "job_valid_status": "1",
        "boss_active_status": "今日活跃",
        "boss_title": "HR",
        "boss_name": "闪念科技",
        "proxy_job": "",
        "proxy_type": "",
        "job_type": "",
        "link_verified_at": _now(),
        "page_update_date": "2026-10-22",
        "skill_tags": ["B端产品"],
        "skills": [],
        "direction_evidence": ["AI", "产品经理"],
        "is_ai_pm": True,
        "direction": "ai_pm",
        "tags": "3-5年 | 本科",
        "experience": "3-5年",
        "education": "本科",
        "job_labels": "3-5年 | 本科",
        "welfare": "五险一金",
        "icon_flags": "",
        "icon_word": "",
        "anonymous": "0",
        "dup_group": "",
        "dup_size": 1,
        "is_primary": True,
    }
    base.update(over)
    return base


def _payload(records, **meta):
    """一个完整的导出产物（meta 与 jobs 平级，和真实导出同构）。"""
    payload = {
        "format_version": 2,
        "keyword": "AI产品经理",
        "city": "杭州",
        "job_count": len(records),
        "total": len(records),
        "jobs": list(records),
    }
    payload.update(meta)
    return payload


class AcceptanceCounterexampleTests(unittest.TestCase):
    """逐条反例：每条都指名应该由哪条性质抓住。"""

    def setUp(self):
        self.acc = load_module("acceptance", SCRIPTS / "acceptance.py")
        self.spec = self.acc.load_spec()

    # -- 工具 --------------------------------------------------------
    def _check(self, payload):
        ok, findings = self.acc.check_export(payload, self.spec)
        return ok, findings

    def _assert_red(self, findings, prop_id, level="error"):
        hits = [f for f in findings if f["id"] == prop_id and f["level"] == level]
        self.assertTrue(
            hits,
            f"{prop_id} 应该以 {level} 报出来，实际 findings："
            + json.dumps(findings, ensure_ascii=False)[:2000])
        return hits

    def _assert_green(self, findings):
        errors = [f for f in findings if f["level"] == "error"]
        self.assertFalse(
            errors,
            "不该有 error，实际：" + json.dumps(errors, ensure_ascii=False)[:2000])

    # -- 审计清单：17 条单缺陷记录 ------------------------------------
    def test_c01_duplicate_job_id_is_red(self):
        """第 1 类：重复 job_id（同一身份出现两次）。"""
        a = _record(job_id="0123456789abcdef")
        b = _record(job_id="0123456789abcdef")
        ok, findings = self._check(_payload([a, b]))
        self.assertFalse(ok)
        self._assert_red(findings, "P13_identity_freshness_credentials")

    def test_c02_direct_but_proxy_job_is_red(self):
        """第 2 类：post_type=direct 却带 proxy_job=1（平台说它是代发）。"""
        job = _record(post_type="direct", proxy_job="1")
        ok, findings = self._check(_payload([job]))
        self.assertFalse(ok)
        self._assert_red(findings, "P12_semantic_cross_consistency")

    def test_c03_social_but_job_type_4_is_red(self):
        """第 3 类：recruit_type=social 却 job_type=4（平台说它是实习）。"""
        job = _record(recruit_type="social", job_type="4")
        ok, findings = self._check(_payload([job]))
        self.assertFalse(ok)
        self._assert_red(findings, "P12_semantic_cross_consistency")

    def test_c03b_intern_without_job_type_tag_is_not_flagged(self):
        """空标记 + 标题自称实习 → 实习，不算矛盾（issue #159；与模型同口径）。"""
        job = _record(title="AI产品经理实习生", title_key="ai产品经理实习生",
                      recruit_type="intern", job_type="",
                      salary="150-200元/天", salary_min="150",
                      salary_max="200", salary_unit="day",
                      tags="实习 | 本科", experience="")
        ok, findings = self._check(_payload([job]))
        bad = [f for f in findings
               if f["id"] == "P12_semantic_cross_consistency"
               and "recruit_type 与 job_type 矛盾" in str(f.get("detail", ""))]
        self.assertEqual(bad, [],
                         f"空标记实习岗不应报 P12：{json.dumps(findings, ensure_ascii=False)[:1000]}")

    def test_c04_valid_status_0_but_active_is_red(self):
        """第 4 类：job_valid_status=0 却 active_state=active。"""
        job = _record(job_valid_status="0", active_state="active")
        ok, findings = self._check(_payload([job]))
        self.assertFalse(ok)
        self._assert_red(findings, "P12_semantic_cross_consistency")

    def test_c05_garbage_link_verified_at_is_red(self):
        """第 5 类：link_verified_at 是垃圾串。"""
        job = _record(link_verified_at="前天那个点")
        ok, findings = self._check(_payload([job]))
        self.assertFalse(ok)
        self._assert_red(findings, "P13_identity_freshness_credentials")

    def test_c06_negotiable_salary_with_numeric_columns_is_red(self):
        """第 6 类：薪资"面议"却带着结构化薪资列（面议与数字口径矛盾）。"""
        job = _record(salary="面议", salary_min="15000", salary_max="20000")
        ok, findings = self._check(_payload([job]))
        self.assertFalse(ok)
        self._assert_red(findings, "P12_semantic_cross_consistency")

    def test_c07_social_with_daily_salary_is_red(self):
        """第 7 类：社招用日薪口径（口径混装，Excel 排序直接错位）。"""
        job = _record(recruit_type="social", salary="300-500元/天",
                      salary_unit="day", salary_min="6600", salary_max="11000")
        ok, findings = self._check(_payload([job]))
        self.assertFalse(ok)
        self._assert_red(findings, "P12_semantic_cross_consistency")

    def test_c08_company_info_all_empty_is_red(self):
        """第 8 类：company 信息全空（消费者问「这家公司是谁」得不到答案）。"""
        job = _record(company_name="", company_industry="", company_scale="",
                      company_stage="", company_link="")
        ok, findings = self._check(_payload([job]))
        self.assertFalse(ok)
        self._assert_red(findings, "P1_required_non_empty")

    def test_c09_job_count_mismatch_is_red(self):
        """第 9 类：meta.job_count 与实际条数不符。"""
        job = _record()
        payload = _payload([job], job_count=30)
        ok, findings = self._check(payload)
        self.assertFalse(ok)
        self._assert_red(findings, "P11_delivery_matches_request")

    def test_c10_skill_tags_type_drift_is_red(self):
        """第 10 类：skill_tags 类型漂移（list[str] 漂成字符串）。"""
        job = _record(skill_tags="B端产品")
        ok, findings = self._check(_payload([job]))
        self.assertFalse(ok)
        self._assert_red(findings, "P2_type_stable")

    def test_c11_same_company_same_role_not_flagged_duplicate_is_red(self):
        """第 11 类：同公司同岗两条（薪资带被微调）却没标重复。"""
        a = _record(job_id=_md5_id("https://www.zhipin.com/job_detail/aaa.html"),
                    job_link="https://www.zhipin.com/job_detail/aaa.html",
                    salary="11-20K")
        b = _record(job_id=_md5_id("https://www.zhipin.com/job_detail/bbb.html"),
                    job_link="https://www.zhipin.com/job_detail/bbb.html",
                    salary="12-20K")
        ok, findings = self._check(_payload([a, b]))
        self.assertFalse(ok)
        self._assert_red(findings, "P3_duplicates_are_flagged")

    def test_c12_keyword_hit_rate_too_low_is_red(self):
        """第 12 类：搜「AI产品经理」回来的东西大半不命中（最大的洞）。"""
        jobs = [_record()]
        for index in range(3):
            link = f"https://www.zhipin.com/job_detail/off{index}.html"
            jobs.append(_record(job_id=_md5_id(link), job_link=link,
                                title="餐饮产品经理", title_key="餐饮产品经理"))
        ok, findings = self._check(_payload(jobs))
        self.assertFalse(ok)
        hits = self._assert_red(findings, "P11_delivery_matches_request")
        self.assertIn("命中", hits[0]["detail"])

    def test_c13_city_mismatch_is_red(self):
        """第 13 类：记录城市与请求城市不一致。"""
        job = _record(location="上海·浦东新区·世纪大道")
        ok, findings = self._check(_payload([job], city="杭州"))
        self.assertFalse(ok)
        self._assert_red(findings, "P11_delivery_matches_request")

    def test_c14_unparsable_keyword_is_reported_as_cannot_verify(self):
        """第 14 类：meta.keyword 不可机器解析 → 必须明说「无法核对」，不能静默放过。"""
        job = _record()
        ok, findings = self._check({"jobs": [job]})
        hits = self._assert_red(findings, "P11_delivery_matches_request", "warn")
        self.assertIn("无法核对", hits[0]["detail"])

    def test_c15_agency_label_without_evidence_is_red(self):
        """第 15 类：标了猎头代发却没有任何平台证据。"""
        job = _record(post_type="agency", proxy_job="", proxy_type="",
                      boss_title="HR")
        ok, findings = self._check(_payload([job]))
        self.assertFalse(ok)
        self._assert_red(findings, "P12_semantic_cross_consistency")

    def test_c16_active_state_contradicts_platform_is_red(self):
        """第 16 类：active_state=active 但平台没给任何活跃信息。"""
        job = _record(active_state="active", boss_active_status="")
        ok, findings = self._check(_payload([job]))
        self.assertFalse(ok)
        self._assert_red(findings, "P12_semantic_cross_consistency")

    def test_c17_title_key_mismatch_is_red(self):
        """第 17 类：title_key 与岗位名对不上（分组/匹配会落到错误的组）。"""
        job = _record(title_key="产品经理")
        ok, findings = self._check(_payload([job]))
        self.assertFalse(ok)
        self._assert_red(findings, "P12_semantic_cross_consistency")

    # -- 追加类别（新增性质带来的）-----------------------------------
    def test_c18_illegal_job_type_is_red(self):
        job = _record(job_type="7")
        ok, findings = self._check(_payload([job]))
        self.assertFalse(ok)
        self._assert_red(findings, "P12_semantic_cross_consistency")

    def test_c19_unparsable_salary_is_red(self):
        job = _record(salary="若干", salary_source="api")
        ok, findings = self._check(_payload([job]))
        self.assertFalse(ok)
        self._assert_red(findings, "P12_semantic_cross_consistency")

    def test_c20_credential_in_url_is_red(self):
        """URL 里带 securityId / token → 红线，必须被抓出来。"""
        link = "https://www.zhipin.com/job_detail/abc.html?securityId=SECRET123"
        job = _record(job_link=link, job_id=_md5_id(link))
        ok, findings = self._check(_payload([job]))
        self.assertFalse(ok)
        hits = self._assert_red(findings, "P13_identity_freshness_credentials")
        self.assertTrue(any("securityId" in h["detail"] for h in hits),
                        f"应点名 securityId：{hits}")

    def test_c21_two_day_old_link_is_ok(self):
        job = _record(link_verified_at=_ago(48))
        ok, findings = self._check(_payload([job]))
        self.assertTrue(ok)
        stale = [f for f in findings
                 if f.get("id") == "P13_identity_freshness_credentials"
                 and "超过" in str(f.get("detail"))]
        self.assertEqual(stale, [])

    def test_c21b_link_older_than_7_days_is_warn(self):
        job = _record(link_verified_at=_ago(24 * 8))
        ok, findings = self._check(_payload([job]))
        self.assertTrue(ok)
        hits = self._assert_red(findings, "P13_identity_freshness_credentials",
                                "warn")
        self.assertIn("7 天", hits[0]["detail"])

    def test_c22_naive_timestamp_is_warn_not_silent(self):
        job = _record(link_verified_at="2026-10-20T10:00:00")
        ok, findings = self._check(_payload([job]))
        hits = self._assert_red(findings, "P13_identity_freshness_credentials", "warn")
        self.assertIn("时区", hits[0]["detail"])

    def test_c23_dup_size_mismatch_is_red(self):
        a = _record(job_id=_md5_id("https://www.zhipin.com/job_detail/dup1.html"),
                    job_link="https://www.zhipin.com/job_detail/dup1.html",
                    dup_group="dup-1", dup_size=3, is_primary=True)
        b = _record(job_id=_md5_id("https://www.zhipin.com/job_detail/dup2.html"),
                    job_link="https://www.zhipin.com/job_detail/dup2.html",
                    dup_group="dup-1", dup_size=3, is_primary=False)
        ok, findings = self._check(_payload([a, b]))
        self.assertFalse(ok)
        self._assert_red(findings, "P13_identity_freshness_credentials")

    def test_c24_two_primaries_in_one_group_is_red(self):
        a = _record(job_id=_md5_id("https://www.zhipin.com/job_detail/p1.html"),
                    job_link="https://www.zhipin.com/job_detail/p1.html",
                    dup_group="dup-2", dup_size=2, is_primary=True)
        b = _record(job_id=_md5_id("https://www.zhipin.com/job_detail/p2.html"),
                    job_link="https://www.zhipin.com/job_detail/p2.html",
                    dup_group="dup-2", dup_size=2, is_primary=True)
        ok, findings = self._check(_payload([a, b]))
        self.assertFalse(ok)
        self._assert_red(findings, "P13_identity_freshness_credentials")

    def test_c25_group_content_mismatch_is_red(self):
        """被标成重复的记录，内容其实不一样 → 重复组标记是假的。"""
        a = _record(job_id=_md5_id("https://www.zhipin.com/job_detail/m1.html"),
                    job_link="https://www.zhipin.com/job_detail/m1.html",
                    dup_group="dup-3", dup_size=2, is_primary=True)
        b = _record(job_id=_md5_id("https://www.zhipin.com/job_detail/m2.html"),
                    job_link="https://www.zhipin.com/job_detail/m2.html",
                    jd=_OTHER_JD, dup_group="dup-3", dup_size=2, is_primary=False)
        ok, findings = self._check(_payload([a, b]))
        self.assertFalse(ok)
        self._assert_red(findings, "P13_identity_freshness_credentials")

    def test_c26_job_id_not_derived_from_link_is_red(self):
        """job_id 必须由自己的 job_link 推出（模型里的真身定义）。"""
        job = _record(job_id="0123456789abcdef")
        ok, findings = self._check(_payload([job]))
        self.assertFalse(ok)
        self._assert_red(findings, "P13_identity_freshness_credentials")

    def test_c27_short_jd_is_red(self):
        job = _record(jd="太短")
        ok, findings = self._check(_payload([job]))
        self.assertFalse(ok)
        self._assert_red(findings, "P9_jd_not_too_short")

    def test_c28_junk_tail_is_red(self):
        job = _record(jd=_GOOD_JD + "\n薪资详情\n刚刚活跃")
        ok, findings = self._check(_payload([job]))
        self.assertFalse(ok)
        self._assert_red(findings, "P10_no_junk_tail")

    def test_c29_forbidden_internal_field_is_red(self):
        job = _record(security_id="SECRET")
        ok, findings = self._check(_payload([job]))
        self.assertFalse(ok)
        self._assert_red(findings, "P0_forbidden_fields")

    def test_c30_missing_required_field_is_red(self):
        job = _record(company_name="")
        ok, findings = self._check(_payload([job]))
        self.assertFalse(ok)
        self._assert_red(findings, "P1_required_non_empty")

    def test_c31_jd_title_mismatch_is_signal_only(self):
        """P5 是信号：能报出来，但**不能**把产物判成不合格。"""
        job = _record(jd=_OTHER_JD)
        ok, findings = self._check(_payload([job]))
        self._assert_red(findings, "P5_jd_matches_its_title", "warn")
        self._assert_green(findings)

    def test_c32_unregistered_field_is_warn(self):
        job = _record(mystery_field="没人知道这是什么")
        ok, findings = self._check(_payload([job]))
        self._assert_red(findings, "P2_no_unregistered_field", "warn")
        self._assert_green(findings)


class AcceptanceCleanDataTests(unittest.TestCase):
    """"应该全绿"的记录：不许被误判。"""

    def setUp(self):
        self.acc = load_module("acceptance", SCRIPTS / "acceptance.py")
        self.spec = self.acc.load_spec()

    def test_clean_export_has_no_error(self):
        ok, findings = self.acc.check_export(_payload([_record()]), self.spec)
        errors = [f for f in findings if f["level"] == "error"]
        self.assertFalse(
            errors, "干净数据不该有 error：" + json.dumps(errors, ensure_ascii=False))
        self.assertTrue(ok)

    def test_clean_export_reports_shape(self):
        _ok, findings = self.acc.check_export(_payload([_record()]), self.spec)
        shapes = [f for f in findings if f["id"] == "artifact_shape"]
        self.assertTrue(shapes, "结论里必须说明用的是哪一档产物形态")
        self.assertIn("详情档", shapes[0]["detail"])

    def test_list_product_skips_content_level_checks(self):
        """列表产物本来就没有 JD：不该被判"内容相同/JD 太短"。"""
        jobs = []
        for index in range(3):
            link = f"https://www.zhipin.com/job_detail/list{index}.html"
            job = _record(job_id=_md5_id(link), job_link=link, jd="",
                          title=f"AI产品经理{index}", title_key=f"ai产品经理{index}")
            jobs.append(job)
        ok, findings = self.acc.check_export(_payload(jobs), self.spec)
        ids = {f["id"] for f in findings}
        self.assertNotIn("P9_jd_not_too_short", ids)
        self.assertNotIn("P10_no_junk_tail", ids)
        self.assertNotIn("P3_duplicates_are_flagged", ids)
        shapes = [f for f in findings if f["id"] == "artifact_shape"]
        self.assertIn("列表档", shapes[0]["detail"])
        self.assertTrue(ok, json.dumps(findings, ensure_ascii=False)[:2000])

    def test_list_product_still_checks_identity_and_delivery(self):
        """分级不等于放水：列表产物照样查身份与交付一致性。"""
        link = "https://www.zhipin.com/job_detail/listdup.html"
        a = _record(job_id=_md5_id(link), job_link=link, jd="")
        b = _record(job_id=_md5_id(link), job_link=link, jd="")
        ok, findings = self.acc.check_export(_payload([a, b]), self.spec)
        self.assertFalse(ok)
        self.assertTrue(any(f["id"] == "P13_identity_freshness_credentials"
                            for f in findings))

    def test_flagged_duplicates_are_green(self):
        a = _record(job_id=_md5_id("https://www.zhipin.com/job_detail/g1.html"),
                    job_link="https://www.zhipin.com/job_detail/g1.html",
                    dup_group="dup-9", dup_size=2, is_primary=True)
        b = _record(job_id=_md5_id("https://www.zhipin.com/job_detail/g2.html"),
                    job_link="https://www.zhipin.com/job_detail/g2.html",
                    dup_group="dup-9", dup_size=2, is_primary=False)
        ok, findings = self.acc.check_export(_payload([a, b]), self.spec)
        self.assertFalse([f for f in findings if f["id"] == "P3_duplicates_are_flagged"])
        self.assertTrue(ok, json.dumps(findings, ensure_ascii=False)[:2000])


class AcceptanceStructureTests(unittest.TestCase):
    """结构性约束：性质必须来自规格、必须单一实现、不能再与实现同源。"""

    def setUp(self):
        self.acc = load_module("acceptance", SCRIPTS / "acceptance.py")
        self.spec = self.acc.load_spec()
        self.source = (SCRIPTS / "acceptance.py").read_text(encoding="utf-8")

    def test_every_property_has_verdict_and_counterexample(self):
        props = self.spec.get("properties") or []
        self.assertGreaterEqual(len(props), 12)
        for prop in props:
            self.assertTrue(prop.get("id"), "每条性质要有 id")
            self.assertTrue(prop.get("desc"), f"{prop.get('id')} 要有一句话判定")
            self.assertTrue(prop.get("counterexample"),
                            f"{prop.get('id')} 要有反例（什么样的记录应该让它变红）")
            self.assertIn(prop.get("level"), ("error", "warn", "warn/error", "error/warn"))

    def test_constant_false_p8_is_removed(self):
        ids = {p["id"] for p in (self.spec.get("properties") or [])}
        self.assertNotIn("P8_salary_source_consistent", ids)
        removed = {r["id"] for r in (self.spec.get("removed_properties") or [])}
        self.assertIn("P8_salary_source_consistent", removed)

    def test_jd_length_and_junk_tail_have_single_implementation(self):
        """P9/P10 必须委托 semantics.check_invariants，验收器里不再留第二份正则。"""
        self.assertIn("check_invariants", self.source)
        for marker in ("薪资详情", "刚刚活跃", "_JUNK_TAIL_RE", "_MIN_JD_LENGTH"):
            self.assertNotIn(marker, self.source,
                             f"验收器里不该再有垃圾尾/长度的第二份副本：{marker}")

    def test_p9_p10_actually_come_from_semantics(self):
        """把语义层的判定换成失败，P9 必须跟着红（证明是委托而不是复刻）。"""
        fake = type("FakeSemantics", (), {})()
        fake.check_invariants = lambda jobs, meta=None: {
            "jd_length": {"ok": False, "detail": "1 条 < 120 字"},
            "no_junk_tail": {"ok": True, "detail": "0 条"},
        }
        original = self.acc._SEMANTICS_MODULE
        self.acc._SEMANTICS_MODULE = fake
        try:
            _ok, findings = self.acc.check_export(_payload([_record()]), self.spec)
        finally:
            self.acc._SEMANTICS_MODULE = original
        self.assertTrue(any(f["id"] == "P9_jd_not_too_short" for f in findings),
                        json.dumps(findings, ensure_ascii=False)[:1500])

    def test_field_registry_is_owned_by_spec_not_by_implementation(self):
        """P2 的登记字段表必须来自规格，不能再取 model.FIELD_INDEX（否则同源）。"""
        self.assertIn("field_registry", self.spec)
        # AST 级别断言：运行期既没有 import job_model，也没有 FIELD_INDEX 属性访问
        tree = ast.parse(self.source)
        used = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                used.add(node.attr)
            elif isinstance(node, ast.Import):
                used.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                used.update(alias.name for alias in node.names)
        self.assertNotIn("FIELD_INDEX", used)
        self.assertNotIn("job_model", used)

    def test_credential_scan_reuses_consumer_contract_check(self):
        """凭据扫描必须复用消费端 contract_check 的实现，不另写一套。"""
        self.assertIn("contract_check", self.source)
        self.assertTrue(os.path.exists(self.acc._CONTRACT_CHECK_PATH))
        module = self.acc._contract_check_module()
        self.assertIsNotNone(module, "必须能加载消费端契约校验器")
        self.assertTrue(hasattr(module, "_structured_sensitive_keys"))

    def test_spec_covers_consumer_questions(self):
        """规格的出发点仍是"消费者要回答的问题"（含新增的"是不是这次要找的岗"）。"""
        questions = self.spec.get("questions") or []
        self.assertGreaterEqual(len(questions), 9)
        ids = {q["id"] for q in questions}
        self.assertIn("is_it_the_job_i_wanted", ids)
        for question in questions:
            self.assertTrue(question.get("ask"))
            self.assertTrue(question.get("requires"))


class AcceptanceCliTests(unittest.TestCase):
    """CLI 用法不许变：``python scripts/acceptance.py <file.json> [...]``，退出码 0/1。"""

    def setUp(self):
        self.acc = load_module("acceptance", SCRIPTS / "acceptance.py")

    def _run(self, payload):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "export.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = self.acc.main([path])
            return code, buffer.getvalue()

    def test_clean_file_exits_zero(self):
        code, output = self._run(_payload([_record()]))
        self.assertEqual(code, 0, output)

    def test_broken_file_exits_one(self):
        link = "https://www.zhipin.com/job_detail/aaa.html"
        job = _record(job_id=_md5_id(link), job_link=link, proxy_job="1")
        code, output = self._run(_payload([job]))
        self.assertEqual(code, 1, output)
        self.assertIn("P12", output)

    def test_output_states_which_shape_was_used(self):
        link = "https://www.zhipin.com/job_detail/aaa.html"
        job = _record(job_id=_md5_id(link), job_link=link, jd="")
        _code, output = self._run(_payload([job]))
        self.assertIn("列表档", output)


if __name__ == "__main__":
    unittest.main()


class MissingCoverageTests(unittest.TestCase):
    """补齐覆盖矩阵报出的缺口：R01 的 P6/P7、R02 的 P0_empty、R08 的 P4。

    矩阵（``scripts/audit_pipeline.py matrix``）会报"某性质没有反例 = 等于没写"，
    这里把缺的补上，让矩阵归零。
    """

    def setUp(self):
        self.acc = load_module("acceptance", SCRIPTS / "acceptance.py")
        self.spec = self.acc.load_spec()

    def _check(self, payload):
        ok, findings = self.acc.check_export(payload, self.spec)
        return ok, findings

    def test_missing_semantic_label_is_flagged(self):
        """P7_semantic_labels_present：语义标签缺失 → 必须被指出。"""
        record = _record()
        record.pop("active_state", None)
        ok, findings = self._check(_payload([record]))
        self.assertTrue(
            any(f["id"] == "P7_semantic_labels_present" for f in findings),
            f"缺语义标签必须被报出来：{findings}")

    def test_one_id_two_links_is_flagged(self):
        """P4_link_belongs_to_record：同一 job_id 挂在多条 job_link 上 → 必须被指出。"""
        a = _record(job_id="a1", job_link="https://x/a.html")
        b = _record(job_id="a1", job_link="https://x/b.html")
        ok, findings = self._check(_payload([a, b]))
        self.assertTrue(
            any(f["id"] == "P4_link_belongs_to_record" for f in findings),
            f"一个 id 挂多条链接必须被报出来：{findings}")

    def test_job_id_not_matching_link_is_flagged(self):
        """P13：job_id 与 md5(job_link)[:16] 对不上 → 必须被指出（模型真身定义）。"""
        record = _record(job_id="deadbeefdeadbeef")
        ok, findings = self._check(_payload([record]))
        self.assertTrue(
            any(f["id"] == "P13_identity_freshness_credentials" for f in findings),
            f"job_id 与链接对不上必须被报出来：{findings}")

    def test_missing_link_verified_at_is_flagged(self):
        """P6_link_verified：缺链接校验时刻 → 必须被指出。"""
        record = _record()
        record.pop("link_verified_at", None)
        ok, findings = self._check(_payload([record]))
        self.assertTrue(
            any(f["id"] == "P6_link_verified" for f in findings),
            f"缺 link_verified_at 必须被报出来：{findings}")

    def test_empty_product_is_flagged(self):
        """P0_empty：产物里没有记录 → 必须被指出。"""
        ok, findings = self._check(_payload([]))
        self.assertFalse(ok)
        self.assertTrue(any(f["id"] == "P0_empty" for f in findings),
                        f"空产物必须被报出来：{findings}")
