"""Generate the runtime source manifest immediately before the D3 image build."""

import json
from pathlib import Path

from cloudsre.reporting import versions

ROOT = Path(__file__).resolve().parents[1]
(ROOT / "deploy/d3-build-manifest.json").write_text(
    json.dumps(versions(ROOT), indent=2)
)
