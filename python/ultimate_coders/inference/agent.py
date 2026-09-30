"""Inference domain selection; UC remains the global planner."""

from __future__ import annotations

import json
import os
import re
from typing import Any

from .models import InferenceTaskType

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
