"""Tests for configuration loading."""

from __future__ import annotations

import os
import tempfile

from ultimate_coders.config import load_config


class TestLoadConfigDefaults:
    """Tests for default configuration."""

    def test_default_config(self):
        config = load_config()
        assert config.engine.mode == "local"
        assert config.llm.provider == "anthropic"
        assert config.llm.model == "claude-sonnet-4-6"

    def test_env_overrides(self):
        os.environ["UC_ENGINE_MODE"] = "grpc"
        os.environ["UC_GRPC_ENDPOINT"] = "http://localhost:50051"
        try:
            config = load_config()
            assert config.engine.mode == "grpc"
            assert config.engine.grpc_endpoint == "http://localhost:50051"
        finally:
            del os.environ["UC_ENGINE_MODE"]
            del os.environ["UC_GRPC_ENDPOINT"]


class TestLoadConfigToml:
    """Tests for TOML config file loading."""

    def test_toml_file(self):
        content = """
[engine]
mode = "grpc"
grpc_endpoint = "http://remote:50051"

[llm]
provider = "openai"
model = "gpt-4o"
"""
        with tempfile.NamedTemporaryFile(suffix=".toml", mode="w", delete=False) as f:
            f.write(content)
            f.flush()
            config = load_config(f.name)
            assert config.engine.mode == "grpc"
            assert config.engine.grpc_endpoint == "http://remote:50051"
            assert config.llm.provider == "openai"
            assert config.llm.model == "gpt-4o"
            os.unlink(f.name)

    def test_missing_file_warns(self):
        config = load_config("/nonexistent/config.toml")
        # Should fall back to defaults
        assert config.engine.mode == "local"


class TestLoadConfigYaml:
    """Tests for YAML config file loading."""

    def test_yaml_file(self):
        content = """
engine:
  mode: grpc
  grpc_endpoint: "http://remote:50051"
llm:
  provider: gemini
  model: gemini-2.5-pro
"""
        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
            f.write(content)
            f.flush()
            config = load_config(f.name)
            assert config.engine.mode == "grpc"
            assert config.llm.provider == "gemini"
            assert config.llm.model == "gemini-2.5-pro"
            os.unlink(f.name)

    def test_file_priority_over_defaults(self):
        content = "[llm]\nprovider = 'openai'\nmodel = 'gpt-4'"
        with tempfile.NamedTemporaryFile(suffix=".toml", mode="w", delete=False) as f:
            f.write(content)
            f.flush()
            config = load_config(f.name)
            assert config.llm.provider == "openai"
            assert config.llm.model == "gpt-4"
            os.unlink(f.name)


class TestLoadConfigEnvVars:
    """Tests for environment variable overrides."""

    def test_llm_model_override(self):
        os.environ["UC_LLM_MODEL"] = "gpt-4o-mini"
        try:
            config = load_config()
            assert config.llm.model == "gpt-4o-mini"
        finally:
            del os.environ["UC_LLM_MODEL"]

    def test_llm_rpm_limit_override(self):
        os.environ["UC_LLM_RPM_LIMIT"] = "120"
        try:
            config = load_config()
            assert config.llm.rpm_limit == 120
        finally:
            del os.environ["UC_LLM_RPM_LIMIT"]

    def test_tikv_endpoints_override(self):
        os.environ["UC_TIKV_ENDPOINTS"] = "10.0.0.1:2379,10.0.0.2:2379"
        try:
            config = load_config()
            assert config.storage.tikv_endpoints == ["10.0.0.1:2379", "10.0.0.2:2379"]
        finally:
            del os.environ["UC_TIKV_ENDPOINTS"]


class TestLoadConfigAutoDiscovery:
    """Tests for config file auto-discovery."""

    def test_discovers_uc_toml(self, tmp_path, monkeypatch):
        """Should find uc.toml in current directory."""
        config_file = tmp_path / "uc.toml"
        config_file.write_text('[llm]\nprovider = "openai"\n')
        monkeypatch.chdir(tmp_path)
        config = load_config()
        assert config.llm.provider == "openai"

    def test_no_discovery_when_explicit_path(self, tmp_path, monkeypatch):
        """Explicit path should skip auto-discovery."""
        config_file = tmp_path / "uc.toml"
        config_file.write_text('[llm]\nprovider = "openai"\n')
        other = tmp_path / "other.toml"
        other.write_text('[llm]\nprovider = "gemini"\n')
        monkeypatch.chdir(tmp_path)
        config = load_config(str(other))
        assert config.llm.provider == "gemini"
