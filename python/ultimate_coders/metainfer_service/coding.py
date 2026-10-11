#!/usr/bin/env python3
"""Opt-in native Ollama client for the pinned upstream's Codex event interface."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path

from ultimate_coders.inference.process import spawn_command
from ultimate_coders.metainfer_service.observations import read_json
from ultimate_coders.metainfer_service.ollama import MAX_RESPONSE, OllamaProfile

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "exec_command",
            "description": "Execute a shell command in the workspace and return actual output.",
            "parameters": {
                "type": "object",
                "properties": {"cmd": {"type": "string"}},
                "required": ["cmd"],
            },
        },
    }
]


def require_containment() -> None:
    status = Path("/proc/self/status").read_text()
    if (
        os.getuid() == 0
        or not (Path("/run/.containerenv").is_file() or Path("/.dockerenv").is_file())
        or not os.statvfs("/").f_flag & os.ST_RDONLY
        or "NoNewPrivs:\t1" not in status
        or any(
            int(line.split()[1], 16) != 0
            for line in status.splitlines()
            if line.startswith(("CapEff:", "CapPrm:", "CapBnd:", "CapAmb:"))
        )
    ):
        raise ValueError("Native coding requires an unprivileged contained job")


def emit(kind: str, **fields) -> None:
    print(json.dumps({"type": kind, **fields}), flush=True)


async def execute_command(
    command: str, workspace: Path, timeout: float = 60, *, deadline: float | None = None
) -> dict:
    """Drain bounded tails; the shared process boundary kills the command group."""
    if not isinstance(command, str) or not command or len(command) > 65536:
        raise ValueError("Invalid native shell tool arguments")
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Native coding turn deadline exceeded before tool launch")
        timeout = min(timeout, remaining)
    proc, tree = await spawn_command(
        ["bash", "-lc", command],
        cwd=workspace,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output = bytearray()

    async def drain() -> None:
        while chunk := await proc.stdout.read(4096):
            output.extend(chunk)
            del output[:-16000]

    reader = asyncio.create_task(drain())
    try:
        await asyncio.wait_for(proc.wait(), timeout)
    except asyncio.TimeoutError:
        return {"exit_code": 124, "output": "Native command deadline exceeded"}
    finally:
        tree.close()
        try:
            await asyncio.wait_for(reader, 2)
        except asyncio.TimeoutError:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        await proc.wait()
    return {"exit_code": proc.returncode, "output": output.decode(errors="replace")}


def save_session(directory: Path, thread: str, profile: OllamaProfile, messages: list) -> None:
    content = json.dumps({"identity": profile.identity(), "messages": messages}).encode()
    if len(content) > MAX_RESPONSE:
        raise ValueError("Native conversation exceeds the session bound")
    temporary = directory / (thread + "." + uuid.uuid4().hex + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as writer:
        writer.write(content)
    os.replace(temporary, directory / (thread + ".json"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-C", type=Path, default=Path.cwd())
    parser.add_argument("--model")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--skip-git-repo-check", action="store_true")
    parser.add_argument("--color")
    parser.add_argument("--sandbox", choices=["workspace-write"])
    parser.add_argument("--add-dir", action="append")
    parser.add_argument("prompt")
    arguments, resume = list(sys.argv[1:] if argv is None else argv), None
    if arguments and arguments[0] == "exec":
        arguments = arguments[1:]
        if arguments and arguments[0] == "resume":
            arguments = arguments[1:]
            resume = str(uuid.UUID(arguments[-2]))
            arguments = arguments[:-2] + ["-"]
    args = parser.parse_args(arguments)
    require_containment()
    profile = OllamaProfile.from_environment(dict(os.environ))
    if args.model and args.model != profile.model:
        raise ValueError("Native coding cannot override the attested model")
    prompt = sys.stdin.read(MAX_RESPONSE + 1) if args.prompt == "-" else args.prompt
    if not prompt.strip() or len(prompt.encode()) > MAX_RESPONSE:
        raise ValueError("Invalid native coding prompt")
    workspace = args.C.resolve(strict=True)
    thread = resume or str(uuid.uuid4())
    directory = Path("/tmp/uc-ollama-sessions")
    directory.mkdir(mode=0o700, exist_ok=True)
    if resume:
        saved = read_json(directory, thread + ".json", None)
        if not saved or saved.get("identity") != profile.identity():
            raise ValueError("Native session model identity changed or is unavailable")
        messages = saved["messages"]
    else:
        messages = [
            {
                "role": "system",
                "content": "Use exec_command for actual coding work in the current directory. "
                "Verify tool results before claiming completion. Do not use the network. "
                "When reviewing, judge only the documented input contract of the reference kernel.",
            }
        ]
    messages.append({"role": "user", "content": prompt})
    emit("thread.started", thread_id=thread)
    emit("turn.started")
    deadline, executed = time.monotonic() + 900, 0
    for turn in range(16):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Native coding turn deadline exceeded")
        data = profile.chat(messages, tools=TOOLS, timeout=min(600, remaining))
        message = data["message"]
        messages.append(message)
        save_session(directory, thread, profile, messages)
        calls = message.get("tool_calls", [])
        if not calls:
            if not message.get("content", "").strip():
                raise ValueError("Native coding returned an empty final response")
            emit(
                "item.completed",
                item={"id": str(turn), "type": "agent_message", "text": message["content"]},
            )
            emit(
                "turn.completed",
                usage={
                    "input_tokens": data.get("prompt_eval_count", 0),
                    "output_tokens": data.get("eval_count", 0),
                },
                actual_tools_executed=executed,
                model_digest=profile.digest,
            )
            return 0
        if not isinstance(calls, list) or len(calls) > 16:
            raise ValueError("Native tool batch exceeds its bound")
        for call in calls:
            function = call.get("function", {})
            if function.get("name") != "exec_command" or not isinstance(
                function.get("arguments"), dict
            ):
                raise ValueError("Unsupported native tool call")
            result = asyncio.run(
                execute_command(function["arguments"].get("cmd"), workspace, deadline=deadline)
            )
            executed += 1
            emit(
                "item.completed",
                item={
                    "id": str(executed),
                    "type": "command_execution",
                    "command": function["arguments"]["cmd"],
                    "aggregated_output": result["output"],
                    "exit_code": result["exit_code"],
                    "status": "completed" if result["exit_code"] == 0 else "failed",
                },
            )
            messages.append(
                {"role": "tool", "tool_name": "exec_command", "content": json.dumps(result)}
            )
            save_session(directory, thread, profile, messages)
    raise RuntimeError("Native coding exceeded the turn limit")


if __name__ == "__main__":
    raise SystemExit(main())
