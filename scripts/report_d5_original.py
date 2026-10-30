import json
import sys
from datetime import UTC, datetime
from pathlib import Path

R = Path("/home/lee/sre-project")
sys.path[:0] = [str(R), str(R / "backend")]
from cloudsre.model_budget import ModelBudget

W = R / "evaluation/d5-runtime"
s = json.loads((W / "followup/state.json").read_text())
p = json.loads((W / "followup/plan.json").read_text())
groups = {}
rows = []
for t in p["tasks"]:
    g = groups.setdefault(
        t["split"] + "/" + t["variant"],
        {
            "planned": 0,
            "finished": 0,
            "valid": 0,
            "correct_disposition": 0,
            "attempts": 0,
            "transport_failures": 0,
        },
    )
    g["planned"] += 1
    e = s["tasks"][t["id"]]
    if e["status"] != "finished":
        continue
    r = json.loads((W / "results" / (e["result_id"] + ".json")).read_text())
    a = json.loads((W / "diagnostics" / r["id"] / "provider-attempts.json").read_text())
    g["finished"] += 1
    g["valid"] += bool(r["output_valid"])
    g["correct_disposition"] += r.get("scoring", {}).get("disposition_correct") is True
    g["attempts"] += len(a)
    g["transport_failures"] += sum(bool(x.get("error_type")) for x in a)
    rows.append(
        {
            "case": t["case_id"],
            "variant": t["variant"],
            "result": r["id"],
            "scoring": r.get("scoring"),
        }
    )
counts = {
    x.name: ModelBudget(x, 100).summary()["used_slots"]
    for x in (W / "budget-private").glob("batch*.sqlite")
}
out = {
    "updated_at": datetime.now(UTC).isoformat(),
    "status": s["status"],
    "groups": groups,
    "rows": rows,
    "ledger_counts": counts,
    "total_attempts": sum(counts.values()),
    "authorized_global_cap": None,
    "C1_excluded": True,
    "live_recovery_claim": False,
}
(W / "followup/analysis.json").write_text(json.dumps(out, indent=2))
text = (
    "# D5 原思考方案续跑结果\n\n"
    + json.dumps(
        {k: v for k, v in out.items() if k != "rows"}, ensure_ascii=False, indent=2
    )
    + "\n\n历史传输失败保留。诊断正确不等于实机恢复；C方案已停止且不混入统计。\n"
)
(R / "docs/D5_ORIGINAL_RESUMPTION.md").write_text(text)
Path("/mnt/d/uclaude/D5_ORIGINAL_RESUMPTION.md").write_text(text)
print(json.dumps({k: v for k, v in out.items() if k != "rows"}, ensure_ascii=False))
