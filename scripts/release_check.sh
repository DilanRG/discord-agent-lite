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
import re

root = Path.cwd()
version_ns: dict[str, object] = {}
exec((root / "agentbot" / "__init__.py").read_text(encoding="utf-8"), version_ns)
assert version_ns.get("__version__") == "1.3.0", version_ns.get("__version__")
assert version_ns.get("CLIENT_AGENT") == "discord-agent-lite:1.3.0:discord-bot"

for path in sorted((root / "agentbot").glob("*.py")):
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    allowed_process_call: ast.Call | None = None
    if path.name == "attachments.py":
        process_calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "asyncio"
            and node.func.attr == "create_subprocess_exec"
        ]
        assert len(process_calls) == 1, len(process_calls)
        call = process_calls[0]
        allowed_process_call = call
        assert len(call.args) >= 5
        assert isinstance(call.args[0], ast.Attribute)
        assert isinstance(call.args[0].value, ast.Name)
        assert (call.args[0].value.id, call.args[0].attr) == ("sys", "executable")
        assert [ast.literal_eval(item) for item in call.args[1:4]] == ["-I", "-X", "utf8"]
        assert isinstance(call.args[4], ast.Call)
        assert isinstance(call.args[4].func, ast.Name) and call.args[4].func.id == "str"
        assert len(call.args[4].args) == 1
        assert isinstance(call.args[4].args[0], ast.Name) and call.args[4].args[0].id == "worker"
        keywords = {keyword.arg: keyword.value for keyword in call.keywords}
        assert set(keywords) == {"stdin", "stdout", "stderr", "env", "limit"}, set(keywords)

        def attribute_chain(node: ast.AST) -> tuple[str, ...]:
            parts: list[str] = []
            while isinstance(node, ast.Attribute):
                parts.append(node.attr)
                node = node.value
            assert isinstance(node, ast.Name)
            return (node.id, *reversed(parts))

        assert attribute_chain(keywords["stdin"]) == ("asyncio", "subprocess", "PIPE")
        assert attribute_chain(keywords["stdout"]) == ("asyncio", "subprocess", "PIPE")
        assert attribute_chain(keywords["stderr"]) == ("asyncio", "subprocess", "DEVNULL")
        assert isinstance(keywords["env"], ast.Name) and keywords["env"].id == "environment"
        assert ast.literal_eval(keywords["limit"]) == 65_536

        environment_values = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "environment" for target in node.targets)
        ]
        assert len(environment_values) == 1
        environment_node = environment_values[0]
        assert isinstance(environment_node, ast.Dict)
        environment_items = {
            ast.literal_eval(key): value
            for key, value in zip(environment_node.keys, environment_node.values, strict=True)
        }
        assert set(environment_items) == {"PATH", "LANG", "PYTHONIOENCODING"}
        assert attribute_chain(environment_items["PATH"]) == ("os", "defpath")
        assert ast.literal_eval(environment_items["LANG"]) == "C.UTF-8"
        assert ast.literal_eval(environment_items["PYTHONIOENCODING"]) == "utf-8"

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported = {alias.name for alias in node.names}
            assert not imported & {
                "builtins", "importlib", "marshal", "multiprocessing", "pickle", "runpy", "subprocess"
            }
            for alias in node.names:
                if alias.name in {"asyncio", "os", "sys"}:
                    assert alias.asname in {None, alias.name}, (alias.name, alias.asname)
        if isinstance(node, ast.ImportFrom):
            assert node.module not in {
                "builtins", "importlib", "marshal", "multiprocessing", "pickle", "runpy", "subprocess"
            }
            if node.module == "os":
                for alias in node.names:
                    assert not alias.name.startswith(("exec", "spawn")), alias.name
                    assert alias.name not in {
                        "fork", "forkpty", "popen", "posix_spawn", "posix_spawnp", "startfile", "system"
                    }, alias.name
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            assert node.func.id not in {"__import__", "eval", "exec"}, node.func.id
            if node.func.id == "getattr" and len(node.args) >= 2:
                target, attribute = node.args[:2]
                if isinstance(target, ast.Name) and target.id in {"builtins", "os"}:
                    assert isinstance(attribute, ast.Constant) and isinstance(attribute.value, str)
                    assert not attribute.value.startswith(("exec", "spawn")), attribute.value
                    assert attribute.value not in {
                        "fork", "forkpty", "popen", "posix_spawn", "posix_spawnp", "startfile", "system"
                    }, attribute.value
        if isinstance(node.func, ast.Attribute):
            assert node.func.attr not in {"Popen", "system"}, node.func.attr
            if isinstance(node.func.value, ast.Name):
                assert node.func.value.id != "subprocess"
                if node.func.value.id == "os":
                    assert not node.func.attr.startswith(("exec", "spawn")), node.func.attr
                    assert node.func.attr not in {
                        "fork", "forkpty", "popen", "posix_spawn", "posix_spawnp", "startfile"
                    }, node.func.attr
                if node.func.value.id == "asyncio" and node.func.attr.startswith(
                    "create_subprocess"
                ):
                    assert node is allowed_process_call, (path.name, node.func.attr)

manifest_paths = [
    line.split("  ", 1)[1].removeprefix("./")
    for line in (root / "MANIFEST.sha256").read_text(encoding="ascii").splitlines()
]
private_persona = re.compile("Mi" + "ka(?:Bot)?", re.IGNORECASE)
discord_snowflake = re.compile(r"[0-9]{17,20}")
for relative in manifest_paths:
    public_text = (root / relative).read_text(encoding="utf-8")
    assert private_persona.search(public_text) is None, f"private identifier in public file: {relative}"
    for match in discord_snowflake.finditer(public_text):
        value = match.group(0)
        is_fixture = len(set(value)) == 1 or value == "123456789012345678"
        assert is_fixture, f"non-fixture Discord snowflake in public file: {relative}"

requirements = (root / "requirements.txt").read_text(encoding="utf-8").casefold()
for forbidden in ("pynacl", "ffmpeg", "torch", "transformers", "numpy", "sentence-transformers"):
    assert forbidden not in requirements, forbidden
assert "pypdf==6.14.2" in requirements, requirements

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
    "ATTACHMENT_DOCUMENT_LOCK_PATH",
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
