"""Evaluator-side Docker resource observer. Workers receive only a read-only snapshot."""

import json
import os
import time
from pathlib import Path

from cloudsre.quality import resources

target = Path(
    os.environ.get("CLOUDSRE_OBSERVER_OUTPUT", "evaluation/d3-observer/resources.json")
)
target.parent.mkdir(parents=True, exist_ok=True)
while True:
    data = resources()
    data["sampled_unix"] = time.time()
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(data))
    temporary.replace(target)
    time.sleep(5)
