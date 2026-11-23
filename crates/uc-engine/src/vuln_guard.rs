//! Vulnerability propagation guard and memory-file CWE audit (ledger item #85).
//!
//! Coding agents often copy an insecure pattern from the surrounding repository
//! context even when they can name the vulnerability. This module provides a
//! deterministic, LLM-independent gate: known vulnerable snippets are
//! registered, added patch lines are checked for imitation, and memory files
//! are audited against a compact set of CWE rules. The guard is deliberately
//! pure state and does not run an external scanner.

use serde::{Deserialize, Serialize};

pub const DEFAULT_MAX_SNIPPETS: usize = 256;
pub const DEFAULT_MAX_MEMORY_FILES: usize = 512;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum CweClass {
    CodeInjection,
    CommandInjection,
    SqlInjection,
    PathTraversal,
    InsecureDeserialization,
    HardcodedSecret,
    WeakCrypto,
    Xxe,
    LdapInjection,
    OpenRedirect,
    UnsafeReflection,
    MemoryCorruption,
}

#[derive(Debug, Clone, Copy)]
pub struct MemoryRule {
    pub id: &'static str,
    pub cwe: CweClass,
    pub needles: &'static [&'static str],
    pub description: &'static str,
}

pub const MEMORY_RULES: [MemoryRule; 12] = [
    MemoryRule {
        id: "MEM-001",
        cwe: CweClass::CodeInjection,
        needles: &["eval(", "exec(", "compile("],
        description: "dynamic code execution",
    },
    MemoryRule {
        id: "MEM-002",
        cwe: CweClass::CommandInjection,
        needles: &["shell=true", "os.system(", "subprocess.call(", "cmd /c"],
        description: "shell command execution",
    },
    MemoryRule {
        id: "MEM-003",
        cwe: CweClass::SqlInjection,
        needles: &["execute(f\"", "execute(\"select", "query + ", "where \" +"],
        description: "SQL string interpolation",
    },
    MemoryRule {
        id: "MEM-004",
        cwe: CweClass::PathTraversal,
        needles: &["../", "open(request.", "path.join(user"],
        description: "untrusted path traversal",
    },
    MemoryRule {
        id: "MEM-005",
        cwe: CweClass::InsecureDeserialization,
        needles: &["pickle.loads(", "yaml.load(", "marshal.loads("],
        description: "unsafe deserialization",
    },
    MemoryRule {
        id: "MEM-006",
        cwe: CweClass::HardcodedSecret,
        needles: &["password = \"", "api_key = \"", "secret = \""],
        description: "hardcoded credential",
    },
    MemoryRule {
        id: "MEM-007",
        cwe: CweClass::WeakCrypto,
        needles: &["md5(", "sha1(", "des("],
        description: "weak cryptography",
    },
    MemoryRule {
        id: "MEM-008",
        cwe: CweClass::Xxe,
        needles: &[
            "resolve_entities=true",
            "xmlreader",
            "documentbuilderfactory",
        ],
        description: "XML external entity",
    },
    MemoryRule {
        id: "MEM-009",
        cwe: CweClass::LdapInjection,
        needles: &["ldap.search(", "(&(uid="],
        description: "LDAP filter interpolation",
    },
    MemoryRule {
        id: "MEM-010",
        cwe: CweClass::OpenRedirect,
        needles: &["redirect(request.", "location = request."],
        description: "untrusted redirect target",
    },
    MemoryRule {
        id: "MEM-011",
        cwe: CweClass::UnsafeReflection,
        needles: &["getattr(", "setattr(", "method.invoke("],
        description: "unsafe reflection",
    },
    MemoryRule {
        id: "MEM-012",
        cwe: CweClass::MemoryCorruption,
        needles: &["strcpy(", "memcpy(dst, src", "sprintf(", "gets("],
        description: "unbounded memory operation",
    },
];

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct MemoryFile {
    pub path: String,
    pub content: String,
}

impl MemoryFile {
    pub fn new(path: impl Into<String>, content: impl Into<String>) -> Self {
        Self {
            path: path.into(),
            content: content.into(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct MemoryFinding {
    pub path: String,
    pub rule_id: String,
    pub cwe: CweClass,
    pub evidence: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct MemoryAuditReport {
    pub files_scanned: usize,
    pub findings: Vec<MemoryFinding>,
}

impl MemoryAuditReport {
    pub fn clean(&self) -> bool {
        self.findings.is_empty()
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct VulnerableSnippet {
    pub id: String,
    pub path: String,
    pub symbol: String,
    pub signature: String,
    pub cwe: CweClass,
}

impl VulnerableSnippet {
    pub fn new(
        id: impl Into<String>,
        path: impl Into<String>,
        symbol: impl Into<String>,
        signature: impl Into<String>,
        cwe: CweClass,
    ) -> Self {
        Self {
            id: id.into(),
            path: path.into(),
            symbol: symbol.into(),
            signature: signature.into(),
            cwe,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PatchHunk {
    pub path: String,
    pub symbol: String,
    pub added_lines: Vec<String>,
}

impl PatchHunk {
    pub fn new(
        path: impl Into<String>,
        symbol: impl Into<String>,
        added_lines: Vec<String>,
    ) -> Self {
        Self {
            path: path.into(),
            symbol: symbol.into(),
            added_lines,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum PatchFindingKind {
    VulnerableImitation,
    UnsafePattern,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PatchFinding {
    pub kind: PatchFindingKind,
    pub path: String,
    pub symbol: String,
    pub line: String,
    pub reference: String,
    pub cwe: CweClass,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PatchReview {
    pub blocked: bool,
    pub findings: Vec<PatchFinding>,
    pub snippets_considered: usize,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Disclosure {
    pub memory_files_scanned: usize,
    pub vulnerable_snippets: usize,
    pub finding_count: usize,
    pub message: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct VulnGuardError(pub String);

impl std::fmt::Display for VulnGuardError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for VulnGuardError {}

#[derive(Debug, Clone)]
pub struct VulnGuard {
    snippets: Vec<VulnerableSnippet>,
    max_snippets: usize,
}

impl Default for VulnGuard {
    fn default() -> Self {
        Self::new()
    }
}

impl VulnGuard {
    pub fn new() -> Self {
        Self::with_capacity(DEFAULT_MAX_SNIPPETS)
    }

    pub fn with_capacity(max_snippets: usize) -> Self {
        Self {
            snippets: Vec::new(),
            max_snippets,
        }
    }

    pub fn register_snippet(&mut self, snippet: VulnerableSnippet) -> Result<(), VulnGuardError> {
        if snippet.id.trim().is_empty() {
            return Err(VulnGuardError("snippet id must not be empty".into()));
        }
        if snippet.signature.trim().is_empty() {
            return Err(VulnGuardError("snippet signature must not be empty".into()));
        }
        if self.snippets.iter().any(|item| item.id == snippet.id) {
            return Err(VulnGuardError(format!("duplicate snippet: {}", snippet.id)));
        }
        if self.snippets.len() >= self.max_snippets {
            return Err(VulnGuardError("snippet capacity reached".into()));
        }
        self.snippets.push(snippet);
        Ok(())
    }

    pub fn snippets(&self) -> &[VulnerableSnippet] {
        &self.snippets
    }

    pub fn audit_memory(&self, files: &[MemoryFile]) -> MemoryAuditReport {
        let mut findings = Vec::new();
        for file in files.iter().take(DEFAULT_MAX_MEMORY_FILES) {
            let lower = file.content.to_lowercase();
            for rule in MEMORY_RULES {
                if let Some(needle) = rule.needles.iter().find(|needle| lower.contains(**needle)) {
                    findings.push(MemoryFinding {
                        path: file.path.clone(),
                        rule_id: rule.id.to_string(),
                        cwe: rule.cwe,
                        evidence: (*needle).to_string(),
                    });
                }
            }
        }
        MemoryAuditReport {
            files_scanned: files.len().min(DEFAULT_MAX_MEMORY_FILES),
            findings,
        }
    }

    pub fn review_patch(&self, hunks: &[PatchHunk]) -> PatchReview {
        let mut findings = Vec::new();
        for hunk in hunks {
            for line in &hunk.added_lines {
                let normalized = normalize(line);
                if normalized.is_empty() {
                    continue;
                }
                for snippet in &self.snippets {
                    if normalized.contains(&normalize(&snippet.signature)) {
                        findings.push(PatchFinding {
                            kind: PatchFindingKind::VulnerableImitation,
                            path: hunk.path.clone(),
                            symbol: hunk.symbol.clone(),
                            line: line.clone(),
                            reference: snippet.id.clone(),
                            cwe: snippet.cwe,
                        });
                    }
                }
                for rule in MEMORY_RULES {
                    if rule
                        .needles
                        .iter()
                        .any(|needle| normalized.contains(&normalize(needle)))
                    {
                        findings.push(PatchFinding {
                            kind: PatchFindingKind::UnsafePattern,
                            path: hunk.path.clone(),
                            symbol: hunk.symbol.clone(),
                            line: line.clone(),
                            reference: rule.id.to_string(),
                            cwe: rule.cwe,
                        });
                    }
                }
            }
        }
        PatchReview {
            blocked: !findings.is_empty(),
            findings,
            snippets_considered: self.snippets.len(),
        }
    }

    pub fn final_disclosure(&self, memory: &MemoryAuditReport, patch: &PatchReview) -> Disclosure {
        let finding_count = memory.findings.len() + patch.findings.len();
        let message = if finding_count == 0 {
            format!(
                "Security disclosure: {} memory files scanned, {} vulnerable snippets checked; no issues found.",
                memory.files_scanned,
                self.snippets.len()
            )
        } else {
            format!(
                "Security disclosure: {} memory files scanned, {} vulnerable snippets checked; {} finding(s) require review.",
                memory.files_scanned,
                self.snippets.len(),
                finding_count
            )
        };
        Disclosure {
            memory_files_scanned: memory.files_scanned,
            vulnerable_snippets: self.snippets.len(),
            finding_count,
            message,
        }
    }
}

fn normalize(value: &str) -> String {
    value
        .chars()
        .filter(|character| !character.is_whitespace())
        .flat_map(char::to_lowercase)
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn guard() -> VulnGuard {
        let mut guard = VulnGuard::new();
        guard
            .register_snippet(VulnerableSnippet::new(
                "snip-1",
                "src/old.c",
                "copy_name",
                "strcpy(dst, user_input)",
                CweClass::MemoryCorruption,
            ))
            .unwrap();
        guard
    }

    #[test]
    fn memory_audit_finds_known_cwes() {
        let report = guard().audit_memory(&[
            MemoryFile::new(".uc_memory/notes.md", "use eval(user_input)"),
            MemoryFile::new(".uc_memory/notes.md", "safe content"),
        ]);
        assert_eq!(report.files_scanned, 2);
        assert_eq!(report.findings.len(), 1);
        assert_eq!(report.findings[0].rule_id, "MEM-001");
        assert!(!report.clean());
    }

    #[test]
    fn clean_memory_is_clean() {
        let report = guard().audit_memory(&[MemoryFile::new("notes.md", "run the tests")]);
        assert!(report.clean());
    }

    #[test]
    fn imitation_of_registered_snippet_blocks_patch() {
        let review = guard().review_patch(&[PatchHunk::new(
            "src/new.c",
            "copy_name",
            vec!["strcpy(dst, user_input);".into()],
        )]);
        assert!(review.blocked);
        assert_eq!(
            review.findings[0].kind,
            PatchFindingKind::VulnerableImitation
        );
    }

    #[test]
    fn normalized_imitation_is_detected() {
        let review = guard().review_patch(&[PatchHunk::new(
            "src/new.c",
            "copy_name",
            vec!["STRCPY( dst,user_input )".into()],
        )]);
        assert!(review.blocked);
    }

    #[test]
    fn memory_rule_also_blocks_patch() {
        let review = guard().review_patch(&[PatchHunk::new(
            "app.py",
            "run",
            vec!["os.system(user_command)".into()],
        )]);
        assert!(review.blocked);
        assert_eq!(review.findings[0].cwe, CweClass::CommandInjection);
    }

    #[test]
    fn unrelated_patch_is_allowed() {
        let review = guard().review_patch(&[PatchHunk::new(
            "src/lib.rs",
            "add",
            vec!["let total = left + right;".into()],
        )]);
        assert!(!review.blocked);
        assert!(review.findings.is_empty());
    }

    #[test]
    fn empty_signature_is_rejected() {
        let mut guard = VulnGuard::new();
        assert!(guard
            .register_snippet(VulnerableSnippet::new(
                "x",
                "p",
                "s",
                " ",
                CweClass::CodeInjection
            ))
            .is_err());
    }

    #[test]
    fn duplicate_snippet_is_rejected() {
        let mut guard = guard();
        assert!(guard
            .register_snippet(VulnerableSnippet::new(
                "snip-1",
                "other.c",
                "x",
                "strcpy(",
                CweClass::MemoryCorruption,
            ))
            .is_err());
    }

    #[test]
    fn capacity_is_enforced() {
        let mut guard = VulnGuard::with_capacity(1);
        guard
            .register_snippet(VulnerableSnippet::new(
                "a",
                "p",
                "s",
                "eval(",
                CweClass::CodeInjection,
            ))
            .unwrap();
        assert!(guard
            .register_snippet(VulnerableSnippet::new(
                "b",
                "p",
                "s",
                "exec(",
                CweClass::CodeInjection
            ))
            .is_err());
    }

    #[test]
    fn disclosure_reports_counts() {
        let memory = guard().audit_memory(&[MemoryFile::new("notes.md", "eval(x)")]);
        let patch = guard().review_patch(&[]);
        let disclosure = guard().final_disclosure(&memory, &patch);
        assert_eq!(disclosure.memory_files_scanned, 1);
        assert_eq!(disclosure.vulnerable_snippets, 1);
        assert_eq!(disclosure.finding_count, 1);
        assert!(disclosure.message.contains("1 finding"));
    }

    #[test]
    fn disclosure_is_clean_when_nothing_found() {
        let memory = guard().audit_memory(&[]);
        let patch = guard().review_patch(&[]);
        let disclosure = guard().final_disclosure(&memory, &patch);
        assert_eq!(disclosure.finding_count, 0);
        assert!(disclosure.message.contains("no issues found"));
    }

    #[test]
    fn memory_audit_is_bounded() {
        let files = vec![MemoryFile::new("notes.md", "safe"); DEFAULT_MAX_MEMORY_FILES + 5];
        assert_eq!(
            guard().audit_memory(&files).files_scanned,
            DEFAULT_MAX_MEMORY_FILES
        );
    }
}
