"""Publish evidence-derived progress without promoting partial work to completion."""

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from cloudsre.model_budget import ModelBudget

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "evaluation/d5-runtime"


def read_json(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def main():
    captures = read_json(ROOT / "evaluation/d5-private/capture-index.json", {})
    budget = ModelBudget(WORK / "budget-private/batch.sqlite", 100).summary()
    matrix = read_json(WORK / "matrix/state.json", {})
    trials = [read_json(path) for path in (WORK / "e2e").glob("*/result.json")]
    regression = read_json(WORK / "regression/summary.json", {})
    status = {
        "stage": "D5",
        "complete": False,
        "updated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "captured_dev": sum(
            row.get("split") == "dev"
            and row.get("passed")
            and row.get("scoring_ready") is True
            for row in captures.values()
        ),
        "captured_validation": sum(
            row.get("split") == "validation"
            and row.get("passed")
            and row.get("scoring_ready") is True
            for row in captures.values()
        ),
        "authorized_batch_limit": budget["limit"],
        "used_slots": budget["used_slots"],
        "remaining_slots": budget["remaining_slots"],
        "known_total_tokens": budget["known_total_tokens"],
        "cost_amount": None,
        "matrix_status": matrix.get("status", "not_started"),
        "matrix_finished_tasks": sum(
            row.get("status") == "finished" for row in matrix.get("tasks", {}).values()
        ),
        "matrix_planned_tasks": len(matrix.get("tasks", {})),
        "e2e_trials": len(trials),
        "e2e_recovered": sum(row.get("external_recovery") is True for row in trials),
        "regression": regression,
        "production_choice": "D4 retained; no D5 candidate adopted without reliable comparative benefit",
        "formal_holdout_evaluated": False,
    }
    text = f"""# D5 工作进度

更新时间：{status["updated_at"]}。D5 尚未全部验收。

| 项目 | 实际状态 |
|---|---|
| 真实开发样本 | {status["captured_dev"]}/12 |
| 真实验证样本 | {status["captured_validation"]}/8 |
| 模型预算 | 已占用 {status["used_slots"]}/100，剩余 {status["remaining_slots"]}；失败和重试计入 |
| 冻结矩阵 | {status["matrix_status"]}，完成 {status["matrix_finished_tasks"]}/{status["matrix_planned_tasks"]} |
| 真实现场 E2E | {status["e2e_trials"]} 次，独立恢复 {status["e2e_recovered"]} 次 |
| 最新完整回归 | {regression.get("tests", "未运行")} 项；通过={regression.get("passed", False)} |
| 费用 | 金额未知，保留实际调用和provider usage |

已实现持久预算代理、模型密钥隔离、逐HTTP脱敏轨迹、完整快照/事件冻结、固定随机调度、单次2×2与精确H1增量消融、严格现场评测器及结果汇总。实现状态不等于实验收益。

所有主系统与机制实验保留失败和缺失项；不足预算不得删病例或降低验收。真实恢复须根因字段复原、实际有界写入及完整120秒/120请求/12检查。H2检测与反馈再规划分开，H2b尚未实现。

当前保留已验收D4及默认逐次人工审批。剩余工作：完成实测、解释配对结果与采纳决定、核实远端CI、冻结证据归档。正式D6留出及每例三次重复仍属后续，未削减。
"""
    for path in (ROOT / "docs/D5_PROGRESS.md", Path("/mnt/d/uclaude/D5_PROGRESS.md")):
        path.write_text(text)
    for path in (
        WORK / "delivery-status.json",
        Path("/mnt/d/uclaude/D5_DELIVERY_STATUS.json"),
    ):
        path.write_text(json.dumps(status, ensure_ascii=False, indent=2))
    print(json.dumps(status, ensure_ascii=False))


if __name__ == "__main__":
    main()
