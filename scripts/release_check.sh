#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
cd "$ROOT"

export PYTHONHASHSEED=0

"$PYTHON_BIN" - <<'PY'
import sys

if sys.flags.optimize:
    raise SystemExit("Release checks refuse optimized Python because assertions are required")
PY

"$PYTHON_BIN" scripts/verify_manifest.py
"$PYTHON_BIN" -m unittest discover -s tests -v
"$PYTHON_BIN" -m compileall -q agentbot tests scripts
"$PYTHON_BIN" scripts/fuzz_prompt_bounds.py --cases 100
"$PYTHON_BIN" scripts/fuzz_attachment_parsers.py
"$PYTHON_BIN" scripts/measure_lean_runtime.py --settle-seconds 5 --limit-mib 100

"$PYTHON_BIN" - <<'PY'
from pathlib import Path
import ast

root = Path.cwd()
version_ns: dict[str, object] = {}
exec((root / "agentbot" / "__init__.py").read_text(encoding="utf-8"), version_ns)
assert version_ns.get("__version__") == "1.2.0", version_ns.get("__version__")
assert version_ns.get("CLIENT_AGENT") == "discord-agent-lite:1.2.0:discord-bot"

for path in sorted((root / "agentbot").glob("*.py")):
    source = path.read_text(encoding="utf-8")
    ast.parse(source, filename=str(path))
    if path.name == "attachments.py":
        assert "create_subprocess" not in source

requirements = (root / "requirements.txt").read_text(encoding="utf-8").casefold()
for forbidden in ("pynacl", "ffmpeg", "torch", "transformers", "numpy", "sentence-transformers", "pypdf"):
    assert forbidden not in requirements, forbidden

example = (root / ".env.example").read_text(encoding="utf-8")
for key in (
    "RELATIONSHIPS_ENABLED",
    "RELATIONSHIP_DIRECT_ONLY",
    "RELATIONSHIP_MEANINGFUL_EVENT_THRESHOLD",
    "MAX_TOTAL_RELATIONSHIPS",
    "HORDE_ROUTER_METADATA_TTL_SECONDS",
    "HORDE_ROUTER_STICKY_SECONDS",
    "HORDE_MIN_MODEL_PARAMETERS_BN",
    "MAX_MODEL_OUTCOMES",
    "ATTACHMENT_MAX_COUNT",
    "ATTACHMENT_TIMEOUT_SECONDS",
    "ALCHEMIST_ENABLED",
    "ALCHEMIST_API_KEY",
    "PROFILE_CONTEXT_FACTS",
    "JOURNAL_CONTEXT_ENTRIES",
    "PROACTIVE_MIN_IDLE_SECONDS",
    "PROACTIVE_COOLDOWN_SECONDS",
    "PROACTIVE_DAILY_LIMIT",
):
    assert key in example, key

example_values = {
    key.strip(): value.strip()
    for line in example.splitlines()
    if line.strip() and not line.lstrip().startswith("#") and "=" in line
    for key, value in (line.split("=", 1),)
}
assert example_values.get("ATTACHMENT_TIMEOUT_SECONDS") == "60"
for retired in (
    "OPENAI_BASE_URL",
    "OPENAI_API_KEY",
    "OPENAI_MODEL",
    "GROUP_CONTINUITY_ENABLED",
    "SUMMARY_ENABLED",
    "ATTACHMENT_CACHE_ENTRIES",
    "ATTACHMENT_MAX_CHUNKS",
    "BOT_INTERACTION_CHANNELS",
    "BOT_REPLY_REQUESTS",
    "BOT_REPLY_PERIOD_SECONDS",
    "PROACTIVE_MAX_IDLE_SECONDS",
    "PROACTIVE_QUIET_START_HOUR",
):
    assert retired not in example_values, retired
PY

if grep -RIinE '[0-9]{17,20}' agentbot; then
  echo "Release check failed: hardcoded Discord snowflake found in runtime." >&2
  exit 1
fi
EXECUTABLE_MATCHES="$(
  grep -RInE '\b(eval|exec|subprocess|os\.system|create_subprocess|Popen|pickle|marshal)\b' agentbot \
    | grep -vE '^agentbot/attachments\.py:[0-9]+:.*asyncio\.(create_subprocess_exec|subprocess\.(Process|PIPE|DEVNULL))' \
    || true
)"
if [[ -n "$EXECUTABLE_MATCHES" ]]; then
  printf '%s\n' "$EXECUTABLE_MATCHES"
  echo "Release check failed: executable/deserialization primitive found in runtime." >&2
  exit 1
fi
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  RUNTIME_DATA="$(git ls-files '*.db' '*.sqlite' '*.sqlite3' '.env' | head -n 1)"
else
  RUNTIME_DATA="$(find . -type f \( -name '*.db' -o -name '*.sqlite' -o -name '*.sqlite3' -o -name '.env' \) -print -quit)"
fi
if [[ -n "$RUNTIME_DATA" ]]; then
  echo "Release check failed: runtime data or secrets file found in release tree." >&2
  exit 1
fi

printf '%s\n' "Static release checks passed"
