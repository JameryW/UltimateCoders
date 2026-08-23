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
- Final envelope (last line): {"event": "final", "success": bool, ...}

Selection:
    UC_CODING_AGENT=local-harness   (alias: local-llm)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ultimate_coders.agent.sandbox import AgentAdapter, AgentOutput, ExecResult, SandboxConfig

logger = logging.getLogger(__name__)

AGENT_NAME = "local-harness"
AGENT_ALIASES = ("local-llm",)
DEFAULT_MAX_TURNS = 30
RUN_COMMAND_TIMEOUT_SECS = 120
MAX_READ_BYTES = 256 * 1024
MAX_WRITE_BYTES = 256 * 1024

# Deliberately SHORT — large instruction blobs are what break small models.
_SYSTEM_PROMPT = """\
You are a coding agent completing one task in a repository working directory.

Work step by step with your tools:
- list_dir / read_file to inspect the code
- write_file to make edits
- run_command for builds/tests
- done (with a summary) when finished

Rules: stay inside the working directory; make minimal edits; always finish
by calling done.
"""

_TOOLS: list[dict[str, Any]] = [
    {"type": "function", "function": {
        "name": "list_dir",
        "description": "List a directory (relative path, '.' = root).",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read a UTF-8 text file (relative path).",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "write_file",
        "description": "Create or overwrite a UTF-8 file (relative path).",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "run_command",
        "description": "Run a short shell command (build/test/lint).",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string"}}, "required": ["command"]}}},
    {"type": "function", "function": {
        "name": "done",
        "description": "Finish with a one-paragraph summary.",
        "parameters": {"type": "object", "properties": {
            "summary": {"type": "string"}}, "required": ["summary"]}}},
]


def register(reg: Any) -> None:
    """Register into a plugin registry (idempotent)."""
    from ultimate_coders.agent.registry import AgentPluginSpec

    reg.register(AgentPluginSpec(
        name=AGENT_NAME,
        aliases=AGENT_ALIASES,
        factory=LocalLoopHarnessAdapter,
        api_key_env=None,  # provider comes from the litellm env route
        cli_probe=None,    # API-backed, no external CLI
        description=(
            "Lightweight litellm tool loop tuned for small local models "
            "(Ollama/vLLM); uses the UC_LLM_PROVIDER/OPENAI_API_BASE route"
        ),
    ))


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
        return {
            "command": sys.executable,
            "args": [
                "-m", "ultimate_coders.agent.harness_local_loop",
                "--cwd", working_dir,
                "--prompt-file", prompt_file.name,
                "--max-turns", str(max_turns),
            ],
            "timeout_secs": config.max_cpu_seconds,
            "working_dir": working_dir,
            "env_vars": config._build_env_vars(),
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
        tool_calls = [
            json.loads(line)["tool"]
            for line in result.stdout.splitlines()
            if _is_event(line, "tool_call")
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
            )

        return AgentOutput(
            summary=str(envelope.get("summary") or "Local harness completed"),
            success=bool(envelope.get("success", result.exit_code == 0)),
            stderr_tail=stderr_tail,
            tool_calls=tool_calls,
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


# ── Runner (child-process side) ──────────────────────────────────────


@dataclass
class _RunStats:
    turns: int = 0
    tool_calls: int = 0
    files_written: list[str] = field(default_factory=list)


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


def _tool_list_dir(root: Path, args: dict[str, Any]) -> str:
    target = _safe_resolve(root, str(args.get("path") or "."))
    if not target.is_dir():
        return json.dumps({"error": f"not a directory: {args.get('path')}"})
    entries = sorted(
        (e.name + "/" if e.is_dir() else e.name) for e in target.iterdir()
    )
    return json.dumps({"entries": entries[:500]}, ensure_ascii=False)


def _tool_read_file(root: Path, args: dict[str, Any]) -> str:
    target = _safe_resolve(root, str(args.get("path") or ""))
    if not target.is_file():
        return json.dumps({"error": f"not a file: {args.get('path')}"})
    data = target.read_bytes()[:MAX_READ_BYTES]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return json.dumps({"error": "not a UTF-8 text file"})
    return json.dumps(
        {"content": text, "truncated": len(data) == MAX_READ_BYTES},
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
        {"exit_code": proc.returncode,
         "output": out.decode("utf-8", errors="replace")[-4000:]},
        ensure_ascii=False,
    )


def _completion(messages: list[dict[str, Any]]) -> Any:
    """One litellm completion using the env-configured provider route."""
    import litellm

    model = os.environ.get("OPENAI_DEFAULT_MODEL", "openai/qwen3.8-9b-uc")
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "tools": _TOOLS,
        "timeout": float(os.environ.get("UC_LLM_TIMEOUT", "600")),
    }
    if os.environ.get("OPENAI_API_KEY"):
        kwargs["api_key"] = os.environ["OPENAI_API_KEY"]
    return litellm.completion(**kwargs)


async def run_loop(prompt: str, cwd: str, max_turns: int) -> int:
    """The agent loop. Returns a process exit code (0 = success)."""
    root = Path(cwd).resolve()
    stats = _RunStats()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]

    final_summary = ""
    hit_done = False
    try:
        while stats.turns < max_turns:
            stats.turns += 1
            response = await asyncio.to_thread(_completion, messages)
            message = response.choices[0].message
            assistant_text = (getattr(message, "content", None) or "").strip()
            if assistant_text:
                _emit({"event": "assistant", "text": assistant_text[:400]})

            tool_calls = list(getattr(message, "tool_calls", None) or [])
            if not tool_calls:
                final_summary = assistant_text or "Local harness finished"
                hit_done = True
                break

            messages.append({
                "role": "assistant",
                "content": assistant_text or None,
                "tool_calls": [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name,
                                  "arguments": tc.function.arguments}}
                    for tc in tool_calls
                ],
            })
            usage = getattr(response, "usage", None)
            if usage is not None:
                _emit({"event": "usage", "turn": stats.turns,
                       "prompt_tokens": getattr(usage, "prompt_tokens", 0),
                       "completion_tokens": getattr(usage, "completion_tokens", 0)})

            finished = False
            for tc in tool_calls:
                name = tc.function.name
                stats.tool_calls += 1
                try:
                    fn_args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    fn_args = {}
                _emit({"event": "tool_call", "tool": name,
                       "args_preview": str(fn_args)[:200]})
                try:
                    if name == "list_dir":
                        result = _tool_list_dir(root, fn_args)
                    elif name == "read_file":
                        result = _tool_read_file(root, fn_args)
                    elif name == "write_file":
                        result = _tool_write_file(root, fn_args, stats)
                    elif name == "run_command":
                        result = await _tool_run_command(root, fn_args)
                    elif name == "done":
                        final_summary = str(fn_args.get("summary") or "Task completed")
                        finished = True
                        hit_done = True
                        result = json.dumps({"ok": True})
                    else:
                        result = json.dumps({"error": f"unknown tool: {name}"})
                except Exception as exc:  # tool bug must not kill the loop
                    result = json.dumps({"error": f"{type(exc).__name__}: {exc}"})
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
            if finished:
                break
        else:
            final_summary = f"Stopped after max turns ({max_turns}) without done()"
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        _emit({"event": "final", "success": False,
               "summary": f"Local harness failed: {type(exc).__name__}: {exc}",
               "turns": stats.turns, "tool_calls": stats.tool_calls})
        return 1

    _emit({"event": "final", "success": hit_done, "summary": final_summary,
           "turns": stats.turns, "tool_calls": stats.tool_calls,
           "files_written": stats.files_written})
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
