import json
import sys
from pathlib import Path

R = Path("/home/lee/sre-project")
sys.path[:0] = [str(R), str(R / "backend")]
import httpx
from cloudsre.model_budget import ModelBudget

from benchmarks.d5 import followup, matrix
from benchmarks.d5.diagnose import trial
from benchmarks.d5.live import PRIVATE, WORK, exclusive

ledger = sys.argv[1] if len(sys.argv) > 1 else "batch3.sqlite"
with exclusive():
    p = WORK / "followup/plan.json"
    s = WORK / "followup/state.json"
    plan = json.loads(p.read_text())
    state = json.loads(s.read_text())
    assert plan["source_hashes"] == matrix.protocol_hashes()
    captures = matrix.frozen_captures(
        json.loads((PRIVATE / "capture-index.json").read_text())
    )
    assert plan["tasks"] == followup.build_tasks(captures)
    b = ModelBudget(WORK / "budget-private" / ledger, 100)
    assert all(a["state"] == "finished" for a in b.summary()["attempts"])
    with httpx.Client(trust_env=False) as c:
        h = c.get("http://localhost:18090/health").json()
    assert all(
        h[k] == b.summary()[k] for k in ("limit", "used_slots", "remaining_slots")
    )
    result = followup.execute(
        plan,
        state,
        b,
        lambda c, v, i: trial(c, v, i, budget_override=b),
        lambda: matrix.write_json(s, state),
        followup.transport_failed,
    )
    print(
        json.dumps(
            {
                "status": result["status"],
                "finished": sum(
                    x["status"] == "finished" for x in result["tasks"].values()
                ),
                "planned": len(result["tasks"]),
            }
        )
    )
