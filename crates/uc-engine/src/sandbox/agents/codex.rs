//! Codex agent adapter.
//!
//! Wraps the OpenAI Codex CLI for sandbox execution.
//!
//! Command: `codex "{prompt}" --full-auto`
//! Output: stdout text + exit code
//! API key: `OPENAI_API_KEY` env var

use crate::sandbox::{
    AgentAdapter, ExecRequest, ExecResult, AgentOutput, SandboxConfig,
    truncate_str,
};
use uc_types::{ChangeType, FileChange};

/// Agent adapter for OpenAI Codex CLI.
pub struct CodexAgent;

impl CodexAgent {
    /// Create a new Codex adapter.
    pub fn new() -> Self {
        Self
    }
}

impl Default for CodexAgent {
    fn default() -> Self {
        Self::new()
    }
}

impl AgentAdapter for CodexAgent {
    fn name(&self) -> &str {
        "codex"
    }

    fn build_request(
        &self,
        prompt: &str,
        working_dir: &str,
        config: &SandboxConfig,
    ) -> ExecRequest {
        let mut env_vars = config.env_vars.clone();

        // Ensure OPENAI_API_KEY is available
        if !env_vars.contains_key("OPENAI_API_KEY") {
            if let Ok(key) = std::env::var("OPENAI_API_KEY") {
                env_vars.insert("OPENAI_API_KEY".to_string(), key);
            }
        }

        ExecRequest {
            command: "codex".to_string(),
            args: vec![
                prompt.to_string(),
                "--full-auto".to_string(),
            ],
            stdin: None,
            timeout_secs: config.resource_limits.max_cpu_seconds,
            working_dir: if working_dir.is_empty() {
                config.working_dir.clone()
            } else {
                working_dir.to_string()
            },
            env_vars,
        }
    }

    fn parse_output(&self, result: &ExecResult) -> AgentOutput {
        if result.timed_out {
            return AgentOutput {
                summary: "Codex execution timed out".to_string(),
                file_changes: vec![],
                token_usage: None,
                success: false,
            };
        }

        if result.exit_code != 0 {
            return AgentOutput {
                summary: format!(
                    "Codex exited with code {}: {}",
                    result.exit_code,
                    truncate_str(&result.stderr, 200)
                ),
                file_changes: vec![],
                token_usage: None,
                success: false,
            };
        }

        // Codex output is typically plain text.
        // Parse it for file references and summary.
        let output = result.stdout.trim();
        let (summary, file_changes) = parse_codex_output(output);

        AgentOutput {
            summary,
            file_changes,
            token_usage: None, // Codex CLI doesn't expose token usage in stdout
            success: true,
        }
    }
}

/// Parse Codex output text for summary and file changes.
///
/// Codex typically outputs a summary of what it did, sometimes
/// mentioning files it created or modified. We do a best-effort
/// extraction of file paths.
fn parse_codex_output(output: &str) -> (String, Vec<FileChange>) {
    let mut file_changes = Vec::new();
    let mut summary_lines = Vec::new();

    for line in output.lines() {
        let trimmed = line.trim();

        // Look for patterns like "Created file: path" or "Modified: path"
        if let Some(path) = extract_file_path_from_line(trimmed, "Created") {
            file_changes.push(FileChange {
                file_path: path,
                change_type: ChangeType::Created,
                diff: String::new(),
            });
        } else if let Some(path) = extract_file_path_from_line(trimmed, "Modified") {
            file_changes.push(FileChange {
                file_path: path,
                change_type: ChangeType::Modified,
                diff: String::new(),
            });
        } else if let Some(path) = extract_file_path_from_line(trimmed, "Deleted") {
            file_changes.push(FileChange {
                file_path: path,
                change_type: ChangeType::Deleted,
                diff: String::new(),
            });
        } else if !trimmed.is_empty() {
            summary_lines.push(trimmed.to_string());
        }
    }

    let summary = if summary_lines.is_empty() {
        "Codex completed execution".to_string()
    } else {
        // Take the first few non-empty lines as summary
        summary_lines.into_iter().take(5).collect::<Vec<_>>().join("\n")
    };

    (summary, file_changes)
}

/// Try to extract a file path from a line that starts with a keyword.
fn extract_file_path_from_line(line: &str, keyword: &str) -> Option<String> {
    let prefix = format!("{}:", keyword);
    if let Some(rest) = line.strip_prefix(&prefix) {
        let path = rest.trim();
        if !path.is_empty() && !path.contains(' ') {
            return Some(path.to_string());
        }
        // Also handle paths with spaces if quoted
        if path.starts_with('"') && path.ends_with('"') && path.len() > 1 {
            return Some(path[1..path.len()-1].to_string());
        }
    }

    // Also check "Created file:" pattern
    let prefix2 = format!("{} file:", keyword);
    if let Some(rest) = line.strip_prefix(&prefix2) {
        let path = rest.trim();
        if !path.is_empty() {
            return Some(path.to_string());
        }
    }

    None
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sandbox::{ResourceLimits, NetworkMode};
    use std::collections::HashMap;

    fn test_config() -> SandboxConfig {
        SandboxConfig {
            project_path: "/tmp/test".to_string(),
            env_vars: HashMap::new(),
            resource_limits: ResourceLimits::default(),
            network: NetworkMode::Restricted,
            working_dir: "/tmp/test".to_string(),
        }
    }

    #[test]
    fn codex_adapter_name() {
        let adapter = CodexAgent::new();
        assert_eq!(adapter.name(), "codex");
    }

    #[test]
    fn codex_build_request() {
        let adapter = CodexAgent::new();
        let config = test_config();
        let request = adapter.build_request("Implement feature X", "/tmp/test", &config);

        assert_eq!(request.command, "codex");
        assert!(request.args.contains(&"Implement feature X".to_string()));
        assert!(request.args.contains(&"--full-auto".to_string()));
        assert_eq!(request.working_dir, "/tmp/test");
    }

    #[test]
    fn codex_parse_output_success() {
        let adapter = CodexAgent::new();
        let result = ExecResult {
            exit_code: 0,
            stdout: "I implemented feature X by creating a new module.\nCreated: src/feature.rs".to_string(),
            stderr: String::new(),
            duration_ms: 10000,
            timed_out: false,
        };

        let output = adapter.parse_output(&result);
        assert!(output.success);
        assert!(output.summary.contains("implemented feature X"));
        assert_eq!(output.file_changes.len(), 1);
        assert_eq!(output.file_changes[0].file_path, "src/feature.rs");
        assert_eq!(output.file_changes[0].change_type, ChangeType::Created);
    }

    #[test]
    fn codex_parse_output_timeout() {
        let adapter = CodexAgent::new();
        let result = ExecResult {
            exit_code: -1,
            stdout: String::new(),
            stderr: "Timed out".to_string(),
            duration_ms: 30000,
            timed_out: true,
        };

        let output = adapter.parse_output(&result);
        assert!(!output.success);
        assert!(output.summary.contains("timed out"));
    }

    #[test]
    fn codex_parse_output_failure() {
        let adapter = CodexAgent::new();
        let result = ExecResult {
            exit_code: 1,
            stdout: String::new(),
            stderr: "API error".to_string(),
            duration_ms: 1000,
            timed_out: false,
        };

        let output = adapter.parse_output(&result);
        assert!(!output.success);
        assert!(output.summary.contains("exited with code 1"));
    }

    #[test]
    fn codex_parse_output_empty() {
        let adapter = CodexAgent::new();
        let result = ExecResult {
            exit_code: 0,
            stdout: String::new(),
            stderr: String::new(),
            duration_ms: 5000,
            timed_out: false,
        };

        let output = adapter.parse_output(&result);
        assert!(output.success);
        assert_eq!(output.summary, "Codex completed execution");
    }

    #[test]
    fn extract_file_path_created() {
        let result = extract_file_path_from_line("Created: src/main.rs", "Created");
        assert_eq!(result, Some("src/main.rs".to_string()));
    }

    #[test]
    fn extract_file_path_modified() {
        let result = extract_file_path_from_line("Modified: lib.rs", "Modified");
        assert_eq!(result, Some("lib.rs".to_string()));
    }

    #[test]
    fn extract_file_path_no_match() {
        let result = extract_file_path_from_line("Some other text", "Created");
        assert!(result.is_none());
    }

    #[test]
    fn extract_file_path_quoted() {
        let result = extract_file_path_from_line("Created: \"path with spaces.rs\"", "Created");
        assert_eq!(result, Some("path with spaces.rs".to_string()));
    }
}
