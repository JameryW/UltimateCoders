"""Inference domain selection; UC remains the global planner."""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from .models import InferenceTaskType

logger = logging.getLogger(__name__)

OPERATION_CAPABILITIES = {
    "port_model": "model_porting",
    "optimize_kernel": "kernel_optimization",
    "optimize_runtime": "runtime_optimization",
    "analyze_trace": "trace_analysis",
    "benchmark": "inference_benchmark",
}

_CONTEXT = re.compile(
    r"\b(vllm|sglang|tensorrt-llm|nccl|cuda|triton|pagedattention|flashattention|"
    r"flashmla|fp8|int4|int8|kv\s*cache|tpot|ttft)\b",
    re.IGNORECASE,
)
_INTENT = re.compile(
    r"\b(optimi[sz]e|port|trace|profile|benchmark|oom|throughput|latency|kernel)\b|"
    r"优化|移植|追踪|分析|性能|显存|吞吐|延迟",
    re.IGNORECASE,
)


class InferenceInfraAgent:
    @staticmethod
    def required_capabilities(config: dict[str, Any]) -> list[str]:
        operation = config["inference_task"]["task_type"]
        capability = OPERATION_CAPABILITIES[operation]
        return [capability] if operation == "benchmark" else ["inference_infra", capability]

    @staticmethod
    async def probe() -> list[str]:
        import asyncio

        import httpx

        from .adapter import MetaInferAdapter, task_types

        capabilities = ["inference_benchmark"]
        if not InferenceInfraAgent.configured():
            return capabilities
        try:
            adapter = MetaInferAdapter(os.environ["UC_METAINFER_URL"])
            configured = json.loads(os.environ.get("UC_INFERENCE_TASK_JSON") or "{}")
            types = task_types()
            task = configured.get("inference_task", {})
            if task.get("upstream_type") and task.get("task_type"):
                types[InferenceTaskType(task["task_type"])] = task["upstream_type"]
            async with httpx.AsyncClient(follow_redirects=False) as client:

                async def check(operation, upstream):
                    response = await client.get(
                        f"{adapter.url}/api/sys-shell/task-types/{upstream}/schema", timeout=3
                    )
                    response.raise_for_status()
                    schema = response.json()
                    if isinstance(schema, dict) and isinstance(schema.get("fields"), list):
                        return OPERATION_CAPABILITIES[operation.value]
                    return None

                results = await asyncio.gather(
                    *(check(operation, upstream) for operation, upstream in types.items()),
                    return_exceptions=True,
                )
            supported = [item for item in results if isinstance(item, str)]
            for operation, result in zip(types, results):
                if isinstance(result, Exception):
                    logger.warning(
                        "MetaInfer capability probe failed for %s (%s)",
                        operation.value,
                        type(result).__name__,
                    )
            if supported:
                capabilities += ["inference_infra", "metainfer", *supported]
        except (ValueError, KeyError, TypeError, httpx.HTTPError) as exc:
            logger.warning(
                "MetaInfer capability configuration unavailable (%s)", type(exc).__name__
            )
        return capabilities

    @staticmethod
    def requires_workspace(config: dict[str, Any] | None, steps: list | None = None) -> bool:
        configs = [config or {}]
        configs.extend(getattr(step, "agent_config", {}) or {} for step in (steps or []))
        return any(
            (item.get("inference_task") or {}).get("task_type")
            in ("port_model", "optimize_kernel", "optimize_runtime")
            for item in configs
        )

    capabilities = (
        "inference_infra",
        "metainfer",
        "model_porting",
        "kernel_optimization",
        "runtime_optimization",
        "trace_analysis",
        "benchmarking",
        "hardware_adaptation",
    )

    @staticmethod
    def configured() -> bool:
        return bool(os.environ.get("UC_METAINFER_URL", "").strip())

    @staticmethod
    def route(description: str, config: dict[str, Any] | None = None) -> dict[str, Any] | None:
        config = dict(config or {})
        # An explicit adapter choice is authoritative.
        if config.get("agent") and config["agent"] not in ("metainfer", "inference-infra"):
            return None
        explicit = config.get("inference_task")
        if explicit is not None:
            if not isinstance(explicit, dict):
                raise ValueError("inference_task must be an object")
            InferenceTaskType(explicit["task_type"])
            return {**config, "agent": "metainfer"}
        if not InferenceInfraAgent.configured():
            return None
        if not (_CONTEXT.search(description) and _INTENT.search(description)):
            return None
        defaults = json.loads(os.environ.get("UC_INFERENCE_TASK_JSON") or "{}")
        if not isinstance(defaults, dict):
            raise ValueError("UC_INFERENCE_TASK_JSON must be an object")
        desc = description.lower()
        if any(word in desc for word in ("trace", "profile", "追踪", "分析")):
            operation = InferenceTaskType.ANALYZE_TRACE
        elif any(word in desc for word in ("port", "移植")):
            operation = InferenceTaskType.PORT_MODEL
        elif any(word in desc for word in ("kernel", "triton", "内核")):
            operation = InferenceTaskType.OPTIMIZE_KERNEL
        elif "benchmark" in desc:
            operation = InferenceTaskType.BENCHMARK
        else:
            operation = InferenceTaskType.OPTIMIZE_RUNTIME
        task = {
            "task_type": operation.value,
            "objective": description,
            **defaults.get("inference_task", {}),
        }
        return {**defaults, **config, "agent": "metainfer", "inference_task": task}
