"""Local Linux OCI boundary; remote daemons cannot provide local kernel evidence."""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path


class LocalOCI:
    def __init__(self, executable: str, image: str, *, gpu: str = "none"):
        if os.name != "posix" or executable not in ("podman", "docker"):
            raise ValueError("Contained execution requires local Linux Podman or Docker")
        if any(os.environ.get(key) for key in ("DOCKER_HOST", "DOCKER_CONTEXT", "CONTAINER_HOST")):
            raise ValueError("Remote OCI daemons cannot prove local cgroup ownership")
        if gpu not in ("none", "wsl", "nvidia"):
            raise ValueError("Unsupported GPU mode")
        self.executable, self.gpu = executable, gpu
        self.rootless = False
        if executable == "podman":
            self.rootless = self.run("info", "--format", "{{.Host.Security.Rootless}}") == "true"
        else:
            context = json.loads(self.run("context", "inspect"))[0]
            if not context["Endpoints"]["docker"]["Host"].startswith("unix://"):
                raise ValueError("Docker context does not target a local Linux socket")
            security = self.run("info", "--format", "{{json .SecurityOptions}}")
            if "rootless" in security:
                raise ValueError("Rootless Docker UID mapping requires a separate verified adapter")
        info = json.loads(self.run("image", "inspect", image))[0]
        self.image_id = info["Id"]
        self.image_labels = info.get("Config", {}).get("Labels") or {}
        # Pin every create to the observed content identity, not a mutable tag.
        if not re.fullmatch(r"(?:sha256:)?[a-f0-9]{64}", self.image_id):
            raise ValueError("Runtime returned an invalid immutable image identity")

    def run(self, *arguments: str, timeout: int = 30) -> str:
        try:
            result = subprocess.run(
                [self.executable, *arguments],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            # Keep the cause while preventing timeout tracebacks from revealing
            # --env credentials or partial full-inspect JSON.
            exc.cmd, exc.output, exc.stderr = [self.executable, arguments[0]], None, None
            raise RuntimeError(f"OCI {arguments[0]} timed out after {timeout}s") from exc
        if result.returncode:
            raise RuntimeError(f"OCI {arguments[0]} failed: {result.stderr[-1500:]}")
        return result.stdout.strip()

    def _gpu_arguments(self) -> list[str]:
        if self.gpu == "wsl":
            return [
                "--device",
                "/dev/dxg",
                "--volume",
                "/usr/lib/wsl:/usr/lib/wsl:ro",
                "--env",
                "LD_LIBRARY_PATH=/usr/lib/wsl/lib",
                "--env",
                "PATH=/usr/lib/wsl/lib:/usr/local/bin:/usr/bin:/bin",
            ]
        return ["--gpus", "all"] if self.gpu == "nvidia" else []

    def create(
        self, name: str, labels: dict, mounts: list, command: list, environment: dict
    ) -> str:
        self.validate(mounts, command, environment)
        owner = mounts[0][0].stat()
        arguments = [
            "create",
            "--name",
            name,
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pid",
            "private",
            "--cgroupns",
            "private",
            "--restart",
            "no",
            "--user",
            f"{owner.st_uid}:{owner.st_gid}",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev",
            "--tmpfs",
            "/run:rw,nosuid,nodev",
            "--entrypoint",
            command[0],
            *self._gpu_arguments(),
        ]
        if self.rootless:
            arguments += ["--userns", "keep-id"]
        for key, value in labels.items():
            arguments += ["--label", f"{key}={value}"]
        for path, writable in mounts:
            value = str(path)
            arguments += ["--volume", value + ":" + value + (":rw" if writable else ":ro")]
        for key, value in environment.items():
            arguments += ["--env", f"{key}={value}"]
        identity = self.run(*arguments, self.image_id, *command[1:])
        if not re.fullmatch(r"[a-f0-9]{64}", identity):
            raise RuntimeError("OCI create returned no usable container identity")
        return identity

    def validate(self, mounts: list, command: list, environment: dict) -> None:
        if not mounts or not command or any(not isinstance(v, str) for v in command):
            raise ValueError("Job mounts and command are required")
        owner = mounts[0][0].stat()
        if self.rootless and (owner.st_uid, owner.st_gid) != (os.getuid(), os.getgid()):
            raise ValueError(
                "Rootless Podman requires the worktree owner to match the service UID/GID"
            )
        if any(":" in str(path) or "," in str(path) for path, _ in mounts):
            raise ValueError("Mount paths must not contain OCI option separators")
        if any(
            not isinstance(key, str)
            or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key)
            or not isinstance(value, str)
            or "\0" in value
            for key, value in environment.items()
        ):
            raise ValueError("Invalid job environment")

    def start(self, container_id: str) -> None:
        self.run("start", container_id)

    def inspect(self, container_id: str) -> dict:
        for _ in range(2):
            info = json.loads(self.run("inspect", container_id))[0]
            try:
                return self._inspection(info)
            except FileNotFoundError as exc:
                # Re-observe once when a process exits after OCI's snapshot.
                missing = exc
        raise RuntimeError("Container process state changed during inspection") from missing

    def _inspection(self, info: dict) -> dict:
        state, host = info["State"], info["HostConfig"]
        caps = {str(cap).upper().removeprefix("CAP_") for cap in host.get("CapDrop", [])}
        security = host.get("SecurityOpt", [])
        no_caps = "ALL" in caps and not host.get("CapAdd")
        if self.executable == "podman":
            # Podman serializes an empty capability slice as null in 4.9.
            no_caps = all(
                key in info and info[key] in (None, []) for key in ("EffectiveCaps", "BoundingCaps")
            )
        if state["Running"]:
            status = Path(f"/proc/{state['Pid']}/status").read_text()
            kernel_caps = [
                line.split()[1]
                for line in status.splitlines()
                if line.startswith(("CapEff:", "CapPrm:", "CapBnd:", "CapAmb:"))
            ]
            no_caps = (
                no_caps and len(kernel_caps) == 4 and all(int(v, 16) == 0 for v in kernel_caps)
            )
        cgroup_mode = host.get("CgroupnsMode", host.get("CgroupMode"))
        isolation = (
            host.get("ReadonlyRootfs") is True
            and host.get("Privileged") is False
            and host.get("PidMode") in ("private", "")
            and cgroup_mode == "private"
            and no_caps
            and any(str(value).startswith("no-new-privileges") for value in security)
            and host.get("RestartPolicy", {}).get("Name") in ("no", "")
        )
        finished = state.get("FinishedAt")
        finished_at = None
        if isinstance(finished, str) and finished and not finished.startswith("0001-"):
            finished = re.sub(r"(\.\d{6})\d+", r"\1", finished).replace("Z", "+00:00")
            finished_at = datetime.fromisoformat(finished).timestamp()
        return {
            "id": info["Id"],
            "image": info["Image"],
            "labels": info.get("Config", {}).get("Labels") or {},
            "running": state["Running"],
            "pid": state["Pid"],
            "restart_count": info.get("RestartCount", 0),
            "generation": state.get("StartedAt"),
            "finished_at": finished_at,
            "isolation": isolation,
        }

    def capture_scope(self, pid: int, container_id: str) -> str:
        candidates = []
        for line in Path(f"/proc/{pid}/cgroup").read_text().splitlines():
            _, controllers, relative = line.split(":", 2)
            if "pids" in controllers.split(","):
                candidates.append(Path("/sys/fs/cgroup/pids") / relative.lstrip("/"))
            elif not controllers:
                candidates.append(Path("/sys/fs/cgroup") / relative.lstrip("/"))
        for candidate in candidates:
            scope = candidate.resolve(strict=True)
            if (
                container_id in str(scope)
                and scope.joinpath("cgroup.procs").is_file()
                and str(pid) in scope.joinpath("cgroup.procs").read_text().split()
            ):
                return str(scope)
        raise RuntimeError("Cannot identify container-owned kernel cgroup")

    def stop(self, container_id: str, force: bool) -> None:
        if force:
            self.run("kill", "--signal", "KILL", container_id)
        else:
            self.run("stop", "--time", "2", container_id)
