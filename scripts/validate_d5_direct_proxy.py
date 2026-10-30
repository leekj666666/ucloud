import json
import sys
from pathlib import Path

R = Path("/home/lee/sre-project")
sys.path[:0] = [str(R), str(R / "backend")]
from cloudsre.model_budget import ModelBudget

from benchmarks.d5.catalog import VALIDATION
from benchmarks.d5.diagnose import trial
from benchmarks.d5.live import WORK, exclusive

with exclusive():
    b = ModelBudget(WORK / "budget-private/batch2.sqlite", 100)
    folder = WORK / "direct-validation"
    folder.mkdir(exist_ok=True)
    rows = []
    for case, variant, repeat in [
        (VALIDATION[3].id, "B3", 200),
        (VALIDATION[3].id, "B3", 201),
        (VALIDATION[7].id, "H4_alias", 200),
    ]:
        r = trial(case, variant, repeat, budget_override=b)
        attempts = json.loads(
            (WORK / "diagnostics" / r["id"] / "provider-attempts.json").read_text()
        )
        row = {
            "id": r["id"],
            "case": case,
            "variant": variant,
            "output_valid": r["output_valid"],
            "scoring": r.get("scoring"),
            "attempts": len(attempts),
            "statuses": [a.get("http_status") for a in attempts],
            "errors": [a.get("error_type") for a in attempts],
        }
        rows.append(row)
        (folder / "results.json").write_text(
            json.dumps(
                {
                    "protocol": "C1-direct-transport-validation",
                    "formal_matrix": False,
                    "results": rows,
                },
                indent=2,
            )
        )
        print(json.dumps(row), flush=True)
        if any(a.get("error_type") for a in attempts):
            break
