//! ChainSWE-style chain isolation (#67): sanitise each defect patch against the
//! minimum AST change boundary of the bug it is meant to fix.
//!
//! A multi-bug chain applies patches cumulatively:
//! `R_k = Apply(R_{k-1}, delta_k)`. An overshoot edit in `delta_k` contaminates
//! every later step, while an undershoot edit leaves the bug unfixed. This
//! module is pure bookkeeping: callers supply the already-parsed edits and the
//! allowed scope, and receive the kept/stripped split plus a deterministic
//! survival estimate. It never parses source, runs git, or calls a model.

use serde::{Deserialize, Serialize};
use thiserror::Error;

/// Default exponent used to penalise out-of-scope changed lines.
pub const DEFAULT_OVERSHOOT_LAMBDA: f64 = 1.0;

/// The minimum change boundary for one bug in a chain.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct BugScope {
    /// Bug identifier this scope belongs to.
    pub bug_id: String,
    /// Paths the fix is allowed to touch.
    pub paths: Vec<String>,
    /// Symbols the fix is allowed to touch.
    pub symbols: Vec<String>,
    /// Symbols that must receive a substantive edit.
    pub required_symbols: Vec<String>,
}

impl BugScope {
    /// Build a scope. Empty identifiers and empty path/symbol sets are rejected
    /// by [`ChainSanitizer::sanitize`].
    #[must_use]
    pub fn new(
        bug_id: impl Into<String>,
        paths: Vec<String>,
        symbols: Vec<String>,
        required_symbols: Vec<String>,
    ) -> Self {
        Self {
            bug_id: bug_id.into(),
            paths,
            symbols,
            required_symbols,
        }
    }

    fn allows(&self, edit: &AstEdit) -> bool {
        self.paths.iter().any(|path| path == &edit.path)
            || self.symbols.iter().any(|symbol| symbol == &edit.symbol)
    }
}

/// One AST-anchored edit inside a patch.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AstEdit {
    /// Repository-relative path.
    pub path: String,
    /// Symbol whose body changed; may be empty for file-level edits.
    pub symbol: String,
    /// Added source lines.
    pub added_lines: u32,
    /// Removed source lines.
    pub removed_lines: u32,
}

impl AstEdit {
    /// Build an edit.
    #[must_use]
    pub fn new(
        path: impl Into<String>,
        symbol: impl Into<String>,
        added_lines: u32,
        removed_lines: u32,
    ) -> Self {
        Self {
            path: path.into(),
            symbol: symbol.into(),
            added_lines,
            removed_lines,
        }
    }

    /// Number of changed source lines.
    #[must_use]
    pub fn changed_lines(&self) -> u32 {
        self.added_lines.saturating_add(self.removed_lines)
    }
}

/// A candidate patch for one bug in the chain.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ChainPatch {
    /// Bug this patch claims to fix.
    pub bug_id: String,
    /// Parsed edits.
    pub edits: Vec<AstEdit>,
}

impl ChainPatch {
    /// Build a patch.
    #[must_use]
    pub fn new(bug_id: impl Into<String>, edits: Vec<AstEdit>) -> Self {
        Self {
            bug_id: bug_id.into(),
            edits,
        }
    }
}

/// Result of applying the scope filter to one patch.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SanitizedPatch {
    /// Bug identifier.
    pub bug_id: String,
    /// Edits inside the minimum change boundary.
    pub kept: Vec<AstEdit>,
    /// Edits stripped as overshoot.
    pub stripped: Vec<AstEdit>,
    /// Changed lines removed from the patch.
    pub overshoot_lines: u32,
    /// Fraction of changed lines that were stripped.
    pub overshoot_ratio: f64,
    /// Required symbols that received no kept edit.
    pub required_missing: Vec<String>,
    /// True when at least one required symbol is untouched.
    pub undershoot: bool,
    /// `exp(-lambda * overshoot_lines)` for the sanitised patch.
    pub survival: f64,
}

impl SanitizedPatch {
    /// A patch is scope-clean when nothing was stripped and nothing required is
    /// missing.
    #[must_use]
    pub fn clean(&self) -> bool {
        self.stripped.is_empty() && !self.undershoot
    }

    /// Kept changed lines.
    #[must_use]
    pub fn kept_lines(&self) -> u32 {
        self.kept.iter().map(AstEdit::changed_lines).sum()
    }
}

/// Scope filter for chained patches.
#[derive(Debug, Clone, PartialEq)]
pub struct ChainSanitizer {
    lambda: f64,
}

impl Default for ChainSanitizer {
    fn default() -> Self {
        Self::new()
    }
}

impl ChainSanitizer {
    /// Create a sanitizer with the default overshoot penalty.
    #[must_use]
    pub fn new() -> Self {
        Self {
            lambda: DEFAULT_OVERSHOOT_LAMBDA,
        }
    }

    /// Create a sanitizer with an explicit non-negative penalty.
    pub fn with_lambda(lambda: f64) -> Result<Self, ChainError> {
        if !lambda.is_finite() || lambda < 0.0 {
            return Err(ChainError::InvalidLambda(lambda));
        }
        Ok(Self { lambda })
    }

    /// Penalty exponent.
    #[must_use]
    pub fn lambda(&self) -> f64 {
        self.lambda
    }

    /// Strip overshoot edits and detect undershoot against `scope`.
    pub fn sanitize(
        &self,
        patch: &ChainPatch,
        scope: &BugScope,
    ) -> Result<SanitizedPatch, ChainError> {
        if patch.bug_id.trim().is_empty() || scope.bug_id.trim().is_empty() {
            return Err(ChainError::EmptyField("bug_id"));
        }
        if patch.bug_id != scope.bug_id {
            return Err(ChainError::ScopeMismatch {
                patch: patch.bug_id.clone(),
                scope: scope.bug_id.clone(),
            });
        }
        if scope.paths.is_empty() && scope.symbols.is_empty() {
            return Err(ChainError::EmptyField("scope"));
        }
        if patch.edits.is_empty() {
            return Err(ChainError::EmptyPatch);
        }
        for edit in &patch.edits {
            if edit.path.trim().is_empty() {
                return Err(ChainError::EmptyField("edit.path"));
            }
        }

        let mut kept = Vec::new();
        let mut stripped = Vec::new();
        for edit in &patch.edits {
            if scope.allows(edit) {
                kept.push(edit.clone());
            } else {
                stripped.push(edit.clone());
            }
        }

        let total_lines: u32 = patch.edits.iter().map(AstEdit::changed_lines).sum();
        let overshoot_lines: u32 = stripped.iter().map(AstEdit::changed_lines).sum();
        let overshoot_ratio = if total_lines == 0 {
            0.0
        } else {
            f64::from(overshoot_lines) / f64::from(total_lines)
        };

        let kept_symbols: Vec<&str> = kept
            .iter()
            .map(|edit| edit.symbol.as_str())
            .filter(|symbol| !symbol.is_empty())
            .collect();
        let required_missing: Vec<String> = scope
            .required_symbols
            .iter()
            .filter(|required| !kept_symbols.iter().any(|symbol| symbol == required))
            .cloned()
            .collect();
        let undershoot = !required_missing.is_empty();
        let survival = (-self.lambda * f64::from(overshoot_lines)).exp();

        Ok(SanitizedPatch {
            bug_id: patch.bug_id.clone(),
            kept,
            stripped,
            overshoot_lines,
            overshoot_ratio,
            required_missing,
            undershoot,
            survival,
        })
    }

    /// Chain survival for a sequence of step pass rates and an overshoot line
    /// count: `prod(p_i) * exp(-lambda * overshoot_lines)`.
    pub fn chain_survival(
        &self,
        step_pass_rates: &[f64],
        overshoot_lines: u32,
    ) -> Result<f64, ChainError> {
        if step_pass_rates.is_empty() {
            return Err(ChainError::EmptyStepRates);
        }
        let mut product = 1.0_f64;
        for rate in step_pass_rates {
            if !rate.is_finite() || !(0.0..=1.0).contains(rate) {
                return Err(ChainError::InvalidPassRate(*rate));
            }
            product *= *rate;
        }
        Ok(product * (-self.lambda * f64::from(overshoot_lines)).exp())
    }
}

/// Errors returned by the chain sanitiser.
#[derive(Debug, Error, PartialEq)]
pub enum ChainError {
    /// A required string was empty.
    #[error("field `{0}` must not be empty")]
    EmptyField(&'static str),
    /// The patch carried no edits.
    #[error("patch must contain at least one edit")]
    EmptyPatch,
    /// Patch and scope name different bugs.
    #[error("patch bug `{patch}` does not match scope bug `{scope}`")]
    ScopeMismatch {
        /// Patch bug id.
        patch: String,
        /// Scope bug id.
        scope: String,
    },
    /// Lambda was negative or non-finite.
    #[error("lambda must be finite and non-negative, got {0}")]
    InvalidLambda(f64),
    /// A pass rate fell outside `[0, 1]`.
    #[error("pass rate must be within [0, 1], got {0}")]
    InvalidPassRate(f64),
    /// Survival was requested without step rates.
    #[error("at least one step pass rate is required")]
    EmptyStepRates,
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scope() -> BugScope {
        BugScope::new(
            "bug-1",
            vec!["src/cache.py".into()],
            vec!["Cache.get".into(), "Cache.set".into()],
            vec!["Cache.get".into()],
        )
    }

    fn patch(edits: Vec<AstEdit>) -> ChainPatch {
        ChainPatch::new("bug-1", edits)
    }

    #[test]
    fn in_scope_edits_are_kept() {
        let result = ChainSanitizer::new()
            .sanitize(
                &patch(vec![AstEdit::new("src/cache.py", "Cache.get", 3, 1)]),
                &scope(),
            )
            .expect("sanitize");
        assert_eq!(result.kept.len(), 1);
        assert!(result.stripped.is_empty());
        assert!(!result.undershoot);
        assert!(result.clean());
        assert!((result.survival - 1.0).abs() < 1e-12);
    }

    #[test]
    fn out_of_scope_edits_are_stripped() {
        let result = ChainSanitizer::new()
            .sanitize(
                &patch(vec![
                    AstEdit::new("src/cache.py", "Cache.get", 2, 0),
                    AstEdit::new("src/unrelated.py", "Helper.run", 50, 0),
                ]),
                &scope(),
            )
            .expect("sanitize");
        assert_eq!(result.kept.len(), 1);
        assert_eq!(result.stripped.len(), 1);
        assert_eq!(result.overshoot_lines, 50);
        assert!((result.overshoot_ratio - 50.0 / 52.0).abs() < 1e-12);
    }

    #[test]
    fn missing_required_symbol_sets_undershoot() {
        let result = ChainSanitizer::new()
            .sanitize(
                &patch(vec![AstEdit::new("src/cache.py", "Cache.set", 1, 0)]),
                &scope(),
            )
            .expect("sanitize");
        assert!(result.undershoot);
        assert_eq!(result.required_missing, vec!["Cache.get".to_string()]);
        assert!(!result.clean());
    }

    #[test]
    fn scope_mismatch_is_rejected() {
        let err = ChainSanitizer::new()
            .sanitize(
                &ChainPatch::new(
                    "other",
                    vec![AstEdit::new("src/cache.py", "Cache.get", 1, 0)],
                ),
                &scope(),
            )
            .unwrap_err();
        assert!(matches!(err, ChainError::ScopeMismatch { .. }));
    }

    #[test]
    fn empty_patch_and_empty_scope_are_rejected() {
        assert_eq!(
            ChainSanitizer::new()
                .sanitize(&patch(vec![]), &scope())
                .unwrap_err(),
            ChainError::EmptyPatch
        );
        let empty_scope = BugScope::new("bug-1", vec![], vec![], vec![]);
        assert!(matches!(
            ChainSanitizer::new()
                .sanitize(
                    &patch(vec![AstEdit::new("src/cache.py", "Cache.get", 1, 0)]),
                    &empty_scope
                )
                .unwrap_err(),
            ChainError::EmptyField("scope")
        ));
    }

    #[test]
    fn survival_decays_with_overshoot() {
        let survival = ChainSanitizer::new()
            .chain_survival(&[0.9, 0.8], 2)
            .expect("survival");
        assert!((survival - 0.72 * (-2.0_f64).exp()).abs() < 1e-12);
    }

    #[test]
    fn invalid_pass_rates_are_rejected() {
        assert!(matches!(
            ChainSanitizer::new().chain_survival(&[1.2], 0).unwrap_err(),
            ChainError::InvalidPassRate(1.2)
        ));
        assert_eq!(
            ChainSanitizer::new().chain_survival(&[], 0).unwrap_err(),
            ChainError::EmptyStepRates
        );
    }

    #[test]
    fn lambda_must_be_non_negative() {
        assert!(matches!(
            ChainSanitizer::with_lambda(-1.0).unwrap_err(),
            ChainError::InvalidLambda(-1.0)
        ));
        let sanitizer = ChainSanitizer::with_lambda(2.0).expect("lambda");
        assert!((sanitizer.lambda() - 2.0).abs() < 1e-12);
    }

    #[test]
    fn survival_matches_sanitised_patch() {
        let sanitizer = ChainSanitizer::new();
        let result = sanitizer
            .sanitize(
                &patch(vec![
                    AstEdit::new("src/cache.py", "Cache.get", 1, 0),
                    AstEdit::new("src/other.py", "Other.run", 3, 0),
                ]),
                &scope(),
            )
            .expect("sanitize");
        assert!((result.survival - (-3.0_f64).exp()).abs() < 1e-12);
        assert_eq!(result.kept_lines(), 1);
    }
}
