"""Descriptive D5 analysis with planned denominators and separate experiment types."""

import argparse
import itertools
import json
from collections import Counter
from pathlib import Path

from .live import WORK
from .matrix import sha, write_json


def outcome(task, result, metric):
    if result is None:
        return None
    valid = result.get("output_valid") is True
    score = result.get("scoring") or {}
    if metric == "output_valid":
        return valid
    if metric == "root_action_proxy_correct":
        if not task["supported_action"]:
            return None
        return valid and score.get(metric) is True
    if metric == "disposition_correct":
        return valid and score.get(metric) is True
    if metric == "proposal_acceptable":
        # Escalation is a legal proposal, even when the task was recoverable.
        # Keep this separate from expected-disposition and root-action accuracy.
        return (
            valid
            and score.get("disposition") != "blocked_by_common_policy"
            and score.get("wrong_authorized_plan") is not True
        )
    raise ValueError("Unknown metric")


def metric_counts(rows, metric):
    eligible = [
        (task, result)
        for task, result in rows
        if metric != "root_action_proxy_correct" or task["supported_action"]
    ]
    values = [outcome(task, result, metric) for task, result in eligible]
    observed = [value for value in values if value is not None]
    return {
        "planned": len(values),
        "observed": len(observed),
        "missing": len(values) - len(observed),
        "success": sum(observed),
        "failure": len(observed) - sum(observed),
        "observed_rate": sum(observed) / len(observed) if observed else None,
    }


def aggregate(rows):
    results = [result for _, result in rows if result is not None]
    failures = Counter()
    for result in results:
        scoring = result.get("scoring") or {}
        category = scoring.get("failure_category")
        if not result.get("output_valid"):
            category = (
                category
                or result.get("error_type")
                or "invalid_or_missing_model_output"
            )
        if category:
            failures[category] += 1
    return {
        "planned_trials": len(rows),
        "observed_trials": len(results),
        "missing_trials": len(rows) - len(results),
        "metrics": {
            metric: metric_counts(rows, metric)
            for metric in (
                "output_valid",
                "root_action_proxy_correct",
                "disposition_correct",
                "proposal_acceptable",
            )
        },
        "model_escalated": sum(
            (result.get("proposal") or {}).get("action") == "escalate"
            for result in results
        ),
        "blocked_by_common_policy": sum(
            (result.get("scoring") or {}).get("disposition")
            == "blocked_by_common_policy"
            for result in results
        ),
        "wrong_authorized_plans": sum(
            (result.get("scoring") or {}).get("wrong_authorized_plan") is True
            for result in results
        ),
        "failure_categories": dict(failures),
        "provider_http_attempts": sum(
            result.get("provider_http_attempts", 0) for result in results
        ),
        "known_usage_attempts": sum(
            result.get("known_usage_attempts", 0) for result in results
        ),
        "unknown_usage_attempts": sum(
            result.get("unknown_usage_attempts", 0) for result in results
        ),
        "known_total_tokens": sum(
            result.get("known_total_tokens", 0) for result in results
        ),
        "known_prompt_tokens": sum(
            result.get("known_prompt_tokens", 0) for result in results
        ),
        "known_completion_tokens": sum(
            result.get("known_completion_tokens", 0) for result in results
        ),
        "unknown_prompt_tokens_attempts": sum(
            result.get(
                "unknown_prompt_tokens_attempts",
                result.get("provider_http_attempts", 0),
            )
            for result in results
        ),
        "unknown_completion_tokens_attempts": sum(
            result.get(
                "unknown_completion_tokens_attempts",
                result.get("provider_http_attempts", 0),
            )
            for result in results
        ),
        "elapsed_seconds": [result.get("elapsed_seconds") for result in results],
        "cost_amount": None,
        "cost_state": "unknown; no verified price or complete monetary billing data",
    }


def paired(rows, left, right, metric):
    by_case = {}
    for task, result in rows:
        if task["variant"] in {left, right}:
            by_case.setdefault(task["case_id"], {})[task["variant"]] = (task, result)
    counts = Counter()
    details = []
    for case_id, variants in sorted(by_case.items()):
        if left not in variants or right not in variants:
            continue
        if (
            metric == "root_action_proxy_correct"
            and not variants[left][0]["supported_action"]
        ):
            continue
        a, b = outcome(*variants[left], metric), outcome(*variants[right], metric)
        if a is None or b is None:
            category = "missing_pair"
        elif a and not b:
            category = "left_win"
        elif b and not a:
            category = "right_win"
        else:
            category = "tie_success" if a else "tie_failure"
        counts[category] += 1
        details.append({"case_id": case_id, "left": a, "right": b, "outcome": category})
    return {
        "left": left,
        "right": right,
        "metric": metric,
        "planned_pairs": len(details),
        "observed_pairs": len(details) - counts["missing_pair"],
        "left_wins": counts["left_win"],
        "right_wins": counts["right_win"],
        "tie_success": counts["tie_success"],
        "tie_failure": counts["tie_failure"],
        "missing_pairs": counts["missing_pair"],
        "details": details,
    }


def live_summary(results):
    for result in results:
        if (
            result.get("experiment_kind") != "live_end_to_end"
            or result.get("diagnostic_replay_only") is not False
        ):
            raise ValueError("Live recovery table received a non-live result")
        if result.get("external_recovery") is True and not (
            result.get("environment_valid") is True
            and result.get("root_stable") is True
            and (result.get("root_before_window") or {}).get("passed") is True
            and (result.get("root_after_window") or {}).get("passed") is True
            and (result.get("quality") or {}).get("passed") is True
        ):
            raise ValueError(
                "Live recovery claim lacks independent root/window evidence"
            )
    return {
        "observed_trials": len(results),
        "environment_valid": sum(
            row.get("environment_valid") is True for row in results
        ),
        "external_recovery": sum(
            row.get("external_recovery") is True for row in results
        ),
        "correct_escalation": sum(
            row.get("expected_disposition") == "escalate"
            and row.get("disposition_correct") is True
            for row in results
        ),
        "evaluator_cleanup_is_recovery": False,
        "results": [
            {
                key: row.get(key)
                for key in (
                    "id",
                    "case_id",
                    "variant",
                    "completed",
                    "environment_valid",
                    "external_recovery",
                    "disposition",
                    "root_stable",
                    "provider_http_attempts",
                    "known_total_tokens",
                    "error_type",
                )
            }
            for row in results
        ],
    }


def comparison_summary(rows, variants, metrics):
    return {
        "planned_case_ids": sorted({task["case_id"] for task, _ in rows}),
        "per_variant": {
            variant: aggregate(
                [(task, result) for task, result in rows if task["variant"] == variant]
            )
            for variant in variants
        },
        "paired_comparisons": [
            paired(rows, left, right, metric)
            for left, right in itertools.combinations(variants, 2)
            for metric in metrics
        ],
    }


def analyze(plan, state, all_results, live_results=()):
    if sha(plan) != state["plan_sha256"]:
        raise ValueError("Frozen plan integrity mismatch")
    lookup = {result["id"]: result for result in all_results}
    if len(lookup) != len(all_results):
        raise ValueError("Duplicate result IDs")
    rows = []
    used_ids = set()
    for task in plan["tasks"]:
        entry = state["tasks"][task["id"]]
        result = lookup.get(entry.get("result_id"))
        if entry.get("status") == "finished" and result is None:
            raise ValueError("Finished schedule task lacks its raw result")
        if result is not None:
            if any(
                result.get(key) != task[key]
                for key in ("case_id", "variant", "repeat_index")
            ):
                raise ValueError("Result does not match the frozen schedule")
            if result.get("snapshot_id") != task["snapshot"]["snapshot_id"]:
                raise ValueError("Result used a different snapshot")
            if result.get("recovery_measured") is not False:
                raise ValueError(
                    "Diagnostic table received a live/unknown recovery result"
                )
            if result["id"] in used_ids:
                raise ValueError("One raw trial was reused for multiple schedule tasks")
            used_ids.add(result["id"])
        rows.append((task, result))
    validation = [
        (task, result) for task, result in rows if task["split"] == "validation"
    ]
    groups = {}
    for name, variants in plan["comparison_groups"].items():
        group_rows = [
            (task, result) for task, result in validation if task["variant"] in variants
        ]
        metrics = (
            ("root_action_proxy_correct", "proposal_acceptable")
            if name == "incremental_H1_prompt"
            else ("root_action_proxy_correct", "disposition_correct")
        )
        groups[name] = comparison_summary(group_rows, variants, metrics)
        if name == "incremental_H1_prompt":
            if any(
                type(task.get("distractor_present")) is not bool
                for task, _ in group_rows
            ):
                raise ValueError(
                    "H1 subgroup needs a predeclared distractor_present boolean"
                )
            groups[name]["predefined_subgroups"] = {
                "all_8": comparison_summary(group_rows, variants, metrics),
                **{
                    "distractor_" + str(present).lower(): comparison_summary(
                        [
                            (task, result)
                            for task, result in group_rows
                            if task["distractor_present"] is present
                        ],
                        variants,
                        metrics,
                    )
                    for present in (True, False)
                },
            }
            groups[name]["measurement_scope"] = (
                "Only two explicit causal/counter-evidence sentences are removed from the D4 prompt; all observations, formatting and executor safeguards stay fixed. Root-action proxy and invalid/rejected proposal counts do not establish fewer actual erroneous rollbacks."
            )
    other = [row for row in all_results if row["id"] not in used_ids]
    return {
        "schema_version": 1,
        "schedule_status": state["status"],
        "plan_sha256": state["plan_sha256"],
        "model_seed_supported": False,
        "formal_holdout_evaluated": False,
        "scope": "Descriptive D5 screening; 8 related validation configurations, no independent fault-class generalization or award claim",
        "causal_limits": "A_H1_off vs A_D4_compact is the incremental two-sentence H1 ablation. Neutral vs D4 is a whole-prompt factor, not H1 alone. Single-call factorial attribution stays within single-call architecture. Holmes tool truncation and multi-step budgets prevent cross-architecture mechanism attribution.",
        "proposal_acceptable_definition": "Output format is valid, common policy did not reject the plan, and no wrong plan was authorized. A model escalation remains a legal proposal; expected disposition and root-action proxy are separate metrics. Its failure count is the invalid/rejected proposal count, not a count of executed bad repairs.",
        "statistical_limits": "Counts and case-paired wins/losses only; related configurations and repeated runs are not independent samples. No significance or formal D6 result inferred.",
        "diagnostic_validation": groups,
        "rules_development": aggregate(
            [(task, result) for task, result in rows if task["split"] == "dev"]
        ),
        "live_end_to_end": live_summary(live_results),
        "pilot_and_other_unscheduled_trials": [
            {
                key: row.get(key)
                for key in (
                    "id",
                    "case_id",
                    "variant",
                    "output_valid",
                    "error_type",
                    "provider_http_attempts",
                    "known_total_tokens",
                )
            }
            for row in other
        ],
        "unscheduled_zero_call_invalid_trials": sum(
            row.get("output_valid") is False and row.get("provider_http_attempts") == 0
            for row in other
        ),
        "zero_call_failure_classification": "Zero model calls alone do not prove an installation failure. Raw stderr must establish the cause; these trials stay outside the frozen validation matrix.",
        "total_diagnostic_provider_attempts_including_pilots": sum(
            row.get("provider_http_attempts", 0) for row in all_results
        ),
        "cost_amount": None,
    }


def displayed_count(result, metric):
    value = result["metrics"][metric]
    return f"{value['success']}/{value['observed']}（计划{value['planned']}）"


def displayed_invalid(result):
    value = result["metrics"]["proposal_acceptable"]
    return f"{value['failure']}/{value['observed']}（计划{value['planned']}）"


def markdown(report):
    lines = [
        "# D5 诊断筛查结果",
        "",
        "本报告区分诊断重放、单次调用机制实验和真实端到端。未运行项明确保留，诊断正确或转人工均不冒充恢复。",
        "",
        "调度状态：`" + report["schedule_status"] + "`。金额尚未核实，保持未知。",
        "",
    ]
    titles = {
        "main_systems": "主系统诊断对照",
        "single_call_factorial": "单次调用：整套提示×证据视图因素对照",
        "incremental_H1_prompt": "H1 增量提示消融：只移除两句显式因果/反证要求",
    }
    for name, group in report["diagnostic_validation"].items():
        lines.extend(
            [
                "## " + titles.get(name, name),
                "",
                "| 方案 | 已运行/计划 | 输出有效 | 根因动作代理正确（仅支持动作） | 最终处置符合预期 | 无效/被拒提案 | HTTP 尝试 | 已知 token | usage 未知 |",
                "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for variant, result in group["per_variant"].items():
            lines.append(
                f"| {variant} | {result['observed_trials']}/{result['planned_trials']} | {displayed_count(result, 'output_valid')} | {displayed_count(result, 'root_action_proxy_correct')} | {displayed_count(result, 'disposition_correct')} | {displayed_invalid(result)} | {result['provider_http_attempts']} | {result['known_total_tokens']} | {result['unknown_usage_attempts']} |"
            )
        lines.append("")
        if name == "incremental_H1_prompt":
            lines.extend(
                [
                    "预定义子组如下；同案例配对胜负和缺失保留在 JSON。共同执行器始终开启，提案指标不能解释为实际误回滚次数下降。",
                    "",
                    "| 子组 | 计划案例数 | 方案 | 根因动作代理正确 | 无效/被拒提案 |",
                    "|---|---:|---|---:|---:|",
                ]
            )
            for subgroup, summary in group["predefined_subgroups"].items():
                for variant, result in summary["per_variant"].items():
                    lines.append(
                        f"| {subgroup} | {len(summary['planned_case_ids'])} | {variant} | {displayed_count(result, 'root_action_proxy_correct')} | {displayed_invalid(result)} |"
                    )
            lines.append("")
    lines.extend(
        [
            "## 真实端到端",
            "",
            f"已记录 {report['live_end_to_end']['observed_trials']} 次，外部恢复 {report['live_end_to_end']['external_recovery']} 次。恢复须同时满足根因字段复原与完整独立窗口；评测器清理不计恢复。",
            "",
            "## 证据边界",
            "",
            "8 个验证配置具有故障家族相关性，本阶段只报告计数和同案例配对胜负，不作统计显著性或留出集泛化声明。中性提示与 D4 是整套提示对照，不能称为 H1 单因素；H1 单独比较仅去掉两句的 A_H1_off 与 A_D4_compact。单次调用的提示词/证据消融只在相同单次架构内解释；完整 Holmes 对照单独报告。所有配对详情、失败类别和未调度预跑见同名 JSON。",
            "",
        ]
    )
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", type=Path, default=WORK / "matrix")
    parser.add_argument("--output", type=Path, default=WORK / "analysis.json")
    args = parser.parse_args()
    report = analyze(
        json.loads((args.matrix / "plan.json").read_text()),
        json.loads((args.matrix / "state.json").read_text()),
        [json.loads(file.read_text()) for file in (WORK / "results").glob("*.json")],
        [json.loads(file.read_text()) for file in (WORK / "e2e").glob("*/result.json")],
    )
    write_json(args.output, report)
    args.output.with_suffix(".md").write_text(markdown(report))
    print(
        json.dumps(
            {"schedule_status": report["schedule_status"], "output": str(args.output)}
        )
    )


if __name__ == "__main__":
    main()
