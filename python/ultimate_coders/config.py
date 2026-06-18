"""Configuration loading for UltimateCoders.

Reads from TOML/YAML config files or environment variables.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


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


def _apply_dict_to_dataclass(obj: object, data: dict) -> None:
    """Apply a dict to a dataclass instance, matching field names."""
    for key, value in data.items():
        if not hasattr(obj, key):
            continue
        current = getattr(obj, key)
        if isinstance(value, dict) and hasattr(type(current), "__dataclass_fields__"):
            _apply_dict_to_dataclass(current, value)
        else:
            setattr(obj, key, value)


def load_config(path: str | None = None) -> Config:
    """Load configuration from file or environment.

    Priority: file > environment > defaults.
    """
    config = Config()

    # Load from TOML/YAML file if path is provided
    if path is not None:
        try:
            with open(path) as f:
                raw = f.read()

            if path.endswith(".toml"):
                try:
                    import tomllib
                except ImportError:
                    import tomli as tomllib  # type: ignore[no-redef]
                data = tomllib.loads(raw)
            elif path.endswith((".yaml", ".yml")):
                import yaml  # ponytail: soft dep, ImportError if missing
                data = yaml.safe_load(raw)
            else:
                logger.warning("Unknown config file format: %s (expected .toml/.yaml/.yml)", path)
                data = {}

            if isinstance(data, dict):
                _apply_dict_to_dataclass(config, data)
        except FileNotFoundError:
            logger.warning("Config file not found: %s", path)
        except ImportError as e:
            logger.warning("Config file parser not available: %s", e)
        except Exception as e:
            logger.warning("Failed to load config from %s: %s", path, e)

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

    return config
