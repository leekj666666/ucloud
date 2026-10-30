import json
import sys
from pathlib import Path

R = Path("/home/lee/sre-project")
sys.path[:0] = [str(R), str(R / "backend")]
from cloudsre.model_budget import ModelBudget

from benchmarks.d5.diagnose import trial
from benchmarks.d5.live import WORK, exclusive

with exclusive():
    folder = WORK / "original-transport-retests"
    folder.mkdir(exist_ok=True)
    tasks = []
    for group in ("matrix", "followup"):
        state = json.loads((WORK / group / "state.json").read_text())
        for entry in state["tasks"].values():
            if not entry.get("result_id"):
                continue
            ident = entry["result_id"]
            r = json.loads((WORK / "results" / (ident + ".json")).read_text())
            attempts = json.loads(
                (WORK / "diagnostics" / ident / "provider-attempts.json").read_text()
            )
            if any(
                a.get("error_type") and a.get("http_status") is None for a in attempts
            ):
                tasks.append(
                    {
                        "source_group": group,
                        "original_result": ident,
                        "case": r["case_id"],
                        "variant": r["variant"],
                    }
                )
    plan = {
        "protocol": "original-thinking-direct-transport",
        "selection": "All transport-failed results in original matrix and followup; excludes C1",
        "repeat": 400,
        "tasks": tasks,
    }
    path = folder / "plan.json"
    if path.exists():
        assert json.loads(path.read_text()) == plan
    else:
        path.write_text(json.dumps(plan, indent=2))
    statepath = folder / "results.json"
    rows = json.loads(statepath.read_text()) if statepath.exists() else []
    b = ModelBudget(WORK / "budget-private/batch3.sqlite", 100)
    print(
        json.dumps({"planned": len(tasks), "already_recorded": len(rows)}), flush=True
    )
    for t in tasks:
        if any(x["original_result"] == t["original_result"] for x in rows):
            continue
        r = trial(t["case"], t["variant"], 400, budget_override=b)
        a = json.loads(
            (WORK / "diagnostics" / r["id"] / "provider-attempts.json").read_text()
        )
        row = {
            **t,
            "new_result": r["id"],
            "output_valid": r["output_valid"],
            "disposition_correct": r.get("scoring", {}).get("disposition_correct"),
            "attempts": len(a),
            "transport_errors": sum(bool(x.get("error_type")) for x in a),
        }
        rows.append(row)
        statepath.write_text(json.dumps(rows, indent=2))
        print(json.dumps(row), flush=True)
        if row["transport_errors"]:
            break
