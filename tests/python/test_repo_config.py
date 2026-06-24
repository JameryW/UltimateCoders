"""Tests for repo_config — RepoScanner, RepoConfig, RepoConfigWatcher."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from ultimate_coders.repo_config import (
    RepoConfig,
    RepoConfigWatcher,
    RepoEntry,
    RepoScanner,
    _parse_repos_yaml,
    load_repos_config,
)

# ── RepoEntry ──────────────────────────────────────────────────

class TestRepoEntry:
    def test_defaults(self):
        e = RepoEntry(repo_id="r1", local_path="/tmp/r1")
        assert e.remote_url == ""
        assert e.default_branch == "main"
        assert e.tags == []

    def test_with_tags(self):
        e = RepoEntry(repo_id="r1", local_path="/tmp/r1", tags=["core", "rust"])
        assert e.tags == ["core", "rust"]


# ── load_repos_config ──────────────────────────────────────────

class TestLoadReposConfig:
    def test_no_file_returns_defaults(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("UC_REPOS_CONFIG", raising=False)
        config = load_repos_config()
        assert config.repos == []
        assert config.scan_dirs == []
        assert config.scan_depth == 3

    def test_env_var_path(self, tmp_path, monkeypatch):
        yaml_file = tmp_path / "my_repos.yaml"
        yaml_file.write_text(
            "repos:\n"
            "  - repo_id: test-repo\n"
            "    local_path: /tmp/test\n"
            "scan_dirs:\n"
            "  - /tmp\n"
        )
        monkeypatch.setenv("UC_REPOS_CONFIG", str(yaml_file))
        config = load_repos_config()
        assert len(config.repos) == 1
        assert config.repos[0].repo_id == "test-repo"
        assert config.scan_dirs == ["/tmp"]

    def test_explicit_path(self, tmp_path, monkeypatch):
        yaml_file = tmp_path / "custom.yaml"
        yaml_file.write_text(
            "repos:\n"
            "  - repo_id: explicit\n"
            "    local_path: /explicit/path\n"
            "    tags: [a, b]\n"
            "scan_depth: 5\n"
        )
        monkeypatch.delenv("UC_REPOS_CONFIG", raising=False)
        config = load_repos_config(path=yaml_file)
        assert config.repos[0].tags == ["a", "b"]
        assert config.scan_depth == 5

    def test_cwd_auto_discover(self, tmp_path, monkeypatch):
        yaml_file = tmp_path / "uc.repos.yaml"
        yaml_file.write_text(
            "repos:\n"
            "  - repo_id: cwd-repo\n"
            "    local_path: /cwd/path\n"
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("UC_REPOS_CONFIG", raising=False)
        config = load_repos_config()
        assert config.repos[0].repo_id == "cwd-repo"

    def test_empty_required_fields_filtered(self, tmp_path, monkeypatch):
        yaml_file = tmp_path / "uc.repos.yaml"
        yaml_file.write_text(
            "repos:\n"
            "  - repo_id: ''\n"
            "    local_path: /some/path\n"
            "  - repo_id: valid\n"
            "    local_path: ''\n"
            "  - repo_id: good\n"
            "    local_path: /good/path\n"
        )
        monkeypatch.chdir(tmp_path)
        config = load_repos_config()
        # Only the entry with both repo_id and local_path survives
        assert len(config.repos) == 1
        assert config.repos[0].repo_id == "good"


# ── _parse_repos_yaml ──────────────────────────────────────────

class TestParseReposYaml:
    def test_full_config(self, tmp_path):
        yaml_file = tmp_path / "repos.yaml"
        yaml_file.write_text(
            "repos:\n"
            "  - repo_id: r1\n"
            "    local_path: /path/r1\n"
            "    remote_url: https://github.com/org/r1\n"
            "    default_branch: develop\n"
            "    tags: [backend]\n"
            "  - repo_id: r2\n"
            "    local_path: /path/r2\n"
            "scan_dirs:\n"
            "  - /projects\n"
            "  - /work\n"
            "scan_depth: 5\n"
        )
        config = _parse_repos_yaml(yaml_file)
        assert len(config.repos) == 2
        assert config.repos[0].default_branch == "develop"
        assert config.repos[0].tags == ["backend"]
        assert config.repos[1].tags == []
        assert config.scan_dirs == ["/projects", "/work"]
        assert config.scan_depth == 5

    def test_empty_file(self, tmp_path):
        yaml_file = tmp_path / "empty.yaml"
        yaml_file.write_text("")
        config = _parse_repos_yaml(yaml_file)
        assert config.repos == []
        assert config.scan_dirs == []


# ── RepoScanner ────────────────────────────────────────────────

class TestRepoScanner:
    def _make_git_repo(self, path: Path, remote_url: str = "") -> None:
        """Create a minimal git repo directory."""
        git_dir = path / ".git"
        git_dir.mkdir(parents=True)
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
        if remote_url:
            refs = git_dir / "refs" / "remotes" / "origin"
            refs.mkdir(parents=True)
            (refs / "HEAD").write_text("")
            config_file = git_dir / "config"
            config_file.write_text(
                f'[remote "origin"]\n'
                f"\turl = {remote_url}\n"
                "\tfetch = +refs/heads/*:refs/remotes/origin/*\n"
            )

    def test_discover_single_repo(self, tmp_path):
        repo_dir = tmp_path / "my-project"
        self._make_git_repo(repo_dir, remote_url="https://github.com/org/my-project.git")

        scanner = RepoScanner()
        results = scanner.discover([str(tmp_path)], scan_depth=3)
        assert len(results) == 1
        assert results[0].repo_id == "my-project"
        assert results[0].local_path == str(repo_dir)

    def test_discover_nested_repos(self, tmp_path):
        """Each git repo should be found but not recursed into."""
        r1 = tmp_path / "workspace" / "project-a"
        r2 = tmp_path / "workspace" / "project-b"
        self._make_git_repo(r1)
        self._make_git_repo(r2)

        scanner = RepoScanner()
        results = scanner.discover([str(tmp_path)], scan_depth=3)
        assert len(results) == 2
        repo_ids = {r.repo_id for r in results}
        assert "project-a" in repo_ids
        assert "project-b" in repo_ids

    def test_discover_respects_depth(self, tmp_path):
        deep = tmp_path / "a" / "b" / "c" / "d" / "deep-repo"
        self._make_git_repo(deep)

        scanner = RepoScanner()
        # depth=2: tmp_path/a/b/c — can't reach d/deep-repo
        results = scanner.discover([str(tmp_path)], scan_depth=2)
        assert len(results) == 0

        # depth=5: enough to reach it
        results = scanner.discover([str(tmp_path)], scan_depth=5)
        assert len(results) == 1

    def test_discover_skips_hidden_and_common_dirs(self, tmp_path):
        hidden = tmp_path / ".hidden-repo"
        node = tmp_path / "node_modules" / "some-pkg"
        venv = tmp_path / ".venv" / "lib"
        self._make_git_repo(hidden)
        self._make_git_repo(node)
        self._make_git_repo(venv)

        scanner = RepoScanner()
        results = scanner.discover([str(tmp_path)], scan_depth=3)
        assert len(results) == 0

    def test_discover_nonexistent_dir(self):
        scanner = RepoScanner()
        results = scanner.discover(["/nonexistent/path/12345"])
        assert results == []

    def test_derive_repo_id_from_remote(self, tmp_path):
        repo_dir = tmp_path / "my-dir"
        self._make_git_repo(repo_dir, remote_url="https://github.com/org/cool-project.git")

        # Mock git command to return remote URL
        with patch.object(RepoScanner, "_get_remote_url", return_value="https://github.com/org/cool-project.git"):
            scanner = RepoScanner()
            results = scanner.discover([str(tmp_path)], scan_depth=3)
            assert len(results) == 1
            assert results[0].repo_id == "cool-project"

    def test_discover_and_index_skips_existing(self, tmp_path):
        repo_dir = tmp_path / "existing-repo"
        self._make_git_repo(repo_dir)

        engine = MagicMock()
        scanner = RepoScanner(engine=engine)
        config = RepoConfig(scan_dirs=[str(tmp_path)])

        # Already indexed
        new = scanner.discover_and_index(config, indexed_repo_ids={"existing-repo"})
        assert len(new) == 0
        engine.index_repo.assert_not_called()

    def test_discover_and_index_calls_engine(self, tmp_path):
        repo_dir = tmp_path / "new-repo"
        self._make_git_repo(repo_dir)

        engine = MagicMock()
        scanner = RepoScanner(engine=engine)
        config = RepoConfig(scan_dirs=[str(tmp_path)])

        new = scanner.discover_and_index(config, indexed_repo_ids=set())
        assert len(new) == 1
        engine.index_repo.assert_called_once()


# ── RepoConfigWatcher ──────────────────────────────────────────

class TestRepoConfigWatcher:
    def test_reload_calls_callback(self, tmp_path):
        yaml_file = tmp_path / "repos.yaml"
        yaml_file.write_text(
            "repos:\n"
            "  - repo_id: watched\n"
            "    local_path: /watched/path\n"
        )

        callback = MagicMock()
        watcher = RepoConfigWatcher(yaml_file, on_change=callback)

        # Simulate a reload
        watcher._reload()
        callback.assert_called_once()
        config = callback.call_args[0][0]
        assert config.repos[0].repo_id == "watched"

    def test_reload_handles_bad_yaml(self, tmp_path):
        yaml_file = tmp_path / "repos.yaml"
        yaml_file.write_text("repos:\n  - repo_id: ok\n    local_path: /ok\n")

        callback = MagicMock()
        watcher = RepoConfigWatcher(yaml_file, on_change=callback)

        # Corrupt the file then reload
        yaml_file.write_text("{{invalid yaml")
        watcher._reload()
        # Callback should NOT be called on parse error
        callback.assert_not_called()

    def test_start_stop_without_watchdog(self, tmp_path):
        yaml_file = tmp_path / "repos.yaml"
        yaml_file.write_text("repos: []\n")

        callback = MagicMock()
        watcher = RepoConfigWatcher(yaml_file, on_change=callback)

        # This should not raise even if watchdog is not installed
        blocked = {
            "watchdog": None,
            "watchdog.observers": None,
            "watchdog.events": None,
        }
        with patch.dict("sys.modules", blocked):
            watcher.start()
            watcher.stop()
