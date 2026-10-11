"""Contract extension tests at the public HTTP and contained-launch interfaces."""

import json
import os
import subprocess
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from ultimate_coders.metainfer_service import jobs as jobs_module
from ultimate_coders.metainfer_service.app import ServiceConfig, add_contract_routes
from ultimate_coders.metainfer_service.jobs import ContainedJobs
from ultimate_coders.metainfer_service.observations import contain_observations, read_json
from ultimate_coders.metainfer_service.oci import LocalOCI
from ultimate_coders.metainfer_service.upstream import (
    UpstreamLauncher,
    create_service,
    restrict_mutations,
)


class RuntimeFixture:
    """OCI boundary fixture: mutable observations, never a synthetic stop proof."""

    image_id = "sha256:" + "a" * 64

    def __init__(self, cgroup):
        self.cgroup = cgroup
        self.calls = []
        self.containers = {}
        self.current = None

    @property
    def running(self):
        return self.containers[self.current]["running"]

    @running.setter
    def running(self, value):
        self.containers[self.current]["running"] = value

    def create(self, name, labels, mounts, command, environment):
        self.calls.append("create")
        self.current = ("b" if not self.containers else "c") * 64
        self.containers[self.current] = {"labels": labels, "running": False}
        return self.current

    def validate(self, mounts, command, environment):
        pass

    def start(self, container_id):
        self.calls.append("start")
        self.running = True

    def inspect(self, container_id):
        container = self.containers[container_id]
        return {
            "id": container_id,
            "labels": container["labels"],
            "running": container["running"],
            "pid": 123 if container["running"] else 0,
            "image": self.image_id,
            "restart_count": 0,
            "isolation": True,
        }

    def capture_scope(self, pid, container_id):
        return str(self.cgroup)

    def stop(self, container_id, force):
        self.calls.append("stop")
        self.containers[container_id]["running"] = False


def job_fixture(tmp_path):
    root = tmp_path / "worktrees"
    root.mkdir()
    workspace = root / "job"
    workspace.mkdir()
    cgroup = tmp_path / "cgroup"
    cgroup.mkdir()
    (cgroup / "cgroup.procs").write_text("")
    runtime = RuntimeFixture(cgroup)
    config = ServiceConfig(tmp_path / "control", (root,), "backend-a", "revision-a")
    jobs = ContainedJobs(config, runtime, boot_id="boot-a", cgroup_root=tmp_path)
    return config, jobs, runtime, workspace


def test_hardware_profile_refuses_an_unbound_coding_client_before_launch(tmp_path):
    config, jobs, runtime, _ = job_fixture(tmp_path)
    environment = {
        "METAINFER_AGENT_BACKEND": "codex",
        "METAINFER_CODEX_BIN": "/foreign/client",
        "UC_OLLAMA_URL": "http://localhost:11434",
        "UC_OLLAMA_MODEL": "coding:test",
        "UC_OLLAMA_DIGEST": "a" * 64,
    }
    with pytest.raises(ValueError, match="image|client"):
        jobs.enable_hardware(environment)
    assert runtime.calls == []
    app = FastAPI()
    add_contract_routes(app, config, jobs)
    with TestClient(app) as client:
        assert client.get("/api/uc/hardware").status_code == 503


def test_workspace_probe_identity_binds_stop_receipt_and_refuses_replay(tmp_path, monkeypatch):
    config, jobs, runtime, workspace = job_fixture(tmp_path)
    source, target = workspace / "read", workspace / "write"
    source.write_text("challenge")
    launch = jobs.launch

    def complete_fixture(task_id, root, state, generated, command, environment):
        launch(task_id, root, state, generated, command, environment)
        runtime.running = False
        target.write_text("nonce")
        (state / "orchestrator.log").write_text(
            json.dumps({"read_sha256": "fixture", "writer_identity": "fixture"})
        )

    monkeypatch.setattr(jobs, "launch", complete_fixture)
    identity = "probe-" + "a" * 32
    receipt = jobs.workspace_probe(workspace, source, target, "nonce", probe_id=identity)
    assert receipt["quiescence"]["task_id"] == identity
    assert receipt["quiescence"]["writers_stopped"] is True
    calls = list(runtime.calls)
    with pytest.raises(FileExistsError):
        jobs.workspace_probe(workspace, source, target, "nonce", probe_id=identity)
    assert runtime.calls == calls
    with pytest.raises(ValueError, match="probe identity"):
        jobs.workspace_probe(workspace, source, target, "nonce", probe_id="other-task")
    assert runtime.calls == calls
    app = FastAPI()
    add_contract_routes(app, config, jobs)
    with TestClient(app) as client:
        assert "workspace_probe_identity" in client.get("/api/uc/contract").json()["capabilities"]


def test_unknown_probe_fence_prevents_delayed_submission_but_not_an_active_claim(tmp_path):
    _, jobs, runtime, workspace = job_fixture(tmp_path)
    source = workspace / "read"
    source.write_text("nonce")
    identity = "probe-" + "a" * 32
    proof = jobs.quiescence(identity)
    assert proof["writers_stopped"] and proof["proof_kind"] == "workspace_fenced"
    with pytest.raises(FileExistsError):
        jobs.workspace_probe(workspace, source, workspace / "write", "nonce", probe_id=identity)
    assert runtime.calls == []
    active = "probe-" + "b" * 32
    (jobs.probe_claims / (active + ".json")).write_text(
        json.dumps({"task_id": active, "state": "launching"})
    )
    assert jobs.quiescence(active)["writers_stopped"] is False


def test_probe_failure_before_create_can_be_reconciled_without_replaying(tmp_path, monkeypatch):
    _, jobs, runtime, workspace = job_fixture(tmp_path)
    source = workspace / "read"
    source.write_text("nonce")

    def refuse(*args, **kwargs):
        raise RuntimeError("device observation failed before launch intent")

    monkeypatch.setattr(jobs, "launch", refuse)
    identity = "probe-" + "a" * 32
    with pytest.raises(RuntimeError, match="before OCI launch"):
        jobs.workspace_probe(workspace, source, workspace / "write", "nonce", probe_id=identity)
    assert jobs.quiescence(identity)["writers_stopped"] is True
    with pytest.raises(FileExistsError):
        jobs.workspace_probe(workspace, source, workspace / "write", "nonce", probe_id=identity)
    assert runtime.calls == []


@pytest.mark.skipif(sys.platform != "linux", reason="Linux directory fsync and /proc fd evidence")
def test_concurrent_fence_observer_persists_claim_before_publishing_proof(tmp_path, monkeypatch):
    _, jobs, _, _ = job_fixture(tmp_path)
    identity = "probe-" + "a" * 32
    claim = jobs.probe_claims / (identity + ".json")
    first_id = []
    first_at_sync, release_first = threading.Event(), threading.Event()
    second_file, second_directory = threading.Event(), threading.Event()
    sync, fsync = jobs_module.sync_directory, os.fsync

    def observe_sync(path):
        if path == jobs.probe_claims:
            if threading.get_ident() == first_id[0]:
                first_at_sync.set()
                assert release_first.wait(5)
            else:
                second_directory.set()
        sync(path)

    def observe_fsync(descriptor):
        if threading.get_ident() != first_id[0]:
            if Path(f"/proc/self/fd/{descriptor}").resolve() == claim:
                second_file.set()
        fsync(descriptor)

    def first():
        first_id.append(threading.get_ident())
        return jobs.quiescence(identity)

    monkeypatch.setattr(jobs_module, "sync_directory", observe_sync)
    monkeypatch.setattr(jobs_module.os, "fsync", observe_fsync)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(first)
        try:
            assert first_at_sync.wait(3)
            proof = jobs.quiescence(identity)
            assert proof["writers_stopped"] is True
            assert second_file.is_set() and second_directory.is_set()
        finally:
            release_first.set()
        assert pending.result(3)["writers_stopped"] is True


@pytest.mark.skipif(os.name != "posix", reason="Private Linux OCI metadata")
def test_new_hardware_probe_cannot_compete_with_an_unresolved_owned_job(tmp_path):
    _, jobs, runtime, workspace = job_fixture(tmp_path)
    runtime.image_labels, runtime.gpu = {"uc.metainfer.ollama": "1"}, "wsl"
    jobs.launch(
        "active",
        workspace,
        tmp_path / "state",
        tmp_path / "generated",
        ["python", "-c", "pass"],
        {},
    )
    environment = {
        "METAINFER_AGENT_BACKEND": "codex",
        "METAINFER_CODEX_BIN": "/opt/uc/ultimate_coders/metainfer_service/coding.py",
        "UC_OLLAMA_URL": "http://localhost:11434",
        "UC_OLLAMA_MODEL": "coding:test",
        "UC_OLLAMA_DIGEST": "a" * 64,
    }
    with pytest.raises(RuntimeError, match="Quiesce"):
        jobs.enable_hardware(environment)
    assert runtime.calls == ["create", "start"]
    assert jobs.quiescence("active")["writers_stopped"] is False


def test_quiescence_requires_stopped_container_and_all_descendants_and_survives_restart(tmp_path):
    config, jobs, runtime, workspace = job_fixture(tmp_path)
    state = tmp_path / "state"
    generated = tmp_path / "generated"
    jobs.launch("job-a", workspace, state, generated, ["python", "-c", "pass"], {})
    assert jobs.quiescence("job-a")["writers_stopped"] is False
    jobs.stop("job-a", force=True)
    child = runtime.cgroup / "child"
    child.mkdir()
    (child / "cgroup.procs").write_text("456\n")
    assert jobs.quiescence("job-a")["writers_stopped"] is False
    (child / "cgroup.procs").write_text("")
    recovered = ContainedJobs(config, runtime, boot_id="boot-a", cgroup_root=tmp_path)
    proof = recovered.quiescence("job-a")
    assert proof["writers_stopped"] is True
    assert proof["proof_kind"] == "cgroup_empty"
    assert (config.control_dir / "evidence" / (proof["evidence_id"] + ".json")).is_file()
    assert runtime.calls == ["create", "start", "stop"]
    with pytest.raises((OSError, RuntimeError)):
        recovered.quiescence("unknown")


def test_unknown_launch_outcome_is_not_replayed_or_declared_stopped(tmp_path):
    config, jobs, runtime, workspace = job_fixture(tmp_path)

    def interrupted(container_id):
        runtime.running = True
        raise RuntimeError("start response lost")

    runtime.start = interrupted
    with pytest.raises(RuntimeError, match="response lost"):
        jobs.launch(
            "job-a",
            workspace,
            tmp_path / "state",
            tmp_path / "generated",
            ["python", "-c", "pass"],
            {},
        )
    recovered = ContainedJobs(config, runtime, boot_id="boot-a", cgroup_root=tmp_path)
    with pytest.raises(RuntimeError, match="scope"):
        recovered.quiescence("job-a")
    with pytest.raises(FileExistsError):
        recovered.launch(
            "job-a",
            workspace,
            tmp_path / "state",
            tmp_path / "generated",
            ["python", "-c", "pass"],
            {},
        )
    record = json.loads(next((config.control_dir / "jobs").glob("*.json")).read_text())
    assert record["container_id"] == "b" * 64
    assert not (tmp_path / "state" / ".uc-launch-ready").exists()


def test_workspace_reuse_requires_proof_and_old_proof_cannot_release_new_owner(tmp_path):
    _, jobs, runtime, workspace = job_fixture(tmp_path)
    jobs.launch(
        "first", workspace, tmp_path / "state-first", tmp_path / "generated-first", ["true"], {}
    )
    with pytest.raises(FileExistsError):
        jobs.launch(
            "second",
            workspace,
            tmp_path / "state-second",
            tmp_path / "generated-second",
            ["true"],
            {},
        )
    jobs.stop("first", force=True)
    # A kill acknowledgement alone must not release the worktree.
    with pytest.raises(FileExistsError):
        jobs.launch(
            "second",
            workspace,
            tmp_path / "state-second",
            tmp_path / "generated-second",
            ["true"],
            {},
        )
    assert jobs.quiescence("first")["writers_stopped"] is True
    jobs.launch(
        "second", workspace, tmp_path / "state-second", tmp_path / "generated-second", ["true"], {}
    )
    assert jobs.quiescence("first")["writers_stopped"] is True
    with pytest.raises(FileExistsError):
        jobs.launch(
            "third", workspace, tmp_path / "state-third", tmp_path / "generated-third", ["true"], {}
        )


def test_cgroup_permission_error_never_becomes_a_missing_scope_proof(tmp_path, monkeypatch):
    config, jobs, runtime, workspace = job_fixture(tmp_path)
    jobs.launch("job-a", workspace, tmp_path / "state", tmp_path / "generated", ["true"], {})
    jobs.stop("job-a", force=True)
    original = Path.stat

    def inaccessible(path, *args, **kwargs):
        if path == runtime.cgroup:
            raise PermissionError("kernel scope unreadable")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", inaccessible)
    with pytest.raises(PermissionError):
        jobs.quiescence("job-a")
    assert list((config.control_dir / "evidence").glob("*.json")) == []


def test_control_overlap_is_rejected_before_creating_directories(tmp_path):
    config, jobs, runtime, workspace = job_fixture(tmp_path)
    state = config.control_dir / "illegal-task"
    with pytest.raises(ValueError, match="Control store"):
        jobs.launch("job-a", workspace, state, tmp_path / "generated", ["true"], {})
    assert not state.exists()
    assert runtime.calls == []


@pytest.mark.skipif(os.name != "posix", reason="Production metadata reads require Linux")
def test_failed_submission_remains_visible_and_dead_container_becomes_failed(tmp_path, monkeypatch):
    _, jobs, _, workspace = job_fixture(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setitem(
        sys.modules,
        "metainfer.server.launcher",
        SimpleNamespace(ProcStatus=lambda **kwargs: SimpleNamespace(**kwargs)),
    )
    monkeypatch.setitem(
        sys.modules,
        "metainfer.server",
        SimpleNamespace(paths=SimpleNamespace(task_dir=lambda _: state)),
    )
    launcher = UpstreamLauncher(jobs, {})
    assert launcher.status("never-started").exit_hint == "execution-unknown"
    with pytest.raises(OSError):
        jobs.quiescence("never-started")
    jobs.launch("job-a", workspace, state, tmp_path / "generated", ["true"], {})
    jobs.stop("job-a", force=True)
    assert launcher.status("job-a").running is False
    assert json.loads((state / "run.json").read_text())["final_status"] == "failed"


def test_workspace_probe_roundtrips_and_rejects_files_outside_configured_roots(tmp_path):
    root = tmp_path / "worktrees"
    root.mkdir()
    workspace = root / "job"
    workspace.mkdir()
    source = workspace / "read"
    source.write_bytes(b"abc")
    config = ServiceConfig(tmp_path / "control", (root,), "backend-a", "revision-a")
    app = FastAPI()
    add_contract_routes(app, config, None)
    with TestClient(app) as client:
        contract = client.get("/api/uc/contract").json()
        assert contract["backend_id"] == "backend-a"
        payload = {
            "workspace": str(workspace),
            "read_path": str(source),
            "write_path": str(workspace / "write"),
            "write_token": "challenge",
        }
        response = client.post("/api/uc/workspaces/verify", json=payload)
        assert response.status_code == 200
        assert response.json()["read_sha256"] == (
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        )
        assert Path(payload["write_path"]).read_text() == "challenge"
        payload["read_path"] = str(tmp_path / "private")
        assert client.post("/api/uc/workspaces/verify", json=payload).status_code == 422


@pytest.mark.skipif(os.name != "posix", reason="Linux symlink containment boundary")
def test_workspace_root_retargeting_cannot_authorize_protected_directories(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    allowed = workspace / "allowed"
    allowed.symlink_to(workspace, target_is_directory=True)
    protected = tmp_path / "task-state"
    protected.mkdir()
    config = ServiceConfig(tmp_path / "control", (allowed,), "backend-a", "revision-a")
    allowed.unlink()
    allowed.symlink_to(protected, target_is_directory=True)
    assert config.workspace(str(workspace)) == workspace
    with pytest.raises(ValueError, match="outside configured roots"):
        config.workspace(str(protected))


@pytest.mark.skipif(os.name != "posix", reason="Linux symlink containment boundary")
def test_control_store_alias_cannot_be_retargeted_by_a_workspace_writer(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    private = tmp_path / "private-control"
    private.mkdir()
    alias = workspace / "control-alias"
    alias.symlink_to(private, target_is_directory=True)
    config = ServiceConfig(alias, (workspace,), "backend-a", "revision-a")
    alias.unlink()
    alias.symlink_to(workspace, target_is_directory=True)
    cgroup = tmp_path / "cgroup"
    cgroup.mkdir()
    jobs = ContainedJobs(config, RuntimeFixture(cgroup), boot_id="boot-a", cgroup_root=tmp_path)
    assert config.control_dir == private
    assert (private / "identity.json").is_file()
    assert jobs.records.parent == private
    assert not (workspace / "identity.json").exists()


@pytest.mark.parametrize("change", ["backend", "container", "restart", "generation", "isolation"])
def test_foreign_or_restarted_container_cannot_authorize_cleanup(tmp_path, change):
    config, jobs, runtime, workspace = job_fixture(tmp_path)
    jobs.launch("job-a", workspace, tmp_path / "state", tmp_path / "generated", ["true"], {})
    runtime.running = False
    original = runtime.inspect

    def altered(identity):
        info = original(identity)
        if change == "backend":
            info["labels"] = {**info["labels"], "uc.backend": "foreign"}
        elif change == "container":
            info["id"] = "c" * 64
        elif change == "restart":
            info["restart_count"] = 1
        elif change == "generation":
            info["generation"] = "second-start"
        else:
            info["isolation"] = False
        return info

    runtime.inspect = altered
    if change == "generation":
        record_path = next((config.control_dir / "jobs").glob("*.json"))
        record = json.loads(record_path.read_text())
        record["generation"] = "first-start"
        record_path.write_text(json.dumps(record))
    with pytest.raises(RuntimeError, match="identity/isolation"):
        jobs.quiescence("job-a")
    assert list((config.control_dir / "evidence").glob("*.json")) == []


def test_uncontained_mutations_and_same_identity_restart_are_disabled():
    app = FastAPI()

    @app.post("/api/sys-shell/job-a/control")
    async def control():
        return {"ok": True}

    @app.post("/api/cluster/start")
    async def cluster():
        return {"ok": True}

    restrict_mutations(app)
    with TestClient(app) as client:
        assert (
            client.post("/api/sys-shell/job-a/control", json={"action": "kill"}).status_code == 200
        )
        for action in ("restart", "reset"):
            assert (
                client.post("/api/sys-shell/job-a/control", json={"action": action}).status_code
                == 409
            )
        assert client.post("/api/cluster/start").status_code == 409


def test_known_validation_failure_leaves_workspace_available(tmp_path):
    config, jobs, runtime, workspace = job_fixture(tmp_path)

    def reject(*args):
        raise ValueError("Invalid environment")

    runtime.validate = reject
    with pytest.raises(ValueError, match="environment"):
        jobs.launch("invalid", workspace, tmp_path / "state", tmp_path / "generated", ["true"], {})
    assert list((config.control_dir / "jobs").glob("*.json")) == []
    assert runtime.calls == []
    runtime.validate = lambda *args: None
    jobs.launch("valid", workspace, tmp_path / "state", tmp_path / "generated", ["true"], {})


def test_duplicate_identity_does_not_claim_a_different_workspace(tmp_path):
    _, jobs, runtime, workspace = job_fixture(tmp_path)
    jobs.launch("job-a", workspace, tmp_path / "state", tmp_path / "generated", ["true"], {})
    other = workspace.parent / "other"
    other.mkdir()
    with pytest.raises(FileExistsError):
        jobs.launch(
            "job-a", other, tmp_path / "other-state", tmp_path / "other-generated", ["true"], {}
        )
    assert runtime.calls == ["create", "start"]
    jobs.launch(
        "job-b", other, tmp_path / "other-state", tmp_path / "other-generated", ["true"], {}
    )
    assert jobs.quiescence("job-a")["writers_stopped"] is False


def test_short_probe_exit_race_releases_claim_only_after_stop_proof(tmp_path):
    config, jobs, runtime, workspace = job_fixture(tmp_path)
    source = workspace / "read"
    source.write_bytes(b"challenge")
    original = jobs.quiescence
    observations = []

    def disappeared(task_id):
        observations.append(task_id)
        if len(observations) == 1:
            raise FileNotFoundError("Process exited during kernel inspection")
        return original(task_id)

    jobs.quiescence = disappeared
    with pytest.raises(RuntimeError, match="probe-.*verified stop"):
        jobs.workspace_probe(workspace, source, workspace / "write", "nonce")
    assert len(observations) == 2
    assert len(list((config.control_dir / "evidence").glob("*.json"))) == 1
    jobs.launch(
        "next", workspace, tmp_path / "next-state", tmp_path / "next-generated", ["true"], {}
    )


@pytest.mark.parametrize("exited", [True, False])
def test_stop_error_requires_fresh_identity_checked_stopped_observation(tmp_path, exited):
    config, jobs, runtime, workspace = job_fixture(tmp_path)
    jobs.launch("job-a", workspace, tmp_path / "state", tmp_path / "generated", ["true"], {})

    def raced(container_id, force):
        runtime.running = not exited
        raise RuntimeError("Kill response failed")

    runtime.stop = raced
    if exited:
        assert jobs.stop("job-a", force=True) is True
        assert list((config.control_dir / "evidence").glob("*.json")) == []
        assert jobs.quiescence("job-a")["writers_stopped"] is True
    else:
        with pytest.raises(RuntimeError, match="response failed"):
            jobs.stop("job-a", force=True)
        assert jobs.quiescence("job-a")["writers_stopped"] is False


@pytest.mark.skipif(os.name != "posix", reason="Local Linux OCI boundary")
def test_oci_timeout_keeps_diagnostic_cause_without_environment_secrets(monkeypatch):
    partial = {"output": "private-partial-output", "stderr": "private-stderr"}
    credential = "PRIVATE_KEY=sentinel-secret"

    def cli(command, **kwargs):
        if command[1] == "info":
            return SimpleNamespace(returncode=0, stdout="false", stderr="")
        if command[1:3] == ["image", "inspect"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps([{"Id": "a" * 64}]), stderr="")
        raise subprocess.TimeoutExpired(command, 30, **partial)

    monkeypatch.setattr(subprocess, "run", cli)
    runtime = LocalOCI("podman", "job-image")
    with pytest.raises(RuntimeError, match="timed out") as caught:
        runtime.run("create", "--env", credential)
    trace = "".join(
        traceback.format_exception(type(caught.value), caught.value, caught.value.__traceback__)
    )
    assert isinstance(caught.value.__cause__, subprocess.TimeoutExpired)
    assert "sentinel-secret" not in trace
    assert "private-partial-output" not in trace
    assert "private-stderr" not in trace


@pytest.mark.skipif(os.name != "posix", reason="Local Linux OCI boundary")
def test_oci_finished_timestamp_matches_upstream_epoch_contract(monkeypatch):
    def cli(command, **kwargs):
        info = {
            "Id": "a" * 64,
            "Image": "a" * 64,
            "HostConfig": {},
            "State": {
                "Running": False,
                "Pid": 0,
                "FinishedAt": "2026-10-11T21:34:42.011371132+08:00",
            },
        }
        return SimpleNamespace(
            returncode=0, stdout="false" if command[1] == "info" else json.dumps([info]), stderr=""
        )

    monkeypatch.setattr(subprocess, "run", cli)
    assert LocalOCI("podman", "job-image").inspect("job")["finished_at"] == pytest.approx(
        1791725682.011371
    )


@pytest.mark.skipif(os.name != "posix", reason="No-follow directory descriptors require Linux")
def test_host_observation_rejects_symlinks_and_special_files(tmp_path, monkeypatch):
    private = tmp_path / "private.json"
    private.write_text('{"secret":"host-private-sentinel"}')
    state = tmp_path / "state"
    state.mkdir()
    (state / "requirements.json").symlink_to(private)
    with pytest.raises(OSError):
        read_json(state, "requirements.json", None)
    os.mkfifo(state / "run.json")
    with pytest.raises(ValueError, match="regular file"):
        read_json(state, "run.json", {})
    entry = SimpleNamespace(
        id="job-a", type="evolve-kernel", label="job", created_at=1, launcher="local"
    )
    monkeypatch.setitem(
        sys.modules,
        "metainfer.server",
        SimpleNamespace(
            paths=SimpleNamespace(task_dir=lambda _: state),
            tasks=SimpleNamespace(get_task=lambda _: entry, list_tasks=lambda: [entry]),
        ),
    )
    app = FastAPI()
    shell = APIRouter()

    @shell.get("/task-types")
    def discover():
        return [{"type": "evolve-kernel"}]

    @shell.post("/tasks")
    def create():
        return {"task_id": "job-a"}

    @shell.get("/{task_id}/agents/{name}/tail")
    def unsafe_tail(task_id: str, name: str):
        return {"secret": private.read_text()}

    launcher = SimpleNamespace(status=lambda _: SimpleNamespace(to_dict=lambda: {"running": False}))
    contain_observations(app, launcher, shell)
    with TestClient(app) as client:
        assert client.get("/api/sys-shell/task-types").status_code == 200
        assert client.post("/api/sys-shell/tasks").status_code == 200
        assert client.get("/api/sys-shell/tasks").status_code == 200
        response = client.get("/api/sys-shell/job-a")
        assert response.status_code == 503
        assert "host-private-sentinel" not in response.text
        assert client.get("/api/sys-shell/job-a/agents/a/tail").status_code == 404


@pytest.mark.parametrize("protected", ["source", "root", "control"])
def test_service_roots_cannot_be_exposed_to_job_writers(tmp_path, protected):
    source, root, control = (tmp_path / name for name in ("source", "root", "control"))
    source.mkdir()
    config = ServiceConfig(control, (tmp_path / protected,), "backend-a", "revision-a")
    with pytest.raises(ValueError, match="Workspace roots"):
        create_service(source, root, config, SimpleNamespace(), {})
