"""验收流水线：需求 → 验收性质 → 反例 → 独立执行 → 结论留痕。

这个模块解决的是**根因**（2026-10-22 追到第 6 层）：
「没有独立于实现、且能重复执行的验收流水线」。
再加检查脚本只能治当下的症状；有了流水线，**下一次漏检会被同一条流水线抓住**。

三个入口：

``python scripts/audit_pipeline.py self-check [产物.json ...]``
    自检：跑验收器 + 需求/性质/反例的覆盖矩阵 → 结论可写进产物 meta。
    这是"以后我能自己来自检"的入口。

``python scripts/audit_pipeline.py matrix``
    打印需求 → 性质 → 反例的对应关系，**缺口**（某需求没有性质覆盖、
    某性质没有反例）直接列出来。

``python scripts/audit_pipeline.py brief <kind>``
    打印标准化的子代理审查 brief（数据可用性 / 实现正确性 / 验收机制 /
    交付物验收）。brief 是**数据**（在 data/audit_briefs/ 下），不是每次现编，
    这样审查方式本身可被 review、可被改进。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
REQUIREMENTS_PATH = os.path.join(_ROOT, "data", "audit_requirements.json")
SPEC_PATH = os.path.join(_ROOT, "data", "acceptance_spec.json")
BRIEF_DIR = os.path.join(_ROOT, "data", "audit_briefs")
COUNTEREXAMPLE_TEST = os.path.join("tests", "test_acceptance_counterexamples.py")


def load_requirements(path=None):
    with open(path or REQUIREMENTS_PATH, encoding="utf-8") as handle:
        return json.load(handle)


def load_spec(path=None):
    with open(path or SPEC_PATH, encoding="utf-8") as handle:
        return json.load(handle)


def spec_property_ids(spec):
    """规格里声明的性质 id 集合。"""
    ids = set()
    for prop in spec.get("properties") or []:
        if prop.get("id"):
            ids.add(prop["id"])
    return ids


def counterexample_ids(repo_root=None):
    """反例测试里出现的性质 id（从测试源码里扫出来）。

    反例文件里每个反例都写成 ``# Pxx: 说明`` 注释 → 这里按注释扫，
    避免"测试文件在但没覆盖某性质"这种假覆盖。
    """
    root = repo_root or _ROOT
    path = os.path.join(root, COUNTEREXAMPLE_TEST)
    if not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    return set(re.findall(r"#\s*(P\d+[a-zA-Z0-9_]*)\s*[:：]", text))


def coverage_matrix(repo_root=None):
    """需求 → 性质 → 反例 的对应关系 + 缺口。

    Returns:
        ``(rows, gaps)``：
        - rows: 每条需求一行（需求 id / 问题 / 覆盖的性质 / 有反例的性质 / 缺口标记）
        - gaps: 缺口清单（人类可读）
    """
    requirements = load_requirements()
    spec = load_spec()
    known = spec_property_ids(spec)
    counterexamples = counterexample_ids(repo_root)
    rows, gaps = [], []
    for req in requirements.get("requirements") or []:
        props = [p for p in (req.get("properties") or [])]
        covered = [p for p in props if p in known]
        missing = [p for p in props if p not in known]
        with_counter = [p for p in covered if p in counterexamples]
        without_counter = [p for p in covered if p not in counterexamples]
        rows.append({
            "id": req.get("id"), "ask": req.get("ask"),
            "properties": props, "covered": covered,
            "missing_in_spec": missing,
            "with_counterexample": with_counter,
            "without_counterexample": without_counter,
        })
        if not props:
            gaps.append(f"{req.get('id')}：没有挂任何验收性质（需求没人管）")
        for prop in missing:
            gaps.append(f"{req.get('id')}：性质 {prop} 在规格里不存在")
        for prop in without_counter:
            gaps.append(f"{req.get('id')}：性质 {prop} 没有反例（等于没写）")
    # 规格里有、但没有任何需求挂的性质
    used = {p for row in rows for p in row["covered"]}
    for prop in sorted(known - used):
        gaps.append(f"性质 {prop} 没有对应需求（可能是孤儿性质）")
    return rows, gaps


def run_counterexample_suite(repo_root=None):
    """跑反例测试；返回 ``(passed, output_tail)``。"""
    root = repo_root or _ROOT
    test_path = os.path.join(root, COUNTEREXAMPLE_TEST)
    if not os.path.exists(test_path):
        return None, "反例测试文件不存在"
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "tests.test_acceptance_counterexamples"],
        cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace")
    tail = "\n".join((proc.stdout + proc.stderr).splitlines()[-6:])
    return proc.returncode == 0, tail


def self_check(paths=None, repo_root=None, write_into=None):
    """自检：验收器 + 覆盖矩阵 + 反例套件。

    Args:
        paths: 要验收的产物 json（可多个）。
        write_into: 把结论写进这个产物的 meta（``meta.acceptance``）。

    Returns:
        ``(ok, report_dict)``。
    """
    root = repo_root or _ROOT
    try:
        from scripts import acceptance as acc
    except ImportError:  # pragma: no cover - 直接运行脚本时走此分支
        sys.path.insert(0, os.path.join(root, "scripts"))
        import acceptance as acc

    rows, gaps = coverage_matrix(root)
    suite_ok, suite_tail = run_counterexample_suite(root)

    results = []
    for path in paths or []:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        ok, findings = acc.check_export(payload)
        results.append({"path": os.path.basename(path), "ok": ok, "findings": findings})

    ok = all(item["ok"] for item in results) and (suite_ok is True) and not gaps
    report = {
        "ok": ok,
        "requirements": len(rows),
        "coverage_gaps": gaps,
        "counterexample_suite_ok": suite_ok,
        "counterexample_suite_output": suite_tail,
        "products": results,
        "generated_at": None,
    }
    if write_into and os.path.exists(write_into):
        with open(write_into, encoding="utf-8") as handle:
            payload = json.load(handle)
        if isinstance(payload, dict):
            payload["acceptance"] = {
                "ok": ok,
                "coverage_gaps": gaps,
                "counterexample_suite_ok": suite_ok,
                "products": [{"path": r["path"], "ok": r["ok"],
                              "error_count": sum(1 for f in r["findings"]
                                                 if f["level"] == "error"),
                              "warn_count": sum(1 for f in r["findings"]
                                                if f["level"] == "warn")}
                             for r in results],
            }
            from scripts import export_contract as ec
            ec._atomic_write_export_json(write_into, payload)
    return ok, report


def format_matrix(rows, gaps):
    lines = ["需求 → 性质 → 反例 覆盖矩阵", "=" * 78]
    for row in rows:
        lines.append(f"{row['id']}  {row['ask']}")
        lines.append(f"     性质：{', '.join(row['covered']) or '（无）'}")
        counter = ", ".join(row["with_counterexample"]) or "（无）"
        lines.append(f"     有反例：{counter}")
        if row["without_counterexample"]:
            lines.append(f"     ⚠️ 缺反例：{', '.join(row['without_counterexample'])}")
        if row["missing_in_spec"]:
            lines.append(f"     ⚠️ 规格里没有：{', '.join(row['missing_in_spec'])}")
    lines.append("")
    if gaps:
        lines.append(f"缺口 {len(gaps)} 处：")
        lines.extend(f"  · {gap}" for gap in gaps)
    else:
        lines.append("✅ 没有缺口")
    return "\n".join(lines)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__)
        return 2
    command = argv[0]
    if command == "matrix":
        rows, gaps = coverage_matrix()
        print(format_matrix(rows, gaps))
        return 1 if gaps else 0
    if command == "brief":
        if len(argv) < 2:
            print("用法：audit_pipeline.py brief <kind>")
            return 2
        kind = argv[1]
        requirements = load_requirements()
        kinds = {k.get("id"): k for k in requirements.get("audit_kinds") or []}
        if kind not in kinds:
            print(f"未知 kind：{kind}；可用：{', '.join(kinds)}")
            return 2
        path = os.path.join(_ROOT, kinds[kind]["brief"])
        if not os.path.exists(path):
            print(f"brief 文件不存在：{path}")
            return 2
        with open(path, encoding="utf-8") as handle:
            print(handle.read())
        return 0
    if command == "self-check":
        paths = [a for a in argv[1:] if not a.startswith("--")]
        ok, report = self_check(paths)
        print("=== 验收流水线自检 ===")
        print(f"需求条数：{report['requirements']}")
        print(f"反例套件：{'通过' if report['counterexample_suite_ok'] else '未通过/缺失'}")
        if report["counterexample_suite_output"]:
            print("  " + report["counterexample_suite_output"].replace("\n", "\n  "))
        print(f"覆盖缺口：{len(report['coverage_gaps'])} 处")
        for gap in report["coverage_gaps"][:10]:
            print(f"  · {gap}")
        for item in report["products"]:
            errors = sum(1 for f in item["findings"] if f["level"] == "error")
            warns = sum(1 for f in item["findings"] if f["level"] == "warn")
            print(f"  {item['path']}：{'通过' if item['ok'] else '未通过'}"
                  f"（error {errors} / warn {warns}）")
            for finding in item["findings"][:8]:
                mark = "❌" if finding["level"] == "error" else "⚠️"
                print(f"     {mark} {finding['id']}: {finding['detail']}")
        print("结论：", "通过 ✅" if ok else "未通过 ❌")
        return 0 if ok else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
