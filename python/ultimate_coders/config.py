"""Configuration loading for UltimateCoders.

Reads from TOML/YAML config files or environment variables.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class EngineConfig:
    """Engine configuration."""
    mode: str = "local"  # "local" or "grpc"
    grpc_endpoint: str | None = None
    grpc_timeout_seconds: int = 30


@dataclass
class StorageConfig:
    """Storage configuration."""
    tikv_endpoints: list[str] = field(default_factory=lambda: ["127.0.0.1:2379"])
    qdrant_url: str = "http://127.0.0.1:6333"
    postgres_url: str = "postgresql://localhost:5432/ultimatecoders"


@dataclass
class NatsConfig:
    """NATS configuration."""
    url: str = "nats://127.0.0.1:4222"
    cluster_name: str = "ultimatecoders"


@dataclass
class LlmConfig:
    """LLM API configuration."""
    provider: str = "anthropic"
    api_key: str | None = None
    model: str = "claude-sonnet-4-6"
    fallback_model: str = "claude-haiku-4-5-20251001"
    max_retries: int = 5
    rpm_limit: int = 60
    tpm_limit: int = 100000


@dataclass
class Config:
    """Top-level configuration."""
    engine: EngineConfig = field(default_factory=EngineConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    nats: NatsConfig = field(default_factory=NatsConfig)
    llm: LlmConfig = field(default_factory=LlmConfig)


def load_config(path: str | None = None) -> Config:
    """Load configuration from file or environment.

    Priority: file > environment > defaults.
    """
    config = Config()

    # Override from environment
    config.llm.api_key = os.environ.get("ANTHROPIC_API_KEY", config.llm.api_key)
    config.engine.mode = os.environ.get("UC_ENGINE_MODE", config.engine.mode)
    config.engine.grpc_endpoint = os.environ.get(
        "UC_GRPC_ENDPOINT", config.engine.grpc_endpoint or ""
    ) or None
    config.storage.postgres_url = os.environ.get(
        "UC_POSTGRES_URL", config.storage.postgres_url
    )
    config.storage.qdrant_url = os.environ.get(
        "UC_QDRANT_URL", config.storage.qdrant_url
    )
    config.nats.url = os.environ.get("UC_NATS_URL", config.nats.url)

    # TODO: Load from TOML/YAML file if path is provided (PR5)

    return config
