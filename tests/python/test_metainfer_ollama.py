"""Native Ollama protocol checks use a real local HTTP peer."""

import json
import os
import shlex
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from ultimate_coders.metainfer_service.ollama import OllamaProfile


@pytest.fixture
def ollama_peer():
    observed = {"digest": "a" * 64, "chunks": [], "requests": [], "trickle": None,
                "responses": None, "header_delay": 0}
    stop = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):  # noqa: N802 -- HTTP interface
            payload = {"models": [{"name": "coding:test", "digest": observed["digest"]}]}
            self.send_response(200)
            self.end_headers()
            if observed["trickle"] == "tags":
                self.trickle()
                return
            self.wfile.write(json.dumps(payload).encode())

        def do_POST(self):  # noqa: N802 -- HTTP interface
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            observed["requests"].append(payload)
            if stop.wait(observed["header_delay"]):
                return
            self.send_response(200)
            self.end_headers()
            if observed["trickle"] == "chat":
                self.trickle()
                return
            chunks = observed["responses"].pop(0) if observed["responses"] else observed["chunks"]
            for chunk in chunks:
                self.wfile.write(json.dumps(chunk).encode() + b"\n")

        def trickle(self):
            try:
                while not stop.wait(0.02):
                    self.wfile.write(b" ")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield (
            OllamaProfile(f"http://127.0.0.1:{server.server_port}", "coding:test", "a" * 64),
            observed,
        )
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_chat_uses_pinned_model_and_preserves_actual_streamed_tools(ollama_peer):
    profile, peer = ollama_peer
    call = {"function": {"name": "exec_command", "arguments": {"cmd": "echo verified"}}}
    peer["chunks"] = [
        {"model": "coding:test", "message": {"content": "Run", "tool_calls": [call]}},
        {"model": "coding:test", "message": {"content": " complete"}, "done": True},
    ]
    result = profile.chat([{"role": "user", "content": "Verify"}], tools=[])
    assert result["message"]["content"] == "Run complete"
    assert result["message"]["tool_calls"] == [call]
    assert peer["requests"][0]["model"] == "coding:test"
    assert peer["requests"][0]["options"]["num_ctx"] == 16384
    peer["digest"] = "b" * 64
    with pytest.raises(ValueError, match="digest"):
        profile.chat([], tools=[])
    assert len(peer["requests"]) == 1


@pytest.mark.parametrize(
    "chunks",
    [
        [{"model": "other:model", "message": {}, "done": True}],
        [{"model": "coding:test", "message": {"content": "unfinished"}}],
    ],
)
def test_chat_refuses_changed_model_and_incomplete_stream(ollama_peer, chunks):
    profile, peer = ollama_peer
    peer["chunks"] = chunks
    with pytest.raises(ValueError):
        profile.chat([], tools=[])


def test_text_describing_a_tool_is_preserved_as_text(ollama_peer):
    profile, peer = ollama_peer
    peer["chunks"] = [
        {"model": "coding:test", "message": {"content": "exec_command(echo ok)"}, "done": True}
    ]
    result = profile.chat([], tools=[])
    assert result["message"]["tool_calls"] == []


@pytest.mark.parametrize("tool", [False, True])
def test_output_exhaustion_refuses_nonempty_text_and_complete_tool_calls(ollama_peer, tool):
    profile, peer = ollama_peer
    message = {"content": "This response was truncated."}
    if tool:
        message["tool_calls"] = [{"function": {"name": "write_file", "arguments": {
            "path": "review.md", "content": "Verdict: PASS\n"}}}]
    peer["chunks"] = [{"model": profile.model, "message": message,
                       "done": True, "done_reason": "length"}]
    with pytest.raises(ValueError):
        profile.chat([], tools=[])


@pytest.mark.parametrize("context", [1024, 4096, 32768])
def test_explicit_context_profile_reaches_the_actual_provider_request(ollama_peer, context):
    profile, peer = ollama_peer
    peer["chunks"] = [
        {"model": "coding:test", "message": {"content": "verified"}, "done": True}
    ]
    configured = OllamaProfile.from_environment(
        {
            "UC_OLLAMA_URL": profile.url,
            "UC_OLLAMA_MODEL": profile.model,
            "UC_OLLAMA_DIGEST": profile.digest,
            "UC_OLLAMA_NUM_CTX": str(context),
        }
    )
    configured.chat([], tools=[])
    assert peer["requests"][0]["options"]["num_ctx"] == context
    assert peer["requests"][0]["model"] == profile.model
    assert configured.identity()["digest"] == profile.digest


def test_explicit_output_budget_reaches_provider_without_changing_legacy_default(ollama_peer):
    profile, peer = ollama_peer
    peer["chunks"] = [
        {"model": profile.model, "message": {"content": "verified"}, "done": True}
    ]
    profile.chat([], tools=[])
    assert peer["requests"][0]["options"]["num_predict"] == 4096
    configured = OllamaProfile.from_environment({
        "UC_OLLAMA_URL": profile.url, "UC_OLLAMA_MODEL": profile.model,
        "UC_OLLAMA_DIGEST": profile.digest, "UC_OLLAMA_NUM_PREDICT": "8192",
    })
    configured.chat([], tools=[])
    assert peer["requests"][1]["options"]["num_predict"] == 8192


@pytest.mark.parametrize("setting,expected", [(None, False), ("false", False), ("true", True)])
def test_explicit_thinking_mode_reaches_provider_and_preserves_separate_fields(
    ollama_peer, setting, expected
):
    profile, peer = ollama_peer
    environment = {"UC_OLLAMA_URL": profile.url, "UC_OLLAMA_MODEL": profile.model,
                   "UC_OLLAMA_DIGEST": profile.digest}
    if setting is not None:
        environment["UC_OLLAMA_THINK"] = setting
    configured = OllamaProfile.from_environment(environment)
    call = {"function": {"name": "write_file", "arguments": {"path": "actual.txt", "content": "x"}}}
    peer["chunks"] = [
        {"model": profile.model, "message": {"thinking": "private reasoning"}},
        {"model": profile.model, "message": {"content": "final", "tool_calls": [call]},
         "done": True},
    ]
    result = configured.chat([], tools=[])
    assert peer["requests"][0]["think"] is expected
    assert result["message"]["content"] == "final"
    assert result["message"]["thinking"] == "private reasoning"
    assert result["message"]["tool_calls"] == [call]
    assert configured.identity() == profile.identity()


@pytest.mark.parametrize("setting", ["", "1", "yes", "TRUE", "auto", True, None])
def test_thinking_environment_refuses_ambiguous_values_before_http(setting):
    with pytest.raises(ValueError, match="UC_OLLAMA_THINK"):
        OllamaProfile.from_environment({"UC_OLLAMA_THINK": setting})


@pytest.mark.parametrize("setting", [1, 0, "true", None])
def test_thinking_profile_requires_a_boolean(setting):
    with pytest.raises(ValueError, match="profile"):
        OllamaProfile("http://localhost", "coding:test", "a" * 64, think=setting)


@pytest.mark.skipif(os.name != "posix", reason="Native sessions require Linux no-follow files")
def test_native_session_binds_thinking_mode_and_legacy_false(tmp_path, ollama_peer):
    from ultimate_coders.metainfer_service.coding import load_session, save_session

    profile, _ = ollama_peer
    messages = [{"role": "user", "content": "saved"}]
    thinking = OllamaProfile(profile.url, profile.model, profile.digest, think=True)
    save_session(tmp_path, "thinking", thinking, messages)
    assert json.loads((tmp_path / "thinking.json").read_text())["think"] is True
    assert load_session(tmp_path, "thinking", thinking) == messages
    with pytest.raises(ValueError, match="thinking"):
        load_session(tmp_path, "thinking", profile)
    (tmp_path / "legacy.json").write_text(
        json.dumps({"identity": profile.identity(), "messages": messages})
    )
    assert load_session(tmp_path, "legacy", profile) == messages
    with pytest.raises(ValueError, match="thinking"):
        load_session(tmp_path, "legacy", thinking)
    (tmp_path / "integer.json").write_text(
        json.dumps({"identity": profile.identity(), "messages": messages, "think": 1})
    )
    with pytest.raises(ValueError, match="thinking"):
        load_session(tmp_path, "integer", thinking)


def test_profile_does_not_inherit_host_proxy_settings(ollama_peer, monkeypatch):
    profile, _ = ollama_peer
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    assert profile.identity()["digest"] == "a" * 64


@pytest.mark.parametrize("operation", ["tags", "chat"])
def test_absolute_deadline_terminates_a_peer_that_keeps_trickling_without_newlines(
    ollama_peer, operation
):
    profile, peer = ollama_peer
    peer["trickle"] = operation
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="deadline"):
        if operation == "tags":
            profile.identity(timeout=0.5)
        else:
            profile.chat([], tools=[], timeout=0.5)
    assert time.monotonic() - started < 2


@pytest.mark.parametrize("budget", [60, 600])
def test_http_transport_preserves_the_callers_budget_at_the_real_connection(
    ollama_peer, monkeypatch, budget
):
    from ultimate_coders.metainfer_service import ollama

    profile, peer = ollama_peer
    peer["chunks"] = [{"model": profile.model, "done": True,
                       "message": {"content": "Actual provider bytes"}}]
    original_builder = ollama.build_opener
    observed = []

    def instrument_opener(*handlers):
        opener = original_builder(*handlers)
        original_open = opener.open

        def open_connection(request, *, timeout):
            observed.append(timeout)
            return original_open(request, timeout=timeout)

        opener.open = open_connection
        return opener

    monkeypatch.setattr(ollama, "build_opener", instrument_opener)
    result = ollama.http_exchange(profile, "/api/chat", {}, budget)
    assert result["message"]["content"] == "Actual provider bytes"
    assert observed == [budget]


@pytest.mark.parametrize("delay,budget,completed", [(0.1, 2, True), (2, 0.5, False)])
def test_real_header_delay_uses_the_absolute_operation_deadline(
    ollama_peer, delay, budget, completed
):
    profile, peer = ollama_peer
    peer["header_delay"] = delay
    peer["chunks"] = [{"model": profile.model, "done": True,
                       "message": {"content": "Actual delayed response"}}]
    started = time.monotonic()
    if completed:
        assert profile.chat([], tools=[], timeout=budget)["message"]["content"] == (
            "Actual delayed response"
        )
    else:
        with pytest.raises(TimeoutError, match="deadline"):
            profile.chat([], tools=[], timeout=budget)
        assert time.monotonic() - started < 2


@pytest.mark.parametrize(
    "url", ["http://user:secret@localhost", "http://localhost/api", "file:///tmp/model"]
)
def test_profile_refuses_credentials_and_non_provider_urls(url):
    with pytest.raises(ValueError):
        OllamaProfile(url, "coding:test", "a" * 64)


@pytest.mark.parametrize("context", [0, 512, 32769, True, "4096"])
def test_profile_refuses_unbounded_or_non_integer_context_before_http(context):
    with pytest.raises(ValueError, match="profile"):
        OllamaProfile("http://localhost", "coding:test", "a" * 64, num_ctx=context)


@pytest.mark.parametrize("budget", [0, 127, 8193, True, "8192"])
def test_profile_refuses_unbounded_or_non_integer_output_budget(budget):
    with pytest.raises(ValueError, match="profile"):
        OllamaProfile("http://localhost", "coding:test", "a" * 64, num_predict=budget)


@pytest.mark.skipif(os.name != "posix", reason="Native sessions require Linux no-follow files")
def test_native_session_persists_effective_context(tmp_path, ollama_peer):
    from ultimate_coders.metainfer_service.coding import save_session

    profile, _ = ollama_peer
    configured = OllamaProfile(profile.url, profile.model, profile.digest, num_ctx=4096)
    save_session(tmp_path, "context-test", configured, [{"role": "user", "content": "saved"}])
    assert json.loads((tmp_path / "context-test.json").read_text())["num_ctx"] == 4096
    assert json.loads((tmp_path / "context-test.json").read_text())["num_predict"] == 4096


@pytest.mark.skipif(os.name != "posix", reason="Native sessions require Linux no-follow files")
def test_native_session_resumes_same_context_and_refuses_changed_context(tmp_path, ollama_peer):
    from ultimate_coders.metainfer_service.coding import load_session, save_session

    profile, peer = ollama_peer
    configured = OllamaProfile(profile.url, profile.model, profile.digest, num_ctx=4096)
    messages = [{"role": "user", "content": "saved"}]
    save_session(tmp_path, "context-test", configured, messages)
    assert load_session(tmp_path, "context-test", configured) == messages
    changed = OllamaProfile(profile.url, profile.model, profile.digest, num_ctx=8192)
    with pytest.raises(ValueError, match="context"):
        load_session(tmp_path, "context-test", changed)
    changed_budget = OllamaProfile(
        profile.url, profile.model, profile.digest, num_ctx=4096, num_predict=8192
    )
    with pytest.raises(ValueError, match="output budget"):
        load_session(tmp_path, "context-test", changed_budget)
    assert peer["requests"] == []


@pytest.mark.skipif(os.name != "posix", reason="Native sessions require Linux no-follow files")
def test_legacy_native_session_resumes_only_with_legacy_context(tmp_path, ollama_peer):
    from ultimate_coders.metainfer_service.coding import load_session

    profile, _ = ollama_peer
    messages = [{"role": "user", "content": "legacy"}]
    (tmp_path / "legacy.json").write_text(
        json.dumps({"identity": profile.identity(), "messages": messages})
    )
    assert load_session(tmp_path, "legacy", profile) == messages
    changed = OllamaProfile(profile.url, profile.model, profile.digest, num_ctx=4096)
    with pytest.raises(ValueError, match="context"):
        load_session(tmp_path, "legacy", changed)


@pytest.mark.skipif(os.name != "posix", reason="Native artifacts require Linux no-follow files")
def test_native_file_tool_writes_real_bounded_artifact_and_preserves_permissions(tmp_path):
    import hashlib

    from ultimate_coders.metainfer_service.coding import write_file

    text = "Verdict: FAIL\nObserved reference execution failure.\n"
    first = write_file("reviews/result.md", text, tmp_path)
    target = tmp_path / "reviews/result.md"
    assert target.read_text() == text
    assert first["sha256"] == hashlib.sha256(text.encode()).hexdigest()
    assert first["kind"] == "add"
    target.chmod(0o640)
    updated = write_file(str(target), "Verdict: PASS\n", tmp_path)
    assert updated["kind"] == "update"
    assert target.stat().st_mode & 0o777 == 0o640


@pytest.mark.skipif(os.name != "posix", reason="Native artifacts require Linux no-follow files")
def test_native_file_tool_refuses_escape_symlinks_and_special_files(tmp_path):
    from ultimate_coders.metainfer_service.coding import write_file

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep.md"
    sentinel.write_text("unchanged")
    (workspace / "alias").symlink_to(outside, target_is_directory=True)
    (workspace / "linked.md").symlink_to(sentinel)
    os.mkfifo(workspace / "fifo")
    for path in ("../outside/keep.md", str(sentinel), "alias/keep.md", "linked.md", "fifo"):
        with pytest.raises((ValueError, OSError)):
            write_file(path, "changed", workspace)
    assert sentinel.read_text() == "unchanged"
    with pytest.raises(ValueError, match="bound"):
        write_file("oversized.md", "x" * 65537, workspace)
    assert not (workspace / "oversized.md").exists()


@pytest.mark.skipif(os.name != "posix", reason="Native artifacts require Linux no-follow files")
def test_native_file_tool_closes_directory_when_temporary_cleanup_fails(tmp_path, monkeypatch):
    from ultimate_coders.metainfer_service import coding

    opened = []
    original_open = os.open

    def record_open(*args, **kwargs):
        descriptor = original_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    def fail_unlink(*args, **kwargs):
        raise PermissionError("Observed cleanup refusal")

    monkeypatch.setattr(coding.os, "open", record_open)
    monkeypatch.setattr(coding.os, "unlink", fail_unlink)
    with pytest.raises(PermissionError, match="cleanup refusal"):
        coding.write_file("review.md", "Actual bytes\n", tmp_path)
    for descriptor in opened:
        with pytest.raises(OSError):
            os.fstat(descriptor)


@pytest.mark.skipif(os.name != "posix", reason="Native CLI sessions require Linux")
def test_native_cli_executes_streamed_file_tool_and_emits_distinct_items(
    tmp_path, monkeypatch, ollama_peer, capsys, native_cli_sessions
):
    from ultimate_coders.metainfer_service import coding

    profile, peer = ollama_peer
    monkeypatch.setattr(coding, "require_containment", lambda: None)
    monkeypatch.setenv("UC_OLLAMA_URL", profile.url)
    monkeypatch.setenv("UC_OLLAMA_MODEL", profile.model)
    monkeypatch.setenv("UC_OLLAMA_DIGEST", profile.digest)
    peer["responses"] = [
        [{"model": profile.model, "done": True, "message": {"tool_calls": [
            {"function": {"name": "write_file", "arguments": {
                "path": "review.md", "content": "Verdict: FAIL\n"}}}
        ]}}],
        [{"model": profile.model, "done": True, "message": {"content": "Created review.md"}}],
    ]
    assert coding.main(["exec", "-C", str(tmp_path), "Write review.md with write_file."]) == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    thread = next(item["thread_id"] for item in events if item["type"] == "thread.started")
    (native_cli_sessions / (thread + ".json")).unlink()
    assert (tmp_path / "review.md").read_text() == "Verdict: FAIL\n"
    items = [item["item"] for item in events if item["type"] == "item.completed"]
    assert [item["type"] for item in items] == ["file_change", "agent_message"]
    assert len({item["id"] for item in items}) == len(items)
    assert events[-1]["actual_tools_executed"] == 1
    assert peer["requests"][1]["messages"][-1]["tool_name"] == "write_file"


@pytest.mark.skipif(os.name != "posix", reason="Pinned artifacts require Linux no-follow files")
@pytest.mark.parametrize("phase,filename", [("CORRECTNESS", "correctness_harness.py"),
                                           ("PERFORMANCE", "perf_harness.py")])
@pytest.mark.parametrize("case", ["created", "missing", "empty", "link"])
def test_pinned_generator_requires_actual_artifact_and_bounds_model_feedback(
    tmp_path, monkeypatch, ollama_peer, capsys, native_cli_sessions, phase, filename, case
):
    from ultimate_coders.metainfer_service import coding

    profile, peer = ollama_peer
    monkeypatch.setattr(coding, "require_containment", lambda: None)
    monkeypatch.setenv("UC_OLLAMA_URL", profile.url)
    monkeypatch.setenv("UC_OLLAMA_MODEL", profile.model)
    monkeypatch.setenv("UC_OLLAMA_DIGEST", profile.digest)
    prose = {"model": profile.model, "done": True, "message": {"content": "Code in chat only."}}
    peer["responses"] = [[prose], [prose], [prose]]
    path = tmp_path / filename
    if case == "empty":
        path.write_bytes(b"")
    elif case == "link":
        target = tmp_path / "unchanged.txt"
        target.write_text("Original bytes")
        path.symlink_to(target)
    elif case == "created":
        peer["responses"][1] = [{"model": profile.model, "done": True,
            "message": {"tool_calls": [{"function": {"name": "write_file",
                "arguments": {"path": filename, "content": "# Actual model file\n"}}}]}}]
    prompt = (f"You are the **{phase} HARNESS GENERATOR** for GPU kernel optimization, "
              "iteration #1.\nWrite the requested file.")
    if case == "created":
        assert coding.main(["exec", "-C", str(tmp_path), prompt]) == 0
        assert path.read_text() == "# Actual model file\n"
    else:
        with pytest.raises(OSError if case == "link" else ValueError,
                           match=None if case == "link" else "did not produce"):
            coding.main(["exec", "-C", str(tmp_path), prompt])
        assert (not path.exists()) if case == "missing" else path.read_bytes() == (
            b"" if case == "empty" else b"Original bytes")
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert any(event["type"] == "turn.completed" for event in events) == (case == "created")
    assert len(peer["requests"]) == (1 if case == "link" else 3)
    if case != "link":
        feedback = peer["requests"][1]["messages"][-1]
        assert feedback["role"] == "user"
        assert filename in feedback["content"] and "write_file" in feedback["content"]
    files = [event for event in events if event.get("item", {}).get("type") == "file_change"]
    assert len(files) == int(case == "created")


@pytest.mark.skipif(os.name != "posix", reason="Native CLI sessions require Linux")
@pytest.mark.parametrize("tool", ["write_file", "exec_command"])
@pytest.mark.parametrize("verdict", ["malformed", "PASS", "FAIL"])
def test_native_review_completion_requires_unambiguous_pinned_format(
    tmp_path, monkeypatch, ollama_peer, capsys, native_cli_sessions, tool, verdict
):
    from ultimate_coders.metainfer_service import coding

    profile, peer = ollama_peer
    monkeypatch.setattr(coding, "require_containment", lambda: None)
    monkeypatch.setenv("UC_OLLAMA_URL", profile.url)
    monkeypatch.setenv("UC_OLLAMA_MODEL", profile.model)
    monkeypatch.setenv("UC_OLLAMA_DIGEST", profile.digest)
    filename = "correctness_review.md" if tool == "write_file" else "perf_review.md"
    content = "## Verdict: FAIL\nSome tests passed, but the harness is invalid.\n"
    if verdict != "malformed":
        content = f"**Verdict**: {verdict}\nObserved failures and passes are evidence.\n"
    arguments = {"path": filename, "content": content}
    if tool == "exec_command":
        script = f"from pathlib import Path; Path({filename!r}).write_text({content!r})"
        arguments = {"cmd": shlex.quote(os.sys.executable) + " -c " + shlex.quote(script)}
    peer["responses"] = [
        [{"model": profile.model, "done": True, "message": {"tool_calls": [
            {"function": {"name": tool, "arguments": arguments}}
        ]}}],
        [{"model": profile.model, "done": True, "message": {"content": "Review written."}}],
    ]
    if verdict == "malformed":
        with pytest.raises(ValueError, match="verdict"):
            coding.main(["exec", "-C", str(tmp_path), "Write " + filename])
    else:
        assert coding.main(["exec", "-C", str(tmp_path), "Write " + filename]) == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert (tmp_path / filename).read_text() == content
    assert any(event["type"] == "turn.completed" for event in events) == (verdict != "malformed")


@pytest.mark.parametrize(
    ("text", "valid"),
    [
        ("**Verdict**: PASS\nTests passed; no failures.\n", True),
        ("**Verdict**: FAIL\nSome cases passed.\n", True),
        ("\n- **Verdict**: FAIL\n", True),
        ("## Verdict: FAIL\nSome cases passed.\n", False),
        ("**Verdict**: PASS or FAIL\n", False),
        ("**Verdict**: PASS\n## Verdict: FAIL\n", False),
        ("**Verdict**: PASS\n- **Verdict**: PASS\n", False),
        ("# Review\n**Verdict**: PASS\n", False),
        ("```\n**Verdict**: PASS\n```\n", False),
        ("", False),
    ],
)
def test_native_review_format_preserves_explicit_verdict(text, valid):
    from ultimate_coders.metainfer_service.coding import validate_review

    if valid:
        validate_review(text)
    else:
        with pytest.raises(ValueError, match="verdict"):
            validate_review(text)


@pytest.mark.skipif(os.name != "posix", reason="Native generator review snapshots require Linux")
@pytest.mark.parametrize("phase", ["CORRECTNESS", "PERFORMANCE"])
@pytest.mark.parametrize("tool", ["write_file", "exec_command"])
@pytest.mark.parametrize("case", ["stale", "changed", "new", "reviewer", "generic"])
def test_native_generator_preserves_unchanged_reviews_without_approving_new_reviews(
    tmp_path, monkeypatch, ollama_peer, capsys, native_cli_sessions, phase, tool, case,
):
    from ultimate_coders.metainfer_service import coding

    profile, peer = ollama_peer
    monkeypatch.setattr(coding, "require_containment", lambda: None)
    monkeypatch.setenv("UC_OLLAMA_URL", profile.url)
    monkeypatch.setenv("UC_OLLAMA_MODEL", profile.model)
    monkeypatch.setenv("UC_OLLAMA_DIGEST", profile.digest)
    original = "# Historical Review\n## Verdict: FAIL\nOriginal rejected evidence.\n"
    (tmp_path / "correctness_review.md").write_text(original)
    if case != "new":
        (tmp_path / "perf_review.md").write_text(original)
    filename = "perf_review.md" if case in ("changed", "new") else (
        "correctness_harness.py" if phase == "CORRECTNESS" else "perf_harness.py")
    content = "# Newly written artifact\n## Verdict: FAIL\n"
    arguments = {"path": filename, "content": content}
    if tool == "exec_command":
        script = f"from pathlib import Path; Path({filename!r}).write_text({content!r})"
        arguments = {"cmd": shlex.quote(os.sys.executable) + " -c " + shlex.quote(script)}
    peer["responses"] = [
        [{"model": profile.model, "done": True, "message": {"tool_calls": [
            {"function": {"name": tool, "arguments": arguments}},
        ]}}],
        [{"model": profile.model, "done": True, "message": {"content": "Artifact written."}}],
    ]
    header = f"You are the **{phase} HARNESS GENERATOR** for GPU kernel optimization, iteration #1."
    if case == "reviewer":
        header = f"You are the **{phase} HARNESS REVIEWER** for iteration #1."
    elif case == "generic":
        header = "Generate a correctness harness."
    prompt = header + "\nWrite the requested artifact; preserve historical evidence."
    if case == "stale":
        assert coding.main(["exec", "-C", str(tmp_path), prompt]) == 0
        assert (tmp_path / "perf_review.md").read_text() == original
    else:
        with pytest.raises(ValueError, match="verdict"):
            coding.main(["exec", "-C", str(tmp_path), prompt])
    assert (tmp_path / "correctness_review.md").read_text() == original
    assert (tmp_path / filename).read_text() == content
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert any(event["type"] == "turn.completed" for event in events) == (case == "stale")


@pytest.mark.skipif(os.name != "posix", reason="Review files use Linux no-follow descriptors")
@pytest.mark.parametrize("kind", ["valid", "malformed", "symlink", "fifo", "large"])
def test_native_review_files_are_bounded_regular_and_close_descriptors(tmp_path, monkeypatch, kind):
    from ultimate_coders.metainfer_service import coding

    target = tmp_path / "correctness_review.md"
    content = "**Verdict**: FAIL\nSome tests passed.\n"
    if kind == "symlink":
        outside = tmp_path / "original.md"
        outside.write_text(content)
        target.symlink_to(outside)
    elif kind == "fifo":
        os.mkfifo(target)
    else:
        contents = content if kind == "valid" else "x" * 65537 if kind == "large" else "FAIL"
        target.write_text(contents)
    opened = []
    actual_open = coding.os.open

    def tracked_open(*args, **kwargs):
        descriptor = actual_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    monkeypatch.setattr(coding.os, "open", tracked_open)
    if kind == "valid":
        coding.verify_review_files(tmp_path)
        assert target.read_text() == content
    else:
        with pytest.raises((ValueError, OSError)):
            coding.verify_review_files(tmp_path)
    for descriptor in opened:
        with pytest.raises(OSError):
            os.fstat(descriptor)


@pytest.mark.skipif(os.name != "posix", reason="Native CLI sessions require Linux")
@pytest.mark.parametrize("tool", [False, True])
def test_native_cli_never_writes_or_completes_an_exhausted_response(
    tmp_path, monkeypatch, ollama_peer, capsys, tool, native_cli_sessions
):
    from ultimate_coders.metainfer_service import coding

    profile, peer = ollama_peer
    monkeypatch.setattr(coding, "require_containment", lambda: None)
    monkeypatch.setenv("UC_OLLAMA_URL", profile.url)
    monkeypatch.setenv("UC_OLLAMA_MODEL", profile.model)
    monkeypatch.setenv("UC_OLLAMA_DIGEST", profile.digest)
    message = {"content": "A nonempty but exhausted response."}
    if tool:
        message["tool_calls"] = [{"function": {"name": "write_file", "arguments": {
            "path": "review.md", "content": "Verdict: PASS\n"}}}]
    peer["responses"] = [
        [{"model": profile.model, "message": message, "done": True, "done_reason": "length"}],
        [{"model": profile.model, "message": {"content": "Completed."}, "done": True}],
    ]
    try:
        with pytest.raises(ValueError):
            coding.main(["exec", "-C", str(tmp_path), "Write review.md."])
    finally:
        events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        thread = next(item["thread_id"] for item in events if item["type"] == "thread.started")
        (native_cli_sessions / (thread + ".json")).unlink(missing_ok=True)
    assert not (tmp_path / "review.md").exists()
    assert not any(item["type"] in ("item.completed", "turn.completed") for item in events)


@pytest.fixture
def native_cli_sessions(tmp_path, monkeypatch):
    """Keep real session I/O in this test's directory, away from live OCI jobs."""
    from pathlib import Path

    from ultimate_coders.metainfer_service import coding

    sessions = tmp_path / "sessions"

    class IsolatedPath(type(Path())):
        def __new__(cls, *args):
            if args == ("/tmp/uc-ollama-sessions",):
                return sessions
            return super().__new__(cls, *args)

    monkeypatch.setattr(coding, "Path", IsolatedPath)
    return sessions


@pytest.mark.skipif(os.name != "posix", reason="Native client executes only inside Linux OCI")
@pytest.mark.asyncio
async def test_native_command_deadline_kills_its_writer_before_returning(tmp_path):
    import asyncio

    from ultimate_coders.metainfer_service.coding import execute_command

    result = await execute_command(
        "(sleep 1; echo escaped > late-write) & wait", tmp_path, timeout=0.1
    )
    assert result["exit_code"] == 124
    await asyncio.sleep(1.2)
    assert not (tmp_path / "late-write").exists()


@pytest.mark.skipif(os.name != "posix", reason="Native commands require Linux")
@pytest.mark.asyncio
async def test_expired_turn_cannot_start_the_next_tool_in_a_batch(tmp_path):
    import time

    from ultimate_coders.metainfer_service.coding import execute_command

    deadline = time.monotonic() + 0.1
    result = await execute_command("sleep 5", tmp_path, deadline=deadline)
    assert result["exit_code"] == 124
    with pytest.raises(TimeoutError, match="before tool launch"):
        await execute_command("echo late > never-started", tmp_path, deadline=deadline)
    assert not (tmp_path / "never-started").exists()


@pytest.fixture
def attested_fixture(tmp_path, monkeypatch, ollama_peer):
    """Emulated OCI/device boundaries, explicitly not GPU acceptance evidence."""
    if os.name != "posix":
        pytest.skip("Private Linux OCI evidence")
    from pathlib import Path

    from ultimate_coders.metainfer_service.app import ServiceConfig
    from ultimate_coders.metainfer_service.jobs import ContainedJobs
    from ultimate_coders.metainfer_service.ollama import native_client_identity

    from .test_metainfer_service import RuntimeFixture

    profile, peer = ollama_peer
    workspace = tmp_path / "workspaces/job"
    workspace.mkdir(parents=True)
    cgroup = tmp_path / "cgroup"
    cgroup.mkdir()
    (cgroup / "cgroup.procs").write_text("")
    binary = tmp_path / "bin"
    binary.mkdir()
    smi = binary / "nvidia-smi"
    smi.write_text("#!/bin/sh\nprintf 'GPU-fixture, Fixture GPU, 1, 100\\n'\n")
    smi.chmod(0o755)
    monkeypatch.setenv("PATH", str(binary) + ":" + os.environ["PATH"])

    class Runtime(RuntimeFixture):
        image_labels, gpu = {"uc.metainfer.ollama": "1"}, "wsl"
        corrupt_code = False

        def create(self, name, labels, mounts, command, environment):
            self.gate = Path(command[4])
            report = {
                "devices": [
                    {
                        "uuid": "GPU-fixture",
                        "model": "Fixture GPU",
                        "driver": "1",
                        "memory_mib": "100",
                    }
                ],
                "runtime": {"torch": "fixture", "image_code": native_client_identity()},
                "model_identity": {
                    "provider": "ollama",
                    "model": profile.model,
                    "digest": profile.digest,
                },
                "gpu_cases": [{"size": 33, "max_error": 0}],
                "model_probe": {"nonce": "fixture"},
            }
            if self.corrupt_code:
                report["runtime"]["image_code"]["coding.py"] = "f" * 64
            (mounts[1][0] / "hardware.json").write_text(json.dumps(report))
            return super().create(name, labels, mounts, command, environment)

        def inspect(self, container_id):
            if self.gate.exists():
                self.running = False
            return super().inspect(container_id)

    runtime = Runtime(cgroup)
    config = ServiceConfig(tmp_path / "control", (workspace.parent,), "backend-a", "revision-a")
    jobs = ContainedJobs(config, runtime, cgroup_root=tmp_path)
    environment = {
        "METAINFER_AGENT_BACKEND": "codex",
        "METAINFER_CODEX_BIN": "/opt/uc/ultimate_coders/metainfer_service/coding.py",
        "UC_OLLAMA_URL": profile.url,
        "UC_OLLAMA_MODEL": profile.model,
        "UC_OLLAMA_DIGEST": profile.digest,
    }
    return config, jobs, runtime, environment, peer


def test_hardware_http_and_restart_cache_recheck_live_model_without_new_probe(attested_fixture):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from ultimate_coders.metainfer_service.app import add_contract_routes
    from ultimate_coders.metainfer_service.jobs import ContainedJobs

    config, jobs, runtime, environment, peer = attested_fixture
    jobs.enable_hardware(environment)
    evidence = jobs.hardware()
    recovered = ContainedJobs(config, runtime, cgroup_root=jobs.cgroup_root)
    # Legacy ownership records carry no state; matching cache must not probe
    # while that writer remains owned.
    jobs.ownership.mutate("workspace_owners", "legacy-active", lambda _: {"task_id": "legacy"})
    recovered.enable_hardware(environment)
    assert recovered.hardware()["evidence_id"] == evidence["evidence_id"]
    assert runtime.calls == ["create", "start"]
    app = FastAPI()
    add_contract_routes(app, config, recovered)
    with TestClient(app) as client:
        assert client.get("/api/uc/hardware").json()["devices"][0]["uuid"] == "GPU-fixture"
        assert "hardware_identity" in client.get("/api/uc/contract").json()["capabilities"]
        peer["digest"] = "b" * 64
        assert client.get("/api/uc/hardware").status_code == 503
    with pytest.raises(ValueError, match="digest"):
        ContainedJobs(config, runtime, cgroup_root=jobs.cgroup_root).enable_hardware(environment)
    with pytest.raises(ValueError, match="digest"):
        recovered.launch(
            "new",
            config.workspace_roots[0] / "job",
            config.control_dir.parent / "new-state",
            config.control_dir.parent / "new-generated",
            ["python", "-c", "pass"],
            environment,
        )
    assert runtime.calls == ["create", "start"]


def test_re_attestation_refuses_legacy_ownership_without_a_state(attested_fixture):
    _, jobs, runtime, environment, _ = attested_fixture
    jobs.ownership.mutate("workspace_owners", "legacy", lambda _: {"task_id": "unresolved"})
    with pytest.raises(RuntimeError, match="Quiesce"):
        jobs.enable_hardware(environment)
    assert runtime.calls == []


def test_attested_context_changes_require_quiescent_ownership(attested_fixture):
    from ultimate_coders.metainfer_service.jobs import ContainedJobs

    config, jobs, runtime, environment, _ = attested_fixture
    configured = {**environment, "UC_OLLAMA_NUM_CTX": "4096"}
    jobs.enable_hardware(configured)
    assert jobs.hardware_attestation.profile.num_ctx == 4096
    jobs.ownership.mutate("workspace_owners", "active", lambda _: {"task_id": "live-job"})
    recovered = ContainedJobs(config, runtime, cgroup_root=jobs.cgroup_root)
    with pytest.raises(RuntimeError, match="Quiesce"):
        recovered.enable_hardware({**configured, "UC_OLLAMA_NUM_CTX": "8192"})
    assert runtime.calls == ["create", "start"]


@pytest.mark.parametrize(
    "key,value", [("UC_OLLAMA_NUM_CTX", "4096"), ("UC_OLLAMA_NUM_PREDICT", "8192"),
                  ("UC_OLLAMA_THINK", "true")]
)
def test_implicit_attested_context_cannot_be_overridden_before_launch(attested_fixture, key, value):
    config, jobs, runtime, environment, _ = attested_fixture
    jobs.enable_hardware(environment)
    state = config.control_dir.parent / "context-override-state"
    with pytest.raises(ValueError, match="attested"):
        jobs.launch(
            "context-override",
            config.workspace_roots[0] / "job",
            state,
            config.control_dir.parent / "context-override-generated",
            ["python", "-c", "pass"],
            {**environment, key: value},
        )
    assert runtime.calls == ["create", "start"]
    assert not state.exists()
    assert jobs.ownership.query("workspace_owners", task_id_present=True, limit=1) == []


def test_attested_thinking_profile_restarts_but_cannot_change_with_an_owned_writer(
    attested_fixture,
):
    from ultimate_coders.metainfer_service.jobs import ContainedJobs

    config, jobs, runtime, environment, _ = attested_fixture
    configured = {**environment, "UC_OLLAMA_THINK": "true"}
    jobs.enable_hardware(configured)
    assert jobs.hardware_attestation.profile.think is True
    evidence = jobs.hardware()
    jobs.ownership.mutate("workspace_owners", "active", lambda _: {"task_id": "live-job"})
    recovered = ContainedJobs(config, runtime, cgroup_root=jobs.cgroup_root)
    recovered.enable_hardware(configured)
    assert recovered.hardware()["evidence_id"] == evidence["evidence_id"]
    assert runtime.calls == ["create", "start"]
    with pytest.raises(RuntimeError, match="Quiesce"):
        recovered.enable_hardware(environment)
    assert runtime.calls == ["create", "start"]


def test_hardware_code_mismatch_keeps_endpoint_unavailable_after_verified_stop(attested_fixture):
    _, jobs, runtime, environment, _ = attested_fixture
    runtime.corrupt_code = True
    with pytest.raises(ValueError, match="matching GPU/model"):
        jobs.enable_hardware(environment)
    with pytest.raises(RuntimeError, match="not been configured"):
        jobs.hardware()
    assert runtime.calls == ["create", "start"]
    assert jobs.ownership.query("workspace_owners", task_id_present=True, limit=1) == []


@pytest.mark.parametrize("delay, expected_status", [(2.3, 200), (6, 503)])
def test_hardware_http_accepts_a_cold_query_but_bounds_stalled_identity(
    attested_fixture, delay, expected_status
):
    import shlex
    import sys

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from ultimate_coders.metainfer_service.app import add_contract_routes

    config, jobs, runtime, environment, _ = attested_fixture
    jobs.enable_hardware(environment)
    # This file is owned by attested_fixture inside its isolated temporary PATH.
    smi = config.control_dir.parent / "bin/nvidia-smi"
    assert smi.resolve().parent == config.control_dir.parent / "bin"
    code = f"import time; time.sleep({delay}); print('GPU-fixture, Fixture GPU, 1, 100')"
    smi.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} -c {shlex.quote(code)}\n")
    app = FastAPI()
    add_contract_routes(app, config, jobs)
    started = time.monotonic()
    with TestClient(app) as client:
        response = client.get("/api/uc/hardware")
    assert response.status_code == expected_status
    assert time.monotonic() - started < 6
    assert runtime.calls == ["create", "start"]
