"""Generate only source/version hashes; no runtime credentials or experiment data."""

import json
from pathlib import Path

from cloudsre.reporting import versions

root = Path(__file__).resolve().parents[1]
(root / "deploy/d5-build-manifest.json").write_text(
    json.dumps(versions(root), indent=2)
)
