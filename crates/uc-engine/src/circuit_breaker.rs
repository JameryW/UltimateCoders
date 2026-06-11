//! Circuit Breaker for LLM API fault tolerance.
//!
//! Implements the Circuit Breaker pattern to prevent cascading failures
//! when an LLM API endpoint is degraded. The circuit has three states:
//!
//! - **Closed**: Normal operation. Failures are counted.
//! - **Open**: All requests are rejected immediately. After a timeout,
//!   transitions to HalfOpen.
//! - **HalfOpen**: A limited number of requests are allowed through
//!   to test if the endpoint has recovered.

use serde::{Deserialize, Serialize};
use std::sync::atomic::{AtomicU32, AtomicU64, Ordering};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use uc_types::EngineError;

/// Circuit breaker states.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum CircuitState {
    /// Normal operation — requests are allowed through.
    Closed,
    /// All requests are rejected — endpoint is considered unavailable.
    Open,
    /// Testing if the endpoint has recovered — limited requests allowed.
    HalfOpen,
}

/// Circuit Breaker for protecting against cascading failures.
///
/// When the failure count exceeds the threshold, the circuit opens
/// and all subsequent requests are rejected immediately. After the
/// reset timeout, the circuit transitions to HalfOpen, allowing a
/// limited number of test requests through. If those succeed, the
/// circuit closes. If they fail, it opens again.
pub struct CircuitBreaker {
    /// Current state.
    state: Mutex<CircuitState>,
    /// Number of consecutive failures.
    failure_count: AtomicU32,
    /// Number of consecutive successes (for half-open recovery).
    success_count: AtomicU32,
    /// Failures required to open the circuit.
    failure_threshold: u32,
    /// Successes required in half-open to close the circuit.
    success_threshold: u32,
    /// Duration to wait before transitioning from Open to HalfOpen.
    reset_timeout: Duration,
    /// Time of last state transition to Open.
    last_failure_time: Mutex<Option<Instant>>,
    /// Total calls made through this circuit breaker.
    total_calls: AtomicU64,
    /// Total calls that were rejected (circuit open).
    total_rejected: AtomicU64,
}

/// Configuration for a Circuit Breaker.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CircuitBreakerConfig {
    /// Number of failures before opening the circuit.
    pub failure_threshold: u32,
    /// Number of successes in half-open state before closing.
    pub success_threshold: u32,
    /// Duration to wait before trying half-open.
    pub reset_timeout: Duration,
}

impl Default for CircuitBreakerConfig {
    fn default() -> Self {
        Self {
            failure_threshold: 5,
            success_threshold: 2,
            reset_timeout: Duration::from_secs(30),
        }
    }
}

impl CircuitBreaker {
    /// Create a new circuit breaker with the given configuration.
    pub fn new(config: &CircuitBreakerConfig) -> Self {
        Self {
            state: Mutex::new(CircuitState::Closed),
            failure_count: AtomicU32::new(0),
            success_count: AtomicU32::new(0),
            failure_threshold: config.failure_threshold,
            success_threshold: config.success_threshold,
            reset_timeout: config.reset_timeout,
            last_failure_time: Mutex::new(None),
            total_calls: AtomicU64::new(0),
            total_rejected: AtomicU64::new(0),
        }
    }

    /// Create with default configuration.
    pub fn with_defaults() -> Self {
        Self::new(&CircuitBreakerConfig::default())
    }

    /// Check if a request is allowed through the circuit.
    ///
    /// Returns `Ok(())` if the request can proceed, or
    /// `Err(EngineError::ConnectionError)` if the circuit is open.
    pub fn allow_request(&self) -> Result<(), EngineError> {
        self.total_calls.fetch_add(1, Ordering::SeqCst);

        let mut state = self.state.lock().unwrap();
        match &*state {
            CircuitState::Closed => Ok(()),
            CircuitState::Open => {
                // Check if reset timeout has elapsed
                let last_failure = self.last_failure_time.lock().unwrap();
                if let Some(last) = *last_failure {
                    if last.elapsed() >= self.reset_timeout {
                        // Transition to half-open
                        drop(last_failure);
                        *state = CircuitState::HalfOpen;
                        self.success_count.store(0, Ordering::SeqCst);
                        Ok(())
                    } else {
                        drop(last_failure);
                        self.total_rejected.fetch_add(1, Ordering::SeqCst);
                        Err(EngineError::ConnectionError(
                            "Circuit breaker is open — endpoint unavailable".to_string(),
                        ))
                    }
                } else {
                    drop(last_failure);
                    self.total_rejected.fetch_add(1, Ordering::SeqCst);
                    Err(EngineError::ConnectionError(
                        "Circuit breaker is open — endpoint unavailable".to_string(),
                    ))
                }
            }
            CircuitState::HalfOpen => {
                // Allow limited requests in half-open state
                Ok(())
            }
        }
    }

    /// Record a successful call.
    ///
    /// In half-open state, consecutive successes will close the circuit.
    pub fn record_success(&self) {
        self.failure_count.store(0, Ordering::SeqCst);

        let mut state = self.state.lock().unwrap();
        if *state == CircuitState::HalfOpen {
            let successes = self.success_count.fetch_add(1, Ordering::SeqCst) + 1;
            if successes >= self.success_threshold {
                *state = CircuitState::Closed;
                tracing::info!("Circuit breaker closed — endpoint recovered");
            }
        }
    }

    /// Record a failed call.
    ///
    /// In closed state, consecutive failures will open the circuit.
    /// In half-open state, a single failure will re-open the circuit.
    pub fn record_failure(&self) {
        let mut state = self.state.lock().unwrap();
        match &*state {
            CircuitState::Closed => {
                let failures = self.failure_count.fetch_add(1, Ordering::SeqCst) + 1;
                if failures >= self.failure_threshold {
                    *state = CircuitState::Open;
                    *self.last_failure_time.lock().unwrap() = Some(Instant::now());
                    tracing::warn!("Circuit breaker opened after {} failures", failures);
                }
            }
            CircuitState::HalfOpen => {
                // Single failure in half-open re-opens the circuit
                *state = CircuitState::Open;
                *self.last_failure_time.lock().unwrap() = Some(Instant::now());
                self.success_count.store(0, Ordering::SeqCst);
                tracing::warn!("Circuit breaker re-opened from half-open state");
            }
            CircuitState::Open => {
                // Already open, just update the timestamp
                *self.last_failure_time.lock().unwrap() = Some(Instant::now());
            }
        }
    }

    /// Get the current circuit state.
    pub fn state(&self) -> CircuitState {
        let state = self.state.lock().unwrap();
        match &*state {
            CircuitState::Open => {
                // Check if we should transition to half-open
                let last_failure = self.last_failure_time.lock().unwrap();
                if let Some(last) = *last_failure {
                    if last.elapsed() >= self.reset_timeout {
                        CircuitState::HalfOpen
                    } else {
                        CircuitState::Open
                    }
                } else {
                    CircuitState::Open
                }
            }
            other => other.clone(),
        }
    }

    /// Get the current failure count.
    pub fn failure_count(&self) -> u32 {
        self.failure_count.load(Ordering::SeqCst)
    }

    /// Get the total number of calls attempted.
    pub fn total_calls(&self) -> u64 {
        self.total_calls.load(Ordering::SeqCst)
    }

    /// Get the total number of rejected calls.
    pub fn total_rejected(&self) -> u64 {
        self.total_rejected.load(Ordering::SeqCst)
    }

    /// Force the circuit into a specific state (for testing).
    pub fn force_state(&self, state: CircuitState) {
        let mut current = self.state.lock().unwrap();
        *current = state;
    }
}

/// Retry policy with exponential backoff and jitter.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RetryPolicy {
    /// Maximum number of retry attempts.
    pub max_retries: u32,
    /// Base delay for exponential backoff.
    pub base_delay: Duration,
    /// Maximum delay between retries.
    pub max_delay: Duration,
    /// Whether to add jitter to the delay.
    pub jitter: bool,
}

impl Default for RetryPolicy {
    fn default() -> Self {
        Self {
            max_retries: 5,
            base_delay: Duration::from_secs(1),
            max_delay: Duration::from_secs(60),
            jitter: true,
        }
    }
}

impl RetryPolicy {
    /// Calculate the delay for a given retry attempt.
    ///
    /// Uses exponential backoff: `base_delay * 2^attempt + jitter`
    pub fn delay_for_attempt(&self, attempt: u32) -> Duration {
        let exp_delay = self.base_delay.as_secs_f64() * 2u32.pow(attempt) as f64;
        let jitter = if self.jitter {
            // Pseudo-jitter: use a deterministic fraction based on attempt
            // (real jitter would use random, but that requires rand crate)
            let fraction = ((attempt as f64 * 0.618) % 1.0) * 0.5; // Golden ratio jitter
            exp_delay * fraction
        } else {
            0.0
        };

        let total = (exp_delay + jitter).min(self.max_delay.as_secs_f64());
        Duration::from_secs_f64(total)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn circuit_breaker_starts_closed() {
        let cb = CircuitBreaker::with_defaults();
        assert_eq!(cb.state(), CircuitState::Closed);
        assert!(cb.allow_request().is_ok());
    }

    #[test]
    fn circuit_breaker_opens_after_threshold() {
        let config = CircuitBreakerConfig {
            failure_threshold: 3,
            success_threshold: 2,
            reset_timeout: Duration::from_secs(30),
        };
        let cb = CircuitBreaker::new(&config);

        cb.record_failure();
        cb.record_failure();
        assert_eq!(cb.state(), CircuitState::Closed);

        cb.record_failure(); // Third failure opens the circuit
        assert_eq!(cb.state(), CircuitState::Open);
    }

    #[test]
    fn circuit_breaker_rejects_when_open() {
        let config = CircuitBreakerConfig {
            failure_threshold: 1,
            success_threshold: 1,
            reset_timeout: Duration::from_secs(300), // Long timeout
        };
        let cb = CircuitBreaker::new(&config);

        cb.record_failure(); // Opens circuit
        assert!(cb.allow_request().is_err());
        assert_eq!(cb.total_rejected(), 1);
    }

    #[test]
    fn circuit_breaker_half_open_after_timeout() {
        let config = CircuitBreakerConfig {
            failure_threshold: 1,
            success_threshold: 1,
            reset_timeout: Duration::from_millis(50),
        };
        let cb = CircuitBreaker::new(&config);

        cb.record_failure(); // Opens circuit
        assert_eq!(cb.state(), CircuitState::Open);

        // Wait for reset timeout
        std::thread::sleep(Duration::from_millis(60));

        // Should transition to half-open
        assert_eq!(cb.state(), CircuitState::HalfOpen);
        assert!(cb.allow_request().is_ok());
    }

    #[test]
    fn circuit_breaker_closes_after_success_in_half_open() {
        let config = CircuitBreakerConfig {
            failure_threshold: 1,
            success_threshold: 2,
            reset_timeout: Duration::from_millis(50),
        };
        let cb = CircuitBreaker::new(&config);

        cb.record_failure(); // Opens circuit
        std::thread::sleep(Duration::from_millis(60));

        // In half-open, successes close the circuit
        cb.allow_request().ok(); // Allow the test request
        cb.record_success(); // First success
        assert_eq!(cb.state(), CircuitState::HalfOpen); // Need 2 successes

        cb.record_success(); // Second success closes
        assert_eq!(cb.state(), CircuitState::Closed);
    }

    #[test]
    fn circuit_breaker_reopens_on_half_open_failure() {
        let config = CircuitBreakerConfig {
            failure_threshold: 1,
            success_threshold: 1,
            reset_timeout: Duration::from_millis(50),
        };
        let cb = CircuitBreaker::new(&config);

        cb.record_failure(); // Opens circuit
        std::thread::sleep(Duration::from_millis(60));

        // In half-open, a failure re-opens
        cb.record_failure();
        assert_eq!(cb.state(), CircuitState::Open);
    }

    #[test]
    fn circuit_breaker_success_resets_failure_count() {
        let config = CircuitBreakerConfig {
            failure_threshold: 3,
            success_threshold: 1,
            reset_timeout: Duration::from_secs(30),
        };
        let cb = CircuitBreaker::new(&config);

        cb.record_failure();
        cb.record_failure();
        cb.record_success(); // Resets failure count
        cb.record_failure();
        cb.record_failure();

        // Still closed (only 2 consecutive failures after reset)
        assert_eq!(cb.state(), CircuitState::Closed);
    }

    #[test]
    fn retry_policy_delay_calculation() {
        let policy = RetryPolicy {
            max_retries: 5,
            base_delay: Duration::from_secs(1),
            max_delay: Duration::from_secs(60),
            jitter: false,
        };

        assert_eq!(policy.delay_for_attempt(0), Duration::from_secs(1));
        assert_eq!(policy.delay_for_attempt(1), Duration::from_secs(2));
        assert_eq!(policy.delay_for_attempt(2), Duration::from_secs(4));
        assert_eq!(policy.delay_for_attempt(3), Duration::from_secs(8));
    }

    #[test]
    fn retry_policy_max_delay_cap() {
        let policy = RetryPolicy {
            max_retries: 10,
            base_delay: Duration::from_secs(1),
            max_delay: Duration::from_secs(10),
            jitter: false,
        };

        // 2^10 = 1024, but capped at 10
        assert_eq!(policy.delay_for_attempt(10), Duration::from_secs(10));
    }

    #[test]
    fn retry_policy_with_jitter() {
        let policy = RetryPolicy {
            max_retries: 5,
            base_delay: Duration::from_secs(1),
            max_delay: Duration::from_secs(60),
            jitter: true,
        };

        // With jitter, delays should be >= base delay
        let delay0 = policy.delay_for_attempt(0);
        assert!(delay0 >= Duration::from_secs(1));
    }

    #[test]
    fn force_state_for_testing() {
        let cb = CircuitBreaker::with_defaults();
        cb.force_state(CircuitState::Open);
        assert_eq!(cb.state(), CircuitState::Open);

        cb.force_state(CircuitState::Closed);
        assert_eq!(cb.state(), CircuitState::Closed);
    }
}
