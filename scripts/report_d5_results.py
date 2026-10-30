import json
import sys
from pathlib import Path

R = Path("/home/lee/sre-project")
sys.path[:0] = [str(R), str(R / "backend")]
from scripts.archive_d5_evidence import public_budgets

W = R / "evaluation/d5-runtime"
retests = json.loads((W / "original-transport-retests/results.json").read_text())
mapping = {r["original_result"]: r["new_result"] for r in retests}
tables = {}
for group in ("matrix", "followup"):
    state = json.loads((W / group / "state.json").read_text())
    assert state["status"] == "finished"
    for view in ("original", "linked_retest"):
        groups = {}
        for e in state["tasks"].values():
            assert e["status"] == "finished"
            original = e["result_id"]
            ident = (
                mapping.get(original, original) if view == "linked_retest" else original
            )
            r = json.loads((W / "results" / (ident + ".json")).read_text())
            g = groups.setdefault(
                r["variant"], {"n": 0, "valid": 0, "correct": 0, "attempts": 0, "tokens": 0}
            )
            g["n"] += 1
            g["valid"] += bool(r["output_valid"])
            g["correct"] += r.get("scoring", {}).get("disposition_correct") is True
            g["attempts"] += r["provider_http_attempts"]
            g["tokens"] += r["known_total_tokens"]
        tables[group + "/" + view] = groups
_, budget = public_budgets(R)
out = {
    "tables": tables,
    "retest_links": retests,
    "all_attempts": budget["used_slots"],
    "known_tokens": budget["known_total_tokens"],
    "unknown_usage_attempts": budget["unknown_usage_attempts"],
    "cost_amount": None,
    "production_choice": "Retain D4; no unproven candidate becomes default",
    "C1_excluded": True,
    "D6_not_run": True,
}
(W / "final-results.json").write_text(json.dumps(out, indent=2))
lines = [
    "# D5 结果与采纳结论",
    "",
    "本报告为开发/验证集探索筛查；正式留出和每案例三次重复属于D6，未执行、未削减。原思考协议与直连出口；C1已停止且不混入。",
    "",
]
for name, groups in tables.items():
    lines += [
        "## " + name,
        "",
        "|方案|案例数|格式有效|处置正确|调用|已知token|",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for variant, g in sorted(groups.items()):
        lines.append(
            f"|{variant}|{g['n']}|{g['valid']}|{g['correct']}|{g['attempts']}|{g['tokens']}|"
        )
    lines += [""]
lines += [
    "原始视图与关联补测视图并列；后者不增加独立案例数，也不抹除原失败。真实花费须按全部账本统计，不能按补测视图的调用数结算。处置正确含正确转人工，不等同业务恢复。",
    "",
    f"累计实际请求{budget['used_slots']}，已知token {budget['known_total_tokens']}，usage未知请求{budget['unknown_usage_attempts']}，金额未知。",
    "",
    "## 实机与采纳",
    "",
    "- dev_09：B3和H5均实际恢复并通过120秒/120请求/12检查；调用3与0。仅单例，不宣称恢复率提高。",
    "- dev_11：H5模型回退调用3次后正确转人工；未恢复业务，完整评分仍失败，清理通过。",
    "- 保留D4生产默认、逐次人工审批和完整执行/业务保护。H5保留为实验候选，H4索引不采纳；H1暂无明确增量收益。",
    "- H2a假成功控制通过；H2b反馈再规划未实现、不采纳、不宣称提升恢复。H6缺独立版本检索与泄漏审计；H7–H9未满足瓶颈证据条件，均不采纳。以上为原计划候选采纳决定，不等于这些功能已实现。",
    "- 原始/紧凑视图有信息删减及工具截断，不能宣称无损；准确率与token须联合比较。",
    "",
    "## 复现与边界",
    "",
    "固定源版本/镜像、计划哈希、快照与原始轨迹在独立D盘证据包。模型私钥不归档；外部模型非确定性，不保证逐字复现。",
    "原始矩阵76项、原思考筛选28项、传输关联补测4项分别保留。C1中断/失败和所有现场环境失败同样保留。",
    "直连服务为本机部署适配，重启需运行scripts/restore_d5_direct_proxy.py；无开机自启保证。数据库地址与探针恢复使用scripts/restore_d5_demo_addresses.py；受限Kubernetes令牌24小时到期需重新签发。",
    "最终验收另以D5_ACCEPTANCE.md和交付状态为准；本报告不自行宣布CI或归档完成。",
]
text = "\n".join(lines) + "\n"
for p in (R / "docs/D5_RESULTS.md", Path("/mnt/d/uclaude/D5_RESULTS.md")):
    p.write_text(text)
print(json.dumps({"tables": tables, "calls": budget["used_slots"]}))
