#!/usr/bin/env python3
"""Opt-in native Ollama client for the pinned upstream's Codex event interface."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import stat
import sys
import time
import uuid
from pathlib import Path

from ultimate_coders.inference.process import spawn_command
from ultimate_coders.metainfer_service.observations import read_json
from ultimate_coders.metainfer_service.ollama import (
    DEFAULT_CONTEXT,
    DEFAULT_PREDICT,
    MAX_RESPONSE,
    OllamaProfile,
)

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
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write the complete UTF-8 contents of a requested artifact in the "
            "current workspace. Use this for harnesses, code and review files before finishing.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        },
    },
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


def validate_review(text: str) -> None:
    """Require one leading verdict that the pinned parser reads deterministically."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    formats = {
        "**Verdict**: PASS", "**Verdict**: FAIL", "- **Verdict**: PASS", "- **Verdict**: FAIL",
    }
    declarations = [
        line for line in lines
        if re.match(r"^(?:#{1,6}\s+|-\s+)?(?:\*\*)?Verdict(?:\*\*)?\s*:", line, re.I)
    ]
    if not lines or lines[0] not in formats or len(declarations) != 1:
        raise ValueError("Native review requires one leading '**Verdict**: PASS' or FAIL verdict")


def verify_review_files(workspace: Path) -> None:
    """Observe bounded existing review bytes without following links or rewriting them."""
    directory = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for name in ("correctness_review.md", "perf_review.md"):
            try:
                descriptor = os.open(
                    name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory,
                )
            except FileNotFoundError:
                continue
            with os.fdopen(descriptor, "rb") as reader:
                info = os.fstat(reader.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
                    raise ValueError("Native review must be a bounded regular file")
                data = reader.read(65537)
                if len(data) > 65536:
                    raise ValueError("Native review exceeds its bound")
                validate_review(data.decode("utf-8"))
    finally:
        os.close(directory)


def write_file(path: str, content: str, workspace: Path) -> dict:
    """Atomically write actual model bytes through a no-follow workspace directory."""
    if os.name != "posix" or not isinstance(path, str) or not path or len(path) > 4096:
        raise ValueError("Invalid native file tool path")
    if not isinstance(content, str) or len(content.encode()) > 65536:
        raise ValueError("Native file contents exceed the bound")
    relative = Path(path)
    if relative.is_absolute():
        relative = relative.relative_to(workspace)
    if not relative.parts or any(part == ".." for part in relative.parts):
        raise ValueError("Native file must remain inside its workspace")
    directory = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = ".uc-write-" + uuid.uuid4().hex
    data = content.encode()
    try:
        for part in relative.parts[:-1]:
            try:
                os.mkdir(part, dir_fd=directory)
            except FileExistsError:
                pass
            nested = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = nested
        try:
            original = os.stat(relative.name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            original = None
        if original is not None and not stat.S_ISREG(original.st_mode):
            raise ValueError("Native artifact target must be a regular file")
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
            dir_fd=directory,
        )
        with os.fdopen(descriptor, "wb") as writer:
            os.fchmod(writer.fileno(), stat.S_IMODE(original.st_mode) if original else 0o644)
            writer.write(data)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(temporary, relative.name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        try:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
        finally:
            os.close(directory)
    return {
        "path": str(workspace / relative), "kind": "update" if original else "add",
        "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
    }


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
    content = json.dumps(
        {"identity": profile.identity(), "num_ctx": profile.num_ctx,
         "num_predict": profile.num_predict, "messages": messages}
    ).encode()
    if len(content) > MAX_RESPONSE:
        raise ValueError("Native conversation exceeds the session bound")
    temporary = directory / (thread + "." + uuid.uuid4().hex + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as writer:
        writer.write(content)
    os.replace(temporary, directory / (thread + ".json"))


def load_session(directory: Path, thread: str, profile: OllamaProfile) -> list:
    saved = read_json(directory, thread + ".json", None)
    if (
        not isinstance(saved, dict)
        or saved.get("identity") != profile.identity()
        or saved.get("num_ctx", DEFAULT_CONTEXT) != profile.num_ctx
        or saved.get("num_predict", DEFAULT_PREDICT) != profile.num_predict
        or not isinstance(saved.get("messages"), list)
    ):
        raise ValueError(
            "Native session model identity, context or output budget changed or unavailable"
        )
    return saved["messages"]


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
        messages = load_session(directory, thread, profile)
    else:
        messages = [
            {
                "role": "system",
                "content": "Use exec_command to inspect and test code, and write_file to create "
                "requested code, harness and review artifacts in the current directory. "
                "When asked to write a file, make a real tool call before the final response; "
                "chat text does not create that file. Verify tool results before finishing. "
                "Do not use the network. When generating or reviewing test harnesses, use "
                "the documented callable and valid input contract; explicitly excluded inputs "
                "are not missing requirements. Load a supplied reference from its existing "
                "file with importlib.util, preserving source availability for Triton JIT; "
                "never embed, reimplement or substitute that reference. Execute a generated "
                "correctness harness against the supplied reference before finishing, inspect "
                "its actual JSON results, and fix observed failures. Reviewers should run the "
                "existing harness and record actual observations. Keep artifacts focused and "
                "concise without reducing the requested coverage or changing output contracts. "
                "Start correctness_review.md and perf_review.md with exactly '**Verdict**: PASS' "
                "or '**Verdict**: FAIL' as their first nonblank line, "
                "with no other verdict declarations.",
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
            verify_review_files(workspace)
            emit(
                "item.completed",
                item={"id": "message-" + str(turn), "type": "agent_message",
                      "text": message["content"]},
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
            name = function.get("name")
            if name not in ("exec_command", "write_file") or not isinstance(
                function.get("arguments"), dict
            ):
                raise ValueError("Unsupported native tool call")
            if name == "write_file":
                if time.monotonic() >= deadline:
                    raise TimeoutError("Native coding turn deadline exceeded before file write")
                result = write_file(
                    function["arguments"].get("path"),
                    function["arguments"].get("content"),
                    workspace,
                )
                executed += 1
                emit(
                    "item.completed",
                    item={"id": "tool-" + str(executed), "type": "file_change",
                          "status": "completed",
                          "changes": [{"path": result["path"], "kind": result["kind"]}]},
                )
                messages.append({"role": "tool", "tool_name": name, "content": json.dumps(result)})
                save_session(directory, thread, profile, messages)
                continue
            result = asyncio.run(
                execute_command(function["arguments"].get("cmd"), workspace, deadline=deadline)
            )
            executed += 1
            emit(
                "item.completed",
                item={
                    "id": "tool-" + str(executed),
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
