"""H5 product entry: read-only rules, common B3 fallback, existing job lifecycle."""

import json
import os
import runpy
import subprocess
import sys
from pathlib import Path

folder = Path(sys.argv[1])
mode = os.environ.get("CLOUDSRE_ROUTING_MODE", "h5")
tool_env = {
    k: v
    for k, v in os.environ.items()
    if k not in {"DEEPSEEK_API_KEY", "CLOUDSRE_DIAG_TOKEN", "CLOUDSRE_RUN_TOKEN"}
}
tool_env["PYTHONPATH"] = "/app/backend"
subprocess.run(
    [
        os.environ["CLOUDSRE_TOOL_PYTHON"],
        "-m",
        "cloudsre.product_diagnosis",
        str(folder),
        mode,
    ],
    env=tool_env,
    check=True,
)
routing = json.loads((folder / "routing.json").read_text())
if routing["route"] == "model":
    from cloudsre.call_ledger import install_http_ledger

    install_http_ledger(folder / "calls.jsonl", os.environ["OPENAI_BASE_URL"])
    os.environ["CLOUDSRE_SNAPSHOT_DIR"] = str(folder / "snapshot")
    os.environ["CLOUDSRE_RUN_TOKEN"] = os.environ["DEEPSEEK_API_KEY"]
    sys.argv = [str(Path(__file__).with_name("runner_common.py")), str(folder), "B3"]
    runpy.run_path(sys.argv[0], run_name="__main__")
proposal = json.loads((folder / "proposal.json").read_text())
# Identical public output contract for rule and model; worker still authorizes it.
(folder / "holmes-result.json").write_text(
    json.dumps(
        {"result": json.dumps(proposal, ensure_ascii=False), "routing": routing},
        ensure_ascii=False,
    )
)
