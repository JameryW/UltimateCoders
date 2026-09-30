"""UC subprocess boundary for the optional MetaInfer execution backend."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from ultimate_coders.agent.sandbox import AgentAdapter, AgentOutput, ExecResult, SandboxConfig
from ultimate_coders.agent.types import ChangeType, FileChange


def register(reg: Any) -> None:
    from ultimate_coders.agent.registry import AgentPluginSpec

    reg.register(
        AgentPluginSpec(
            name="metainfer",
            aliases=("inference-infra",),
            factory=MetaInferAgentAdapter,
            discoverable=False,
            description="Optional inference domain via UC_METAINFER_URL",
        )
    )


class MetaInferAgentAdapter(AgentAdapter):
    def name(self) -> str:
        return "metainfer"

    def build_request(
        self,
        prompt: str,
        working_dir: str,
        config: SandboxConfig,
        subtask_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        from ultimate_coders.inference.agent import InferenceInfraAgent

        settings = InferenceInfraAgent.route(prompt, subtask_config)
        if not settings or not settings.get("inference_task"):
            raise ValueError("MetaInfer requires explicit inference_task configuration")
        cancel_dir = tempfile.mkdtemp(prefix="uc-infra-cancel-")
        cancel_file = str(Path(cancel_dir) / "cancel")
        with tempfile.NamedTemporaryFile(
            "w",
            prefix="uc-infra-",
            suffix=".json",
            delete=False,
            encoding="utf-8",
        ) as request:
            json.dump(
                {
                    "config": settings,
                    "prompt": prompt,
                    "cwd": working_dir,
                    "timeout_seconds": max(1, config.max_cpu_seconds - 10),
                    "cancel_file": cancel_file,
                },
                request,
            )
        return {
            "command": sys.executable,
            "args": ["-m", "ultimate_coders.inference.runner", "--request", request.name],
            "timeout_secs": config.max_cpu_seconds,
            "working_dir": working_dir,
            "env_vars": {**config._build_env_vars(), "UC_INFERENCE_PROCESS_REGISTRY": cancel_dir},
            "_temp_files": [request.name, cancel_dir],
            "_cancel_file": cancel_file,
        }

    def parse_output(self, result: ExecResult) -> AgentOutput:
        if result.timed_out:
            return AgentOutput(success=False, summary="Inference execution timed out")
        for line in reversed(result.stdout.splitlines()):
            try:
                payload = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(payload, dict) and payload.get("event") == "final":
                success = result.exit_code == 0 and payload.get("success") is True
                domain = payload.get("result")
                changes = (
                    [
                        FileChange(
                            file_path=item["file_path"], change_type=ChangeType(item["change_type"])
                        )
                        for item in (domain or {}).get("file_changes", [])
                    ]
                    if isinstance(domain, dict)
                    else []
                )
                return AgentOutput(
                    success=success,
                    summary=str(payload.get("summary", "")),
                    domain_result=domain if isinstance(domain, dict) else None,
                    file_changes=changes if success else [],
                )
        return AgentOutput(success=False, summary="Inference runner returned no valid final result")
