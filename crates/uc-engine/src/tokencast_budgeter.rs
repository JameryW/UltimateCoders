//! TokenCast: composable execution-segment cost forecasting.
//!
//! Implements ledger item #57 (arXiv:2609.35760). Each segment contributes a
//! direct token cost and a context-growth cost multiplied by how often that
//! growth is expected to be reread. The contributions are associative, so the
//! controller can combine already-observed segments with the predicted
//! remainder on every step without another model call.

use serde::{Deserialize, Serialize};

pub const DEFAULT_TOKEN_BUDGET: usize = 128_000;

/// One observable execution segment.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct ExecutionSegment {
    pub direct_tokens: usize,
    pub context_growth: usize,
    pub future_reread_multiplier: f32,
}

impl ExecutionSegment {
    pub fn new(direct_tokens: usize, context_growth: usize, future_reread_multiplier: f32) -> Self {
        Self {
            direct_tokens,
            context_growth,
            future_reread_multiplier: future_reread_multiplier.max(0.0),
        }
    }

    /// The segment's own composable cost contribution.
    pub fn contribution(self) -> f64 {
        self.direct_tokens as f64
            + self.context_growth as f64 * self.future_reread_multiplier as f64
    }
}

/// A forecast over the observed prefix and a caller-supplied remainder.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct TokenForecast {
    pub observed_segments: usize,
    pub observed_tokens: f64,
    pub remaining_segments: usize,
    pub remaining_tokens: f64,
    pub predicted_total: f64,
}

impl TokenForecast {
    pub fn headroom(self, budget: usize) -> f64 {
        budget as f64 - self.predicted_total
    }

    pub fn over_budget(self, budget: usize) -> bool {
        self.predicted_total > budget as f64
    }
}

/// The budget action recommended by the forecaster.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum BudgetAction {
    WithinBudget,
    Relax,
    Compact,
    Prune,
}

/// Forecast plus the deterministic budget recommendation.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct BudgetDecision {
    pub forecast: TokenForecast,
    pub action: BudgetAction,
    pub headroom: f64,
}

/// Stateful but deterministic TokenCast bookkeeper.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct TokenCastBudgeter {
    observed: Vec<ExecutionSegment>,
    max_segments: usize,
}

impl Default for TokenCastBudgeter {
    fn default() -> Self {
        Self::new()
    }
}

impl TokenCastBudgeter {
    pub const DEFAULT_MAX_SEGMENTS: usize = 4096;

    pub fn new() -> Self {
        Self::with_capacity(Self::DEFAULT_MAX_SEGMENTS)
    }

    pub fn with_capacity(max_segments: usize) -> Self {
        Self {
            observed: Vec::new(),
            max_segments: max_segments.max(1),
        }
    }

    pub fn observe(&mut self, segment: ExecutionSegment) -> bool {
        if self.observed.len() >= self.max_segments {
            return false;
        }
        self.observed.push(segment);
        true
    }

    pub fn observed(&self) -> &[ExecutionSegment] {
        &self.observed
    }

    pub fn observed_tokens(&self) -> f64 {
        self.observed
            .iter()
            .map(|segment| segment.contribution())
            .sum()
    }

    pub fn forecast(&self, remaining: &[ExecutionSegment]) -> TokenForecast {
        let observed_tokens = self.observed_tokens();
        let remaining_tokens: f64 = remaining.iter().map(|segment| segment.contribution()).sum();
        TokenForecast {
            observed_segments: self.observed.len(),
            observed_tokens,
            remaining_segments: remaining.len(),
            remaining_tokens,
            predicted_total: observed_tokens + remaining_tokens,
        }
    }

    /// Recommend whether the run should continue, relax, compact, or prune.
    ///
    /// `progress` is a caller-supplied fraction in `[0.0, 1.0]`. A nearly
    /// converged task is allowed to exceed the budget; an early high-growth
    /// task should be compacted; a late high-growth task should be pruned.
    pub fn decide(
        &self,
        remaining: &[ExecutionSegment],
        budget: usize,
        progress: f32,
    ) -> BudgetDecision {
        let forecast = self.forecast(remaining);
        let progress = progress.clamp(0.0, 1.0);
        let action = if !forecast.over_budget(budget) {
            BudgetAction::WithinBudget
        } else if progress >= 0.8 {
            BudgetAction::Relax
        } else if progress >= 0.5 {
            BudgetAction::Prune
        } else {
            BudgetAction::Compact
        };
        BudgetDecision {
            forecast,
            action,
            headroom: forecast.headroom(budget),
        }
    }

    pub fn clear(&mut self) {
        self.observed.clear();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn segment(direct: usize, growth: usize, multiplier: f32) -> ExecutionSegment {
        ExecutionSegment::new(direct, growth, multiplier)
    }

    #[test]
    fn segment_contribution_uses_reread_multiplier() {
        assert_eq!(segment(100, 50, 2.0).contribution(), 200.0);
        assert_eq!(segment(100, 50, 0.0).contribution(), 100.0);
    }

    #[test]
    fn empty_forecast_is_zero() {
        let budgeter = TokenCastBudgeter::new();
        let forecast = budgeter.forecast(&[]);
        assert_eq!(forecast.predicted_total, 0.0);
        assert_eq!(forecast.observed_segments, 0);
    }

    #[test]
    fn observed_and_remaining_are_additive() {
        let mut budgeter = TokenCastBudgeter::new();
        budgeter.observe(segment(100, 50, 2.0));
        let forecast = budgeter.forecast(&[segment(10, 0, 0.0), segment(0, 5, 3.0)]);
        assert_eq!(forecast.observed_tokens, 200.0);
        assert_eq!(forecast.remaining_tokens, 25.0);
        assert_eq!(forecast.predicted_total, 225.0);
    }

    #[test]
    fn observe_is_bounded() {
        let mut budgeter = TokenCastBudgeter::with_capacity(2);
        assert!(budgeter.observe(segment(1, 0, 0.0)));
        assert!(budgeter.observe(segment(1, 0, 0.0)));
        assert!(!budgeter.observe(segment(1, 0, 0.0)));
        assert_eq!(budgeter.observed().len(), 2);
    }

    #[test]
    fn negative_multiplier_is_clamped() {
        assert_eq!(segment(5, 10, -1.0).future_reread_multiplier, 0.0);
        assert_eq!(segment(5, 10, -1.0).contribution(), 5.0);
    }

    #[test]
    fn within_budget_returns_no_action() {
        let budgeter = TokenCastBudgeter::new();
        let decision = budgeter.decide(&[segment(100, 0, 0.0)], 1_000, 0.1);
        assert_eq!(decision.action, BudgetAction::WithinBudget);
        assert!(decision.headroom > 0.0);
    }

    #[test]
    fn early_overrun_compacts() {
        let budgeter = TokenCastBudgeter::new();
        let decision = budgeter.decide(&[segment(800, 200, 2.0)], 1_000, 0.2);
        assert!(decision.forecast.over_budget(1_000));
        assert_eq!(decision.action, BudgetAction::Compact);
    }

    #[test]
    fn mid_overrun_prunes() {
        let budgeter = TokenCastBudgeter::new();
        let decision = budgeter.decide(&[segment(800, 200, 2.0)], 1_000, 0.6);
        assert_eq!(decision.action, BudgetAction::Prune);
    }

    #[test]
    fn near_convergence_relaxes() {
        let budgeter = TokenCastBudgeter::new();
        let decision = budgeter.decide(&[segment(800, 200, 2.0)], 1_000, 0.9);
        assert_eq!(decision.action, BudgetAction::Relax);
    }

    #[test]
    fn clear_resets_the_observed_prefix() {
        let mut budgeter = TokenCastBudgeter::new();
        budgeter.observe(segment(10, 10, 1.0));
        assert_eq!(budgeter.observed_tokens(), 20.0);
        budgeter.clear();
        assert_eq!(budgeter.observed_tokens(), 0.0);
        assert!(budgeter.observed().is_empty());
    }
}
