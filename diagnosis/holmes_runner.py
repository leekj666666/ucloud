"""Runs in .venv-holmes; no credentials in command-line arguments or config files."""

import os

os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
os.environ["OVERRIDE_MAX_OUTPUT_TOKEN"] = "1024"
os.environ["OVERRIDE_MAX_CONTENT_SIZE"] = "32000"
import json
import sys
from pathlib import Path

import yaml
from dotenv import dotenv_values
from holmes.config import Config
from holmes.core.tools import PrerequisiteCacheMode
from holmes.core.tools_utils.tool_executor import ToolExecutor
from pydantic import SecretStr

root = Path(__file__).resolve().parents[1]
cfg = dotenv_values("/mnt/d/uclaude/model.env")
out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
toolpath = root / "diagnosis/toolsets.yaml"
config = Config(
    model="deepseek/" + cfg["MODEL_NAME"],
    api_key=SecretStr(cfg["DEEPSEEK_API_KEY"]),
    api_base=cfg["OPENAI_BASE_URL"],
    max_steps=4,
    toolsets=yaml.safe_load(toolpath.read_text())["toolsets"],
)
ai = config.create_toolcalling_llm(
    enable_all_toolsets_possible=False,
    prerequisite_cache=PrerequisiteCacheMode.DISABLED,
)
ai = ai.with_executor(
    ToolExecutor([t for t in ai.tool_executor.toolsets if t.name == "sre-monitoring"])
)
available = ai.tool_executor.get_all_tools_openai_format()
names = [t["function"]["name"] for t in available]
print("Enabled tools:", names, flush=True)
if names != ["get_monitoring_health"]:
    raise RuntimeError("Required monitoring tool not loaded; no model request made")
messages = [
    {
        "role": "system",
        "content": "You are a read-only SRE investigator. You must query get_monitoring_health before answering. Report what is observed, cite the query, distinguish unknown business health. Do not claim any incident was repaired. Be concise.",
    },
    {
        "role": "user",
        "content": "Investigate whether the local monitoring target is up. Use live evidence.",
    },
]
result = ai.call(messages)
raw = result.model_dump(mode="json")
encoded = json.dumps(raw, ensure_ascii=False, indent=2)
encoded = encoded.replace(cfg["DEEPSEEK_API_KEY"], "[REDACTED]")
(out / "holmes-result.json").write_text(encoded)
print("Holmes investigation completed. Result fields:", ", ".join(raw.keys()))
