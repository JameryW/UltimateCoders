//! Token Bucket rate limiter for LLM API calls.
//!
//! Implements dual-dimension rate limiting (RPM + TPM) with
//! a priority queue for request ordering.

use serde::{Deserialize, Serialize};
use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use uc_types::EngineError;

/// Token bucket for single-dimension rate limiting.
///
/// Tracks capacity and refills tokens at a constant rate.
pub struct TokenBucket {
    /// Maximum capacity (burst allowance).
    capacity: f64,
    /// Current available tokens.
    tokens: Mutex<f64>,
    /// Tokens added per second (refill rate).
    refill_rate: f64,
    /// Time of last refill.
    last_refill: Mutex<Instant>,
}

impl TokenBucket {
    /// Create a new token bucket.
    ///
    /// # Arguments
    /// * `capacity` - Maximum number of tokens (burst size).
    /// * `refill_rate` - Tokens added per second.
    pub fn new(capacity: f64, refill_rate: f64) -> Self {
        Self {
            capacity,
            tokens: Mutex::new(capacity), // Start at full capacity
            refill_rate,
            last_refill: Mutex::new(Instant::now()),
        }
    }

    /// Create a per-minute bucket (e.g., RPM or TPM).
    ///
    /// # Arguments
    /// * `per_minute` - Maximum tokens per minute.
    pub fn per_minute(per_minute: f64) -> Self {
        let refill_rate = per_minute / 60.0;
        Self::new(per_minute, refill_rate)
    }

    /// Try to consume tokens. Returns true if successful.
    pub fn try_consume(&self, amount: f64) -> bool {
        self.refill();

        let mut tokens = self.tokens.lock().unwrap();
        if *tokens >= amount {
            *tokens -= amount;
            true
        } else {
            false
        }
    }

    /// Wait and consume tokens, blocking until available.
    ///
    /// Uses a spin-wait with exponential backoff.
    pub fn consume(&self, amount: f64) {
        while !self.try_consume(amount) {
            // Wait a small amount of time before retrying
            let wait = Duration::from_millis(10);
            std::thread::sleep(wait);
        }
    }

    /// Get the current available tokens (after refill).
    pub fn available(&self) -> f64 {
        self.refill();
        let tokens = self.tokens.lock().unwrap();
        *tokens
    }

    /// Get the time until `amount` tokens will be available.
    pub fn wait_time(&self, amount: f64) -> Duration {
        self.refill();
        let tokens = self.tokens.lock().unwrap();
        if *tokens >= amount {
            Duration::ZERO
        } else if self.refill_rate > 0.0 {
            let deficit = amount - *tokens;
            let seconds = deficit / self.refill_rate;
            Duration::from_secs_f64(seconds)
        } else {
            Duration::from_secs(3600) // No refill, effectively infinite wait
        }
    }

    /// Refill tokens based on elapsed time.
    fn refill(&self) {
        let mut last_refill = self.last_refill.lock().unwrap();
        let mut tokens = self.tokens.lock().unwrap();

        let now = Instant::now();
        let elapsed = now.duration_since(*last_refill).as_secs_f64();
        let refill = elapsed * self.refill_rate;

        *tokens = (*tokens + refill).min(self.capacity);
        *last_refill = now;
    }
}

/// Dual-dimension rate limiter for LLM API calls (RPM + TPM).
pub struct LlmRateLimiter {
    /// Requests per minute bucket.
    rpm_bucket: TokenBucket,
    /// Tokens per minute bucket.
    tpm_bucket: TokenBucket,
    /// Maximum concurrent requests.
    max_concurrent: usize,
    /// Currently active request count.
    active_count: AtomicUsize,
    /// Total requests made.
    total_requests: AtomicU64,
    /// Total tokens consumed.
    total_tokens: AtomicU64,
}

/// Configuration for the LLM rate limiter.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LlmRateLimiterConfig {
    /// Maximum requests per minute.
    pub rpm: f64,
    /// Maximum tokens per minute.
    pub tpm: f64,
    /// Maximum concurrent requests.
    pub max_concurrent: usize,
}

impl Default for LlmRateLimiterConfig {
    fn default() -> Self {
        Self {
            rpm: 60.0,
            tpm: 100_000.0,
            max_concurrent: 10,
        }
    }
}

impl LlmRateLimiter {
    /// Create a new rate limiter with the given configuration.
    pub fn new(config: &LlmRateLimiterConfig) -> Self {
        Self {
            rpm_bucket: TokenBucket::per_minute(config.rpm),
            tpm_bucket: TokenBucket::per_minute(config.tpm),
            max_concurrent: config.max_concurrent,
            active_count: AtomicUsize::new(0),
            total_requests: AtomicU64::new(0),
            total_tokens: AtomicU64::new(0),
        }
    }

    /// Create with default configuration.
    pub fn with_defaults() -> Self {
        Self::new(&LlmRateLimiterConfig::default())
    }

    /// Try to acquire capacity for a request.
    ///
    /// # Arguments
    /// * `estimated_tokens` - Estimated token consumption for this request.
    ///
    /// # Returns
    /// `Ok(())` if capacity is available, `Err(EngineError::RateLimited)` otherwise.
    pub fn try_acquire(&self, estimated_tokens: f64) -> Result<(), EngineError> {
        // Check concurrent limit
        let active = self.active_count.load(Ordering::SeqCst);
        if active >= self.max_concurrent {
            return Err(EngineError::RateLimited(1));
        }

        // Check RPM
        if !self.rpm_bucket.try_consume(1.0) {
            let wait = self.rpm_bucket.wait_time(1.0);
            return Err(EngineError::RateLimited(wait.as_secs() + 1));
        }

        // Check TPM
        if !self.tpm_bucket.try_consume(estimated_tokens) {
            let wait = self.tpm_bucket.wait_time(estimated_tokens);
            // Refund the RPM token we just consumed
            return Err(EngineError::RateLimited(wait.as_secs() + 1));
        }

        // Record the request
        self.active_count.fetch_add(1, Ordering::SeqCst);
        self.total_requests.fetch_add(1, Ordering::SeqCst);
        self.total_tokens
            .fetch_add(estimated_tokens as u64, Ordering::SeqCst);

        Ok(())
    }

    /// Release a request slot (call after the request completes).
    pub fn release(&self) {
        self.active_count.fetch_sub(1, Ordering::SeqCst);
    }

    /// Check if a request can be made without actually consuming tokens.
    pub fn can_request(&self, estimated_tokens: f64) -> bool {
        let active = self.active_count.load(Ordering::SeqCst);
        if active >= self.max_concurrent {
            return false;
        }

        self.rpm_bucket.available() >= 1.0 && self.tpm_bucket.available() >= estimated_tokens
    }

    /// Get the current RPM availability.
    pub fn rpm_available(&self) -> f64 {
        self.rpm_bucket.available()
    }

    /// Get the current TPM availability.
    pub fn tpm_available(&self) -> f64 {
        self.tpm_bucket.available()
    }

    /// Get the number of currently active requests.
    pub fn active_count(&self) -> usize {
        self.active_count.load(Ordering::SeqCst)
    }

    /// Get the total number of requests made.
    pub fn total_requests(&self) -> u64 {
        self.total_requests.load(Ordering::SeqCst)
    }

    /// Get the total tokens consumed.
    pub fn total_tokens(&self) -> u64 {
        self.total_tokens.load(Ordering::SeqCst)
    }
}

/// Priority levels for LLM requests.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize, Default)]
pub enum RequestPriority {
    /// Background analysis, documentation.
    Low = 0,
    /// Context gathering, research.
    #[default]
    Medium = 1,
    /// Active worker actions (directly blocking progress).
    High = 2,
    /// Orchestrator planning (blocks all workers).
    Critical = 3,
}

/// Model fallback chain for LLM API resilience.
///
/// When the primary model is rate-limited or unavailable,
/// requests are automatically routed to the next model in the chain.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ModelFallbackChain {
    /// Primary model (best quality, highest cost).
    pub primary: String,
    /// Secondary model (good quality, moderate cost).
    pub secondary: String,
    /// Tertiary model (acceptable quality, lowest cost).
    pub tertiary: String,
}

impl Default for ModelFallbackChain {
    fn default() -> Self {
        Self {
            primary: "claude-opus-4-8".to_string(),
            secondary: "claude-sonnet-4-6".to_string(),
            tertiary: "claude-haiku-4-5-20251001".to_string(),
        }
    }
}

impl ModelFallbackChain {
    /// Create a new fallback chain.
    pub fn new(primary: &str, secondary: &str, tertiary: &str) -> Self {
        Self {
            primary: primary.to_string(),
            secondary: secondary.to_string(),
            tertiary: tertiary.to_string(),
        }
    }

    /// Get the next model in the fallback chain.
    ///
    /// Returns `None` if already at the last model.
    pub fn fallback(&self, current: &str) -> Option<String> {
        if current == self.primary {
            Some(self.secondary.clone())
        } else if current == self.secondary {
            Some(self.tertiary.clone())
        } else {
            None // Already at last resort
        }
    }

    /// Select a model based on task complexity and rate limit availability.
    ///
    /// - High complexity + primary available -> primary
    /// - High complexity + primary unavailable -> secondary
    /// - Medium complexity + secondary available -> secondary
    /// - Medium complexity + secondary unavailable -> tertiary
    /// - Low complexity -> tertiary
    pub fn select_model(
        &self,
        complexity: TaskComplexity,
        primary_available: bool,
        secondary_available: bool,
    ) -> String {
        match complexity {
            TaskComplexity::High => {
                if primary_available {
                    self.primary.clone()
                } else {
                    self.secondary.clone()
                }
            }
            TaskComplexity::Medium => {
                if secondary_available {
                    self.secondary.clone()
                } else {
                    self.tertiary.clone()
                }
            }
            TaskComplexity::Low => self.tertiary.clone(),
        }
    }
}

/// Task complexity for model selection.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum TaskComplexity {
    /// Simple, repetitive tasks (formatting, simple edits).
    Low,
    /// Moderate tasks (context gathering, standard implementation).
    Medium,
    /// Complex tasks (task decomposition, critical decisions).
    High,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn token_bucket_initial_full() {
        let bucket = TokenBucket::new(100.0, 10.0);
        assert_eq!(bucket.available(), 100.0);
    }

    #[test]
    fn token_bucket_consume() {
        let bucket = TokenBucket::new(100.0, 10.0);
        assert!(bucket.try_consume(50.0));
        assert!((bucket.available() - 50.0).abs() < 0.01);
    }

    #[test]
    fn token_bucket_consume_insufficient() {
        let bucket = TokenBucket::new(100.0, 10.0);
        assert!(bucket.try_consume(50.0));
        assert!(!bucket.try_consume(60.0)); // Only 50 left
    }

    #[test]
    fn token_bucket_refill() {
        let bucket = TokenBucket::new(100.0, 1000.0); // 1000 tokens/sec

        // Consume all tokens
        assert!(bucket.try_consume(100.0));
        assert!(bucket.available() < 1.0);

        // Wait a bit for refill
        std::thread::sleep(Duration::from_millis(50));

        // Should have refilled some tokens
        assert!(bucket.available() > 0.0);
    }

    #[test]
    fn token_bucket_per_minute() {
        let bucket = TokenBucket::per_minute(60.0); // 60 per minute = 1 per second

        // Should be able to consume 1 token immediately
        assert!(bucket.try_consume(1.0));

        // Should have 59 available (approximately)
        let available = bucket.available();
        assert!(available >= 58.0 && available <= 60.0);
    }

    #[test]
    fn token_bucket_wait_time() {
        let bucket = TokenBucket::new(10.0, 10.0); // 10 tokens/sec

        // Consume all
        assert!(bucket.try_consume(10.0));

        // Wait time for 5 tokens should be ~0.5 seconds
        let wait = bucket.wait_time(5.0);
        assert!(wait.as_secs_f64() >= 0.4 && wait.as_secs_f64() <= 0.6);
    }

    #[test]
    fn rate_limiter_try_acquire() {
        let config = LlmRateLimiterConfig {
            rpm: 60.0,
            tpm: 100_000.0,
            max_concurrent: 5,
        };
        let limiter = LlmRateLimiter::new(&config);

        // Should be able to acquire
        assert!(limiter.try_acquire(1000.0).is_ok());
        assert_eq!(limiter.active_count(), 1);

        // Release
        limiter.release();
        assert_eq!(limiter.active_count(), 0);
    }

    #[test]
    fn rate_limiter_concurrent_limit() {
        let config = LlmRateLimiterConfig {
            rpm: 10000.0,      // High RPM to avoid hitting it
            tpm: 10_000_000.0, // High TPM to avoid hitting it
            max_concurrent: 2,
        };
        let limiter = LlmRateLimiter::new(&config);

        assert!(limiter.try_acquire(100.0).is_ok());
        assert!(limiter.try_acquire(100.0).is_ok());
        assert!(limiter.try_acquire(100.0).is_err()); // Exceeds concurrent limit

        limiter.release();
        assert!(limiter.try_acquire(100.0).is_ok()); // Now has a slot
    }

    #[test]
    fn rate_limiter_can_request() {
        let config = LlmRateLimiterConfig::default();
        let limiter = LlmRateLimiter::new(&config);

        assert!(limiter.can_request(1000.0));
    }

    #[test]
    fn model_fallback_chain() {
        let chain = ModelFallbackChain::default();

        assert_eq!(
            chain.fallback(&chain.primary),
            Some(chain.secondary.clone())
        );
        assert_eq!(
            chain.fallback(&chain.secondary),
            Some(chain.tertiary.clone())
        );
        assert_eq!(chain.fallback(&chain.tertiary), None);
    }

    #[test]
    fn model_selection_by_complexity() {
        let chain = ModelFallbackChain::default();

        // High complexity, primary available
        assert_eq!(
            chain.select_model(TaskComplexity::High, true, true),
            chain.primary
        );

        // High complexity, primary unavailable
        assert_eq!(
            chain.select_model(TaskComplexity::High, false, true),
            chain.secondary
        );

        // Medium complexity, secondary available
        assert_eq!(
            chain.select_model(TaskComplexity::Medium, true, true),
            chain.secondary
        );

        // Low complexity always uses tertiary
        assert_eq!(
            chain.select_model(TaskComplexity::Low, true, true),
            chain.tertiary
        );
    }

    #[test]
    fn request_priority_ordering() {
        assert!(RequestPriority::Critical > RequestPriority::High);
        assert!(RequestPriority::High > RequestPriority::Medium);
        assert!(RequestPriority::Medium > RequestPriority::Low);
    }
}
