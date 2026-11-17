"""Lightweight local-LLM coding harness (in-tree plugin).

Purpose-built for SMALL local models (Ollama/vLLM/LM Studio, e.g. 4-9B
Qwen distills) that collapse under the huge system prompts and 9-tool
surfaces of codex/dsh: this harness keeps a SHORT system prompt and a
minimal 5-tool set (list_dir / read_file / write_file / run_command /
done), which small tool-trained models handle reliably (verified against
Qwen3 distills via Ollama's tool-calling API).

Provider wiring is env-driven (no new config):
    UC_LLM_PROVIDER=openai + OPENAI_API_BASE=http://<host>:11434/v1
    + OPENAI_API_KEY=<dummy> + OPENAI_DEFAULT_MODEL=openai/<model>
— the same litellm route the Orchestrator's decomposition uses, so ONE
local server config drives both planning and execution.

Execution model: the sandbox spawns this module as a subprocess
(``python -m ultimate_coders.agent.harness_local_loop``), matching the
uniform timeout/streaming/git-baseline contract of every other adapter.

Runner protocol (stdout):
- NDJSON progress events: {"event": "tool_call"|"assistant"|"usage", ...}
- Optional drift event: {"event": "drift_advisor", "signals": [...], "prompt": ...}
- Final envelope (last line): {"event": "final", "success": bool, ...}

Selection:
    UC_CODING_AGENT=local-harness   (alias: local-llm)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import posixpath
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ultimate_coders.agent.drift_monitor import DriftMonitor, drift_monitor_enabled
from ultimate_coders.agent.ecloop_gate import ECLoopGate, ecloop_gate_enabled
from ultimate_coders.agent.radio import (
    BackgroundMentionWatcher,
    InMemoryRadioBus,
    RadioMessage,
    radio_enabled,
)
from ultimate_coders.agent.sandbox import (
    AgentAdapter,
    AgentOutput,
    ExecResult,
    SandboxConfig,
    TokenUsage,
)
from ultimate_coders.architecture.context_folding import (
    ACPFolder,
    ReadLedger,
    folding_enabled,
)

logger = logging.getLogger(__name__)

AGENT_NAME = "local-harness"
AGENT_ALIASES = ("local-llm",)
DEFAULT_MAX_TURNS = 30
RUN_COMMAND_TIMEOUT_SECS = 120
MAX_READ_BYTES = 256 * 1024
DEFAULT_READ_BYTES = 8 * 1024
MAX_WRITE_BYTES = 256 * 1024
DRIFT_BOUNDARY_ENV = "UC_DRIFT_BOUNDARY"
DRIFT_REQUIRED_FILES_ENV = "UC_DRIFT_REQUIRED_FILES"
RADIO_GRAPH_ENV = "UC_RADIO_GRAPH_ID"
RADIO_NODE_ENV = "UC_RADIO_NODE_ID"
RADIO_INBOX_ENV = "UC_RADIO_INBOX_JSON"
TRACE_LOGPROBS_ENV = "UC_TRACE_LOGPROBS"
MAX_TURN_LIKELIHOODS = 64

# Deliberately SHORT — large instruction blobs are what break small models.
_SYSTEM_PROMPT = """\
You are a coding agent completing one task in a repository working directory.

Work step by step with your tools:
- list_dir / read_file to inspect the code
- write_file to make edits
- run_command for builds/tests
- done (with a summary) when finished

Rules: stay inside the working directory; make minimal edits; always finish
by calling done. Follow the requested output and restrictions exactly; do
not invent extra work.
"""

_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List a directory (relative path, '.' = root).",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a UTF-8 text file (relative path), 8 KiB at a time by default.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "offset": {"type": "integer", "minimum": 0},
                    "max_bytes": {"type": "integer", "minimum": 1, "maximum": MAX_READ_BYTES},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a UTF-8 file (relative path).",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a short shell command (build/test/lint).",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "uc_radio_post",
            "description": "Post a short worklog update for teammate agents.",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "uc_radio_create_thread",
            "description": "Create a short teammate thread reply, optionally to another node.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "parent_id": {"type": "string"},
                    "target_node_id": {"type": "string"},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "done",
            "description": "Finish with a one-paragraph summary.",
            "parameters": {
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"],
            },
        },
    },
]


def register(reg: Any) -> None:
    """Register into a plugin registry (idempotent)."""
    from ultimate_coders.agent.registry import AgentPluginSpec

    reg.register(
        AgentPluginSpec(
            name=AGENT_NAME,
            aliases=AGENT_ALIASES,
            factory=LocalLoopHarnessAdapter,
            api_key_env=None,  # provider comes from the litellm env route
            cli_probe=None,  # API-backed, no external CLI
            description=(
                "Lightweight litellm tool loop tuned for small local models "
                "(Ollama/vLLM); uses the UC_LLM_PROVIDER/OPENAI_API_BASE route"
            ),
        )
    )


# ── Adapter (sandbox side) ───────────────────────────────────────────


class LocalLoopHarnessAdapter(AgentAdapter):
    """Spawns the local-loop runner via ``python -m`` (sandbox contract)."""

    def name(self) -> str:
        return AGENT_NAME

    def build_request(
        self,
        prompt: str,
        working_dir: str,
        config: SandboxConfig,
        subtask_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        prompt_file = tempfile.NamedTemporaryFile(
            "w", suffix=".md", prefix="uc-ll-prompt-", delete=False, encoding="utf-8"
        )
        with prompt_file:
            prompt_file.write(prompt)

        max_turns = (subtask_config or {}).get("max_turns") or DEFAULT_MAX_TURNS
        env_vars = dict(config._build_env_vars())
        task_config = subtask_config or {}
        for config_key, env_name in (
            ("_uc_drift_boundary", DRIFT_BOUNDARY_ENV),
            ("_uc_drift_required_files", DRIFT_REQUIRED_FILES_ENV),
            ("_uc_radio_graph_id", RADIO_GRAPH_ENV),
            ("_uc_radio_node_id", RADIO_NODE_ENV),
            ("_uc_radio_inbox_json", RADIO_INBOX_ENV),
        ):
            value = task_config.get(config_key)
            if value:
                env_vars[env_name] = str(value)
        return {
            "command": sys.executable,
            "args": [
                "-m",
                "ultimate_coders.agent.harness_local_loop",
                "--cwd",
                working_dir,
                "--prompt-file",
                prompt_file.name,
                "--max-turns",
                str(max_turns),
            ],
            "timeout_secs": config.max_cpu_seconds,
            "working_dir": working_dir,
            "env_vars": env_vars,
            "_temp_files": [prompt_file.name],
        }

    def parse_output(self, result: ExecResult) -> AgentOutput:
        stderr_tail = ""
        if result.stderr:
            stderr_tail = "\n".join(result.stderr.strip().splitlines()[-10:])

        if result.timed_out:
            return AgentOutput(
                summary="Local harness execution timed out",
                success=False,
                stderr_tail=stderr_tail,
            )

        envelope = _final_envelope(result.stdout)
        token_usage = _usage_from_events(result.stdout)
        turn_likelihoods = _turn_likelihoods_from_events(result.stdout)
        if not turn_likelihoods and envelope is not None:
            raw_likelihoods = envelope.get("turn_likelihoods")
            if isinstance(raw_likelihoods, list):
                turn_likelihoods = [
                    float(value)
                    for value in raw_likelihoods
                    if isinstance(value, (int, float))
                    and math.isfinite(float(value))
                    and 0.0 < float(value) <= 1.0
                ]
        tool_calls = [
            json.loads(line)["tool"]
            for line in result.stdout.splitlines()
            if _is_event(line, "tool_call")
        ]
        commands_run = [
            str(json.loads(line).get("command") or "")
            for line in result.stdout.splitlines()
            if _is_event(line, "command")
        ]
        if envelope is None:
            last_text = ""
            for line in reversed(result.stdout.splitlines()):
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(d, dict) and d.get("event") == "assistant" and d.get("text"):
                    last_text = str(d["text"])
                    break
            return AgentOutput(
                summary=last_text or "Local harness finished without a final envelope",
                success=result.exit_code == 0,
                stderr_tail=stderr_tail,
                tool_calls=tool_calls,
                commands_run=commands_run,
                token_usage=token_usage,
                turn_likelihoods=turn_likelihoods,
            )

        return AgentOutput(
            summary=str(envelope.get("summary") or "Local harness completed"),
            success=bool(envelope.get("success", result.exit_code == 0)),
            stderr_tail=stderr_tail,
            test_output=str(envelope.get("test_output") or ""),
            tool_calls=tool_calls,
            commands_run=commands_run,
            token_usage=token_usage,
            turn_likelihoods=turn_likelihoods,
        )


def _is_event(line: str, event: str) -> bool:
    try:
        d = json.loads(line)
    except json.JSONDecodeError:
        return False
    return isinstance(d, dict) and d.get("event") == event and "tool" in d


def _final_envelope(stdout: str) -> dict[str, Any] | None:
    envelope: dict[str, Any] | None = None
    for line in stdout.splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(d, dict) and d.get("event") == "final":
            envelope = d
    return envelope


def _usage_from_events(stdout: str) -> TokenUsage | None:
    """Sum the per-turn ``usage`` events the child streamed.

    Returns ``None`` when the provider reported no usage at all, so the
    knowledge layer records "not reported" instead of a fake zero that would
    make an A/B token comparison look free.
    """
    input_tokens = 0
    output_tokens = 0
    seen = False
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("event") != "usage":
            continue
        seen = True
        for key in ("prompt_tokens", "completion_tokens"):
            try:
                value = max(0, int(event.get(key, 0) or 0))
            except (TypeError, ValueError):
                value = 0
            if key == "prompt_tokens":
                input_tokens += value
            else:
                output_tokens += value
    if not seen:
        return None
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        source=AGENT_NAME,
    )


def _turn_likelihoods_from_events(stdout: str) -> list[float]:
    """Collect the per-turn answer likelihoods emitted by the child loop."""

    values: list[float] = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("event") != "turn_likelihood":
            continue
        value = event.get("likelihood")
        if isinstance(value, (int, float)) and math.isfinite(float(value)):
            likelihood = float(value)
            if 0.0 < likelihood <= 1.0:
                values.append(likelihood)
    return values


def trace_logprobs_enabled() -> bool:
    """Whether the local harness should request token logprobs."""

    return os.environ.get(TRACE_LOGPROBS_ENV, "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _answer_likelihood(response: Any) -> float | None:
    """Geometric-mean answer likelihood from one completion's token logprobs.

    Returns ``None`` when the provider returned no token logprobs.  The value is
    a real observation, never a placeholder: a provider that ignores the
    ``logprobs`` request produces no sample at all.
    """

    try:
        choice = response.choices[0]
        logprobs = getattr(choice, "logprobs", None)
        if logprobs is None:
            return None
        content = getattr(logprobs, "content", None)
        if content is None and isinstance(logprobs, dict):
            content = logprobs.get("content")
        if not content:
            return None
        values: list[float] = []
        for item in content:
            value = getattr(item, "logprob", None)
            if value is None and isinstance(item, dict):
                value = item.get("logprob")
            if isinstance(value, (int, float)) and math.isfinite(float(value)):
                values.append(float(value))
        if not values:
            return None
        likelihood = math.exp(sum(values) / len(values))
        return min(1.0, max(1e-300, likelihood))
    except (AttributeError, IndexError, TypeError, ValueError):
        return None


# ── Runner (child-process side) ──────────────────────────────────────


@dataclass
class _RunStats:
    turns: int = 0
    tool_calls: int = 0
    files_written: list[str] = field(default_factory=list)
    commands_run: list[str] = field(default_factory=list)
    reads: list[str] = field(default_factory=list)
    last_failed_output: str = ""
    turn_likelihoods: list[float] = field(default_factory=list)


def _emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _safe_resolve(root: Path, rel: str) -> Path:
    if not rel:
        raise ValueError("empty path")
    candidate = (root / rel).resolve()
    root_resolved = root.resolve()
    if candidate != root_resolved and root_resolved not in candidate.parents:
        raise ValueError(f"path escapes the working directory: {rel}")
    return candidate


def _normalise_rel_path(value: str) -> str:
    """Normalize a task-relative path and resolve parent segments lexically."""

    path = str(value or "").strip().replace("\\", "/")
    if not path:
        return ""
    normalized = posixpath.normpath(path)
    return "" if normalized == "." else normalized


def _env_path_list(name: str) -> tuple[str, ...]:
    """Read a comma/semicolon separated path list from the environment."""

    raw = os.environ.get(name, "").replace(";", ",")
    paths: list[str] = []
    for item in raw.split(","):
        normalized = _normalise_rel_path(item)
        if normalized:
            paths.append(normalized)
    return tuple(paths)


def _within_boundary(rel: str, boundary: tuple[str, ...]) -> bool:
    """Whether a task-relative path is inside any configured boundary prefix."""

    if not boundary:
        return True
    normalized = _normalise_rel_path(rel)
    if not normalized:
        return False
    return any(
        normalized == prefix or normalized.startswith(prefix + "/") for prefix in boundary
    )


def _result_is_error(result: str) -> bool:
    """Best-effort detection of an error tool payload."""

    try:
        payload = json.loads(result)
    except (TypeError, ValueError):
        return True
    return not isinstance(payload, dict) or "error" in payload


def _observe_drift_command(drift: DriftMonitor, result: str) -> None:
    """Feed a failed command's output to the repeated-trace detector."""

    try:
        payload = json.loads(result)
    except (TypeError, ValueError):
        return
    if not isinstance(payload, dict) or payload.get("exit_code") == 0:
        return
    output = payload.get("output")
    if isinstance(output, str) and output.strip():
        drift.observe_test(output, net_diff_lines=0)


def _failed_command_output(result: str) -> str:
    """Return bounded output for a failed command, or ``""`` on success."""

    try:
        payload = json.loads(result)
    except (TypeError, ValueError):
        return ""
    if not isinstance(payload, dict) or payload.get("exit_code") in (None, 0):
        return ""
    output = payload.get("output")
    return output[-12000:] if isinstance(output, str) and output.strip() else ""


def _inject_drift_advisor(messages: list[dict[str, Any]], drift: DriftMonitor) -> bool:
    """Append the one-shot advisor prompt once a deterministic signal is live."""

    prompt = drift.advisor_prompt(max_words=150)
    if not prompt:
        return False
    messages.append({"role": "system", "content": f"Advisor: {prompt}"})
    _emit(
        {
            "event": "drift_advisor",
            "signals": [signal.value for signal in drift.triggered()],
            "prompt": prompt,
        }
    )
    return True


def _radio_watcher_from_env() -> BackgroundMentionWatcher | None:
    """Build the opt-in in-process radio watcher, if all hints are present.

    This is deliberately the local-only half of AgentRadio: the harness can
    post worklog/thread messages and receive mentions seeded into the bus, but
    it does not own a NATS subscription.  A worker/orchestrator process is the
    production transport and can pass a seed inbox for this run.
    """

    if not radio_enabled():
        return None
    graph_id = os.environ.get(RADIO_GRAPH_ENV, "").strip()
    node_id = os.environ.get(RADIO_NODE_ENV, "").strip()
    if not graph_id or not node_id:
        return None
    bus = InMemoryRadioBus()
    try:
        watcher = BackgroundMentionWatcher(bus, graph_id=graph_id, node_id=node_id)
    except ValueError:
        return None
    raw = os.environ.get(RADIO_INBOX_ENV, "").strip()
    if raw:
        try:
            seed = json.loads(raw)
        except (TypeError, ValueError):
            seed = None
        if isinstance(seed, list):
            for item in seed:
                if not isinstance(item, dict):
                    continue
                try:
                    bus.publish(RadioMessage.from_dict(item))
                except (TypeError, ValueError):
                    continue
    return watcher


def _drain_radio_updates(
    messages: list[dict[str, Any]],
    watcher: BackgroundMentionWatcher | None,
) -> bool:
    """Inject drained teammate mentions at a prompt boundary."""

    if watcher is None:
        return False
    update = watcher.drain_for_prompt()
    if not update:
        return False
    messages.append({"role": "system", "content": update})
    _emit({"event": "teammate_radio_update", "text": update})
    return True


def _radio_post_result(
    watcher: BackgroundMentionWatcher | None,
    fn_args: dict[str, Any],
    *,
    thread: bool,
) -> str:
    """Handle the two radio tools; disabled/empty input becomes a tool error."""

    if watcher is None:
        return json.dumps({"error": "agent radio is not configured for this run"})
    text = str(fn_args.get("text") or "").strip()
    if not text:
        return json.dumps({"error": "radio text must not be empty"})
    try:
        if thread:
            message = watcher.create_thread(
                text[:2000],
                parent_id=str(fn_args.get("parent_id") or ""),
                target_node_id=str(fn_args.get("target_node_id") or "") or None,
            )
        else:
            message = watcher.post(text[:2000], metadata={"source": "local-harness"})
    except (TypeError, ValueError) as exc:
        return json.dumps({"error": f"{type(exc).__name__}: {exc}"})
    _emit({"event": "radio_post", "message": message.to_dict()})
    return json.dumps(
        {"ok": True, "message_id": message.message_id, "kind": message.kind}
    )


def _ecloop_blocked_result(decision: Any) -> str:
    """Render an ECLoop denial as a tool error the model can act on."""

    payload: dict[str, Any] = {"error": decision.to_tool_error()}
    if decision.gap is not None:
        payload["ecloop"] = decision.gap.to_dict()
    return json.dumps(payload, ensure_ascii=False)


def _emit_ecloop_block(action: str, target: str, decision: Any) -> None:
    _emit(
        {
            "event": "ecloop_blocked",
            "action": action,
            "target": target,
            "attempts": decision.attempts,
            "warning": decision.warning,
            "prompt": decision.prompt[:500],
        }
    )


def _emit_ecloop_warning(action: str, target: str, decision: Any) -> None:
    _emit(
        {
            "event": "ecloop_warning",
            "action": action,
            "target": target,
            "attempts": decision.attempts,
            "warning": decision.warning,
            "prompt": decision.prompt[:500],
        }
    )


def _tool_list_dir(root: Path, args: dict[str, Any]) -> str:
    target = _safe_resolve(root, str(args.get("path") or "."))
    if not target.is_dir():
        return json.dumps({"error": f"not a directory: {args.get('path')}"})
    entries = sorted((e.name + "/" if e.is_dir() else e.name) for e in target.iterdir())
    return json.dumps({"entries": entries[:500]}, ensure_ascii=False)


def _tool_read_file(
    root: Path,
    args: dict[str, Any],
    *,
    ledger: ReadLedger | None = None,
    turn: int = 0,
) -> str:
    rel = str(args.get("path") or "")
    target = _safe_resolve(root, rel)
    if not target.is_file():
        return json.dumps({"error": f"not a file: {args.get('path')}"})
    try:
        offset = int(args.get("offset", 0))
        max_bytes = int(args.get("max_bytes", DEFAULT_READ_BYTES))
    except (TypeError, ValueError):
        return json.dumps({"error": "offset and max_bytes must be integers"})
    if offset < 0 or not 1 <= max_bytes <= MAX_READ_BYTES:
        return json.dumps({"error": "invalid read range"})
    with target.open("rb") as source:
        source.seek(offset)
        data = source.read(max_bytes)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        if exc.reason != "unexpected end of data" or exc.end != len(data):
            return json.dumps({"error": "not a UTF-8 text file"})
        # Keep the paging offset on a character boundary.
        data = data[: exc.start]
        text = data.decode("utf-8")
    if ledger is not None:
        if ledger.duplicate(rel, offset, text):
            return json.dumps(
                {
                    "error": "duplicate read blocked",
                    "path": rel,
                    "offset": offset,
                    "hint": "this unchanged range is already in context; use the existing content",
                },
                ensure_ascii=False,
            )
        ledger.record(rel, offset, text, turn=turn)
    return json.dumps(
        {
            "content": text,
            "truncated": offset + len(data) < target.stat().st_size,
            "next_offset": offset + len(data),
        },
        ensure_ascii=False,
    )


def _tool_write_file(root: Path, args: dict[str, Any], stats: _RunStats) -> str:
    rel = str(args.get("path") or "")
    content = str(args.get("content") or "")
    if len(content.encode("utf-8")) > MAX_WRITE_BYTES:
        return json.dumps({"error": "content exceeds write limit"})
    target = _safe_resolve(root, rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="") as f:
        f.write(content)
    stats.files_written.append(rel)
    return json.dumps({"written": rel})


async def _tool_run_command(root: Path, args: dict[str, Any]) -> str:
    command = str(args.get("command") or "")
    if not command:
        return json.dumps({"error": "empty command"})
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as exc:
        return json.dumps({"error": f"spawn failed: {exc}"})
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=RUN_COMMAND_TIMEOUT_SECS)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return json.dumps({"error": "command timed out", "exit_code": -1})
    return json.dumps(
        {"exit_code": proc.returncode, "output": out.decode("utf-8", errors="replace")[-4000:]},
        ensure_ascii=False,
    )


def _completion(
    messages: list[dict[str, Any]],
    *,
    request_logprobs: bool = False,
) -> Any:
    """One litellm completion using the env-configured provider route."""
    import litellm

    model = os.environ.get("OPENAI_DEFAULT_MODEL", "openai/qwen3.8-9b-uc")
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "tools": _TOOLS,
        "timeout": float(os.environ.get("UC_LLM_TIMEOUT", "600")),
    }
    if request_logprobs:
        # The provider may ignore this; only a response that actually carries
        # token logprobs contributes a sample.
        kwargs["logprobs"] = True
    if os.environ.get("OPENAI_API_KEY"):
        kwargs["api_key"] = os.environ["OPENAI_API_KEY"]
    try:
        return litellm.completion(**kwargs)
    except Exception as exc:
        if not request_logprobs:
            raise
        # Some providers reject the optional ``logprobs`` request parameter
        # outright.  TRACE is diagnostic, so keep the run alive by making one
        # retry without the parameter instead of failing the whole subtask.
        rejected = isinstance(exc, litellm.UnsupportedParamsError) or (
            isinstance(exc, litellm.BadRequestError)
            and "logprob" in str(exc).lower()
        )
        if not rejected:
            raise
        _emit(
            {
                "event": "logprobs_unavailable",
                "error": f"{type(exc).__name__}: {exc}"[:300],
            }
        )
        kwargs.pop("logprobs", None)
        return litellm.completion(**kwargs)


async def run_loop(prompt: str, cwd: str, max_turns: int) -> int:
    """The agent loop. Returns a process exit code (0 = success)."""
    root = Path(cwd).resolve()
    stats = _RunStats()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    # ACP folding is opt-in.  The ledger exists only while folding is enabled,
    # so the legacy harness path (and its prompts) stays byte-identical by
    # default; when enabled, duplicate reads and over-budget intervals are
    # handled in-loop instead of being sent again to the model.
    folder = ACPFolder() if folding_enabled() else None
    ledger = ReadLedger() if folder is not None else None
    # Drift supervision is also opt-in; without the flag the loop below runs
    # exactly the legacy path.  Boundary and required-file hints are explicit
    # env inputs because the harness has no planner contract of its own.
    drift = DriftMonitor() if drift_monitor_enabled() else None
    drift_boundary = _env_path_list(DRIFT_BOUNDARY_ENV)
    drift_required = _env_path_list(DRIFT_REQUIRED_FILES_ENV)
    ecloop = ECLoopGate() if ecloop_gate_enabled() else None
    radio_watcher = _radio_watcher_from_env()
    request_logprobs = trace_logprobs_enabled()

    final_summary = ""
    hit_done = False
    try:
        while stats.turns < max_turns:
            stats.turns += 1
            _drain_radio_updates(messages, radio_watcher)
            if drift is not None and drift_required:
                drift.observe_budget(
                    stats.turns,
                    max_turns,
                    drift_required,
                    stats.files_written,
                )
                _inject_drift_advisor(messages, drift)
            if request_logprobs:
                response = await asyncio.to_thread(
                    _completion,
                    messages,
                    request_logprobs=True,
                )
            else:
                response = await asyncio.to_thread(_completion, messages)
            message = response.choices[0].message
            assistant_text = (getattr(message, "content", None) or "").strip()
            if assistant_text:
                _emit({"event": "assistant", "text": assistant_text[:400]})
            if request_logprobs:
                likelihood = _answer_likelihood(response)
                if likelihood is not None and len(stats.turn_likelihoods) < MAX_TURN_LIKELIHOODS:
                    stats.turn_likelihoods.append(likelihood)
                    _emit(
                        {
                            "event": "turn_likelihood",
                            "turn": stats.turns,
                            "likelihood": likelihood,
                        }
                    )

            tool_calls = list(getattr(message, "tool_calls", None) or [])
            usage = getattr(response, "usage", None)
            if usage is not None:
                _emit(
                    {
                        "event": "usage",
                        "turn": stats.turns,
                        "prompt_tokens": getattr(usage, "prompt_tokens", 0),
                        "completion_tokens": getattr(usage, "completion_tokens", 0),
                    }
                )
            if not tool_calls:
                final_summary = assistant_text or "Local harness finished"
                hit_done = True
                break

            messages.append(
                {
                    "role": "assistant",
                    "content": assistant_text or None,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in tool_calls
                    ],
                }
            )

            finished = False
            for tc in tool_calls:
                name = tc.function.name
                stats.tool_calls += 1
                try:
                    fn_args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    fn_args = {}
                _emit({"event": "tool_call", "tool": name, "args_preview": str(fn_args)[:200]})
                try:
                    if name == "list_dir":
                        result = _tool_list_dir(root, fn_args)
                    elif name == "read_file":
                        result = _tool_read_file(
                            root,
                            fn_args,
                            ledger=ledger,
                            turn=stats.turns,
                        )
                        if not _result_is_error(result):
                            read_path = _normalise_rel_path(str(fn_args.get("path") or ""))
                            if read_path:
                                stats.reads.append(read_path)
                    elif name == "write_file":
                        rel = str(fn_args.get("path") or "")
                        if drift is not None:
                            drift.observe_step(
                                "write",
                                _within_boundary(rel, drift_boundary),
                                within_two_hop=False,
                            )
                        if ecloop is not None:
                            decision = ecloop.evaluate(rel, "", "edit", stats.reads)
                            if decision.allowed:
                                result = _tool_write_file(root, fn_args, stats)
                                if decision.warning:
                                    _emit_ecloop_warning("edit", rel, decision)
                            else:
                                _emit_ecloop_block("edit", rel, decision)
                                result = _ecloop_blocked_result(decision)
                        else:
                            result = _tool_write_file(root, fn_args, stats)
                    elif name == "run_command":
                        command = str(fn_args.get("command") or "")
                        if command:
                            stats.commands_run.append(command)
                            _emit({"event": "command", "turn": stats.turns, "command": command})
                        result = await _tool_run_command(root, fn_args)
                        failed_output = _failed_command_output(result)
                        if failed_output:
                            stats.last_failed_output = failed_output
                        if drift is not None:
                            _observe_drift_command(drift, result)
                    elif name == "uc_radio_post":
                        result = _radio_post_result(radio_watcher, fn_args, thread=False)
                    elif name == "uc_radio_create_thread":
                        result = _radio_post_result(radio_watcher, fn_args, thread=True)
                    elif name == "done":
                        blocked = None
                        if ecloop is not None:
                            for target in dict.fromkeys(stats.files_written):
                                candidate = ecloop.evaluate(
                                    target, "", "submit", stats.reads
                                )
                                if not candidate.allowed:
                                    blocked = candidate
                                    break
                                if candidate.warning:
                                    _emit_ecloop_warning("submit", target, candidate)
                        if blocked is not None:
                            _emit_ecloop_block(
                                "submit",
                                str(blocked.gap.target if blocked.gap else ""),
                                blocked,
                            )
                            result = _ecloop_blocked_result(blocked)
                        else:
                            final_summary = str(fn_args.get("summary") or "Task completed")
                            finished = True
                            hit_done = True
                            result = json.dumps({"ok": True})
                    else:
                        result = json.dumps({"error": f"unknown tool: {name}"})
                except Exception as exc:  # tool bug must not kill the loop
                    result = json.dumps({"error": f"{type(exc).__name__}: {exc}"})
                if drift is not None and name == "write_file":
                    drift.observe_edit(not _result_is_error(result))
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
            _drain_radio_updates(messages, radio_watcher)
            if folder is not None:
                folded = folder.maybe_fold(
                    messages,
                    read_ledger=ledger,
                    turn=stats.turns,
                )
                if folded.folded and folded.checkpoint is not None:
                    messages = folded.messages
                    _emit(
                        {
                            "event": "context_fold",
                            "turn": stats.turns,
                            "folded_messages": folded.folded_messages,
                            "saved_tokens": folded.saved_tokens,
                            "checkpoint_id": folded.checkpoint.checkpoint_id,
                        }
                    )
            if drift is not None and not finished:
                _inject_drift_advisor(messages, drift)
            if finished:
                break
        else:
            final_summary = f"Stopped after max turns ({max_turns}) without done()"
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        _emit(
            {
                "event": "final",
                "success": False,
                "summary": f"Local harness failed: {type(exc).__name__}: {exc}",
                "turns": stats.turns,
                "tool_calls": stats.tool_calls,
                "test_output": stats.last_failed_output,
            }
        )
        return 1

    envelope: dict[str, Any] = {
        "event": "final",
        "success": hit_done,
        "summary": final_summary,
        "turns": stats.turns,
        "tool_calls": stats.tool_calls,
        "files_written": stats.files_written,
        "commands": stats.commands_run,
    }
    if drift is not None:
        envelope["drift_signals"] = [signal.value for signal in drift.triggered()]
    if ecloop is not None:
        envelope["ecloop_blocks"] = dict(ecloop.blocks)
    if stats.turn_likelihoods:
        envelope["turn_likelihoods"] = list(stats.turn_likelihoods)
    if stats.last_failed_output:
        envelope["test_output"] = stats.last_failed_output
    _emit(envelope)
    return 0 if hit_done else 1


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=os.environ.get("UC_LOG_LEVEL", "WARNING"))
    parser = argparse.ArgumentParser(
        prog="python -m ultimate_coders.agent.harness_local_loop",
        description="Lightweight local-LLM coding harness runner",
    )
    parser.add_argument("--prompt-file", required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--max-turns", type=int, default=DEFAULT_MAX_TURNS)
    args = parser.parse_args(argv)
    try:
        prompt = Path(args.prompt_file).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"cannot read prompt file: {exc}", file=sys.stderr)
        return 2
    finally:
        try:
            os.unlink(args.prompt_file)
        except OSError:
            pass
    return asyncio.run(run_loop(prompt, args.cwd, args.max_turns))


if __name__ == "__main__":
    sys.exit(main())
