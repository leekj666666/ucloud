import json
from pathlib import Path

R = Path("/home/lee/sre-project")
W = R / "evaluation/d5-runtime"


def read_group(group):
    state = json.loads((W / group / "state.json").read_text())
    out = {}
    for e in state["tasks"].values():
        if e.get("status") != "finished":
            continue
        r = json.loads((W / "results" / (e["result_id"] + ".json")).read_text())
        out[(r["case_id"], r["variant"])] = r
    return out


baseline = read_group("matrix")
candidates = read_group("followup")
comparisons = []
for candidate, base in [("H4_alias", "B3"), ("H5_hybrid", "B3"), ("H5_hybrid", "B0")]:
    rows = []
    for (case, variant), r in candidates.items():
        if variant != candidate or not case.startswith("val_"):
            continue
        b = baseline.get((case, base))
        if b is None:
            continue

        def compact(x):
            a = json.loads(
                (W / "diagnostics" / x["id"] / "provider-attempts.json").read_text()
            )
            return {
                "id": x["id"],
                "correct": x.get("scoring", {}).get("disposition_correct") is True,
                "attempts": x["provider_http_attempts"],
                "transport_failed": any(
                    z.get("error_type") and z.get("http_status") is None for z in a
                ),
            }

        rows.append({"case": case, "baseline": compact(b), "candidate": compact(r)})
    valid = [
        r
        for r in rows
        if not r["baseline"]["transport_failed"]
        and not r["candidate"]["transport_failed"]
    ]
    comparisons.append(
        {
            "candidate": candidate,
            "baseline": base,
            "scheduled_pairs": len(rows),
            "transport_valid_pairs": len(valid),
            "excluded_pairs_reported_not_deleted": [
                r["case"] for r in rows if r not in valid
            ],
            "wins": sum(
                r["candidate"]["correct"] and not r["baseline"]["correct"]
                for r in valid
            ),
            "losses": sum(
                r["baseline"]["correct"] and not r["candidate"]["correct"]
                for r in valid
            ),
            "ties": sum(
                r["candidate"]["correct"] == r["baseline"]["correct"] for r in valid
            ),
            "rows": rows,
        }
    )
out = {
    "exploratory_only": True,
    "C1_excluded": True,
    "comparisons": comparisons,
    "decision": "H5 remains candidate pending transport-gap completion and final audit; H4_alias not adopted; D4 production unchanged",
}
(W / "adoption-analysis.json").write_text(json.dumps(out, indent=2))
print(json.dumps([{k: v for k, v in x.items() if k != "rows"} for x in comparisons]))
