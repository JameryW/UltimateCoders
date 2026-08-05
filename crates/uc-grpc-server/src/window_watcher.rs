//! Night-window transition watcher.
//!
//! Polls the `SchedulerService`'s configured night window every 60s and
//! publishes `schedule.window.opened` / `schedule.window.closed` events to
//! NATS on inside/outside transitions. This wires the emitter
//! (`publish_window_event`) that was previously defined but never called.
//!
//! ## Layering
//!
//! The watcher lives in the gateway (uc-grpc-server), not in uc-engine,
//! because `publish_window_event` requires an `async_nats::Client` and
//! uc-engine does not depend on async-nats at runtime (only via the optional
//! `messaging` feature, and even then the engine layer should not own NATS
//! connections). This mirrors the `NatsSubmitDispatcher` layering decision
//! (uc-grpc owns NATS-using scheduler code).
//!
//! ## No-op conditions
//!
//! The watcher is not spawned when:
//! - No night window is configured (no events needed).
//! - The `messaging` feature is disabled (no NATS).
//!
//! ## Precision
//!
//! Night windows are coarse (HH:MM granularity), so a 60s poll interval is
//! sufficient — sub-minute precision is unnecessary.

// The entire module is messaging-gated: publish_window_event + async_nats::Client
// require the messaging feature. Without it, the module compiles to nothing and
// start_window_watcher is a no-op stub (declared in main.rs via #[cfg(not(...))]).
#[cfg(feature = "messaging")]
mod messaging_impl {
    use std::sync::Arc;
    use std::time::Duration;

    use uc_engine::scheduler::{LockProvider, NightWindow, SchedulerService};
    use uc_types::NightWindowConfig;

    /// Poll interval for the window watcher.
    ///
    /// Night windows are HH:MM granularity; 60s polling detects transitions
    /// within one minute of the boundary, which is acceptable.
    const POLL_INTERVAL: Duration = Duration::from_secs(60);

    /// TTL for the per-transition distributed lock.
    ///
    /// Window transitions are coarse (minute granularity), so 60s is plenty
    /// for one instance to publish the event before the lock auto-expires.
    /// The lock only deduplicates the one-time transition publish — it is not
    /// held for the duration of any task execution.
    const WINDOW_LOCK_TTL: Duration = Duration::from_secs(60);

    /// Start the night-window transition watcher.
    ///
    /// Reads the configured night window from the `SchedulerService`. If no
    /// window is configured (`None`), returns immediately without spawning a
    /// task (no-op — no events needed when there's no window).
    ///
    /// When a window IS configured, spawns a background tokio task that:
    /// 1. Computes the initial inside/outside state.
    /// 2. Every 60s, re-checks `is_within_window(now)`.
    /// 3. On transition (outside→inside = Opened, inside→outside = Closed),
    ///    calls `publish_window_event` to publish to NATS.
    ///
    /// Publish failures are logged but do not stop the watcher (best-effort).
    ///
    /// # Arguments
    /// * `scheduler` - The scheduler service (Arc — cheap clone).
    /// * `nats_client` - The NATS client for publishing events.
    /// * `lock_provider` - Distributed lock provider for deduplicating transition
    ///   events across multi-instance gateways. Before publishing a transition,
    ///   the watcher acquires a per-transition lock (`window:{event_type}:{tick}`);
    ///   if another instance holds it, this instance skips the publish. Pass
    ///   [`NoOpLockProvider`](uc_engine::scheduler::NoOpLockProvider) for
    ///   single-instance deployments (always acquires — no dedup, no regression).
    pub fn start_window_watcher(
        scheduler: Arc<SchedulerService>,
        nats_client: async_nats::Client,
        lock_provider: Arc<dyn LockProvider>,
    ) {
        // We need to check the night window config asynchronously to decide
        // whether to spawn. Spawn a helper that does the async read + conditionally
        // starts the polling loop.
        tokio::spawn(async move {
            let config = scheduler.get_night_window_config().await;
            let window_config = match config {
                Some(c) => c,
                None => {
                    tracing::info!("Window watcher not started (no night window configured)");
                    return;
                }
            };

            // Build the NightWindow from config for is_within_window checks.
            let window = match NightWindow::from_config(&window_config) {
                Ok(w) => w,
                Err(e) => {
                    tracing::warn!(
                        error = %e,
                        "Window watcher: failed to build NightWindow from config, not starting"
                    );
                    return;
                }
            };

            let window_info = format_window_info(&window_config);

            tracing::info!(
                window = %window,
                poll_interval_secs = POLL_INTERVAL.as_secs(),
                "Window watcher started — will publish schedule.window.opened/closed on transitions"
            );

            // Initial state: check if we're currently inside the window.
            let now = chrono::Utc::now().with_timezone(&window.tz);
            let mut last_inside = window.is_within_window(now);

            tracing::info!(
                inside = last_inside,
                "Window watcher initial state: {}",
                if last_inside { "inside" } else { "outside" }
            );

            loop {
                tokio::time::sleep(POLL_INTERVAL).await;

                let now = chrono::Utc::now().with_timezone(&window.tz);
                let now_inside = window.is_within_window(now);

                if now_inside != last_inside {
                    // Transition detected — publish the appropriate event.
                    let event_type = if now_inside {
                        // outside → inside = window opened
                        uc_engine::scheduler::WindowEventType::Opened
                    } else {
                        // inside → outside = window closed
                        uc_engine::scheduler::WindowEventType::Closed
                    };

                    // Distributed lock: in multi-instance deployments, all N
                    // gateways detect the same transition and would each publish
                    // a duplicate event. Acquire a per-transition lock so only
                    // one instance publishes. The lock key includes the tick
                    // timestamp so each transition gets a fresh lock.
                    //
                    // On lock-failure (another instance holds it): skip the
                    // publish but STILL update last_inside — another instance is
                    // handling the publish, and we don't want to re-attempt the
                    // same transition on the next tick.
                    let event_name = match event_type {
                        uc_engine::scheduler::WindowEventType::Opened => "opened",
                        uc_engine::scheduler::WindowEventType::Closed => "closed",
                    };
                    let tick_ts = chrono::Utc::now().timestamp();
                    let lock_key = format!("window:{}:{}", event_name, tick_ts);

                    if !lock_provider.try_acquire(&lock_key, WINDOW_LOCK_TTL) {
                        tracing::info!(
                            event = ?event_type,
                            lock_key = %lock_key,
                            "Window watcher: lock held by another instance — skipping publish (dedup)"
                        );
                        // Update last_inside so we don't re-attempt this
                        // transition next tick (another instance is publishing).
                        last_inside = now_inside;
                        continue;
                    }

                    tracing::info!(
                        event = ?event_type,
                        inside = now_inside,
                        "Night window transition detected — publishing event"
                    );

                    // Best-effort publish: log on failure but continue watching.
                    if let Err(e) = uc_engine::scheduler::publish_window_event(
                        &nats_client,
                        event_type,
                        &window_info,
                    )
                    .await
                    {
                        tracing::warn!(
                            error = %e,
                            "Window watcher: failed to publish transition event (continuing)"
                        );
                    }

                    last_inside = now_inside;
                }
            }
        });
    }

    /// Format the window info string for the event payload.
    ///
    /// Returns a human-readable description like `"22:00-06:00 Asia/Shanghai"`.
    fn format_window_info(config: &NightWindowConfig) -> String {
        format!(
            "{}-{} {}",
            config.start.format("%H:%M"),
            config.end.format("%H:%M"),
            config.timezone
        )
    }

    #[cfg(test)]
    mod tests {
        use super::*;
        use chrono::TimeZone;
        use chrono_tz::Tz;

        #[test]
        fn format_window_info_cross_midnight() {
            let config = NightWindowConfig::new(
                chrono::NaiveTime::from_hms_opt(22, 0, 0).unwrap(),
                chrono::NaiveTime::from_hms_opt(6, 0, 0).unwrap(),
                "Asia/Shanghai".to_string(),
            );
            let info = format_window_info(&config);
            assert_eq!(info, "22:00-06:00 Asia/Shanghai");
        }

        #[test]
        fn format_window_info_same_day() {
            let config = NightWindowConfig::new(
                chrono::NaiveTime::from_hms_opt(9, 0, 0).unwrap(),
                chrono::NaiveTime::from_hms_opt(17, 0, 0).unwrap(),
                "UTC".to_string(),
            );
            let info = format_window_info(&config);
            assert_eq!(info, "09:00-17:00 UTC");
        }

        #[test]
        fn format_window_info_with_minutes() {
            let config = NightWindowConfig::new(
                chrono::NaiveTime::from_hms_opt(22, 30, 0).unwrap(),
                chrono::NaiveTime::from_hms_opt(6, 15, 0).unwrap(),
                "America/New_York".to_string(),
            );
            let info = format_window_info(&config);
            assert_eq!(info, "22:30-06:15 America/New_York");
        }

        // ── Transition detection logic tests ──────────────────────────
        //
        // These tests verify the core decision logic of the watcher's loop:
        // given last_inside and now_inside, the correct WindowEventType is
        // selected. We test the logic directly (not the spawned task) to
        // avoid needing a real NATS server.

        /// Determine the event type for a transition, mirroring the watcher's loop logic.
        fn transition_event(
            last_inside: bool,
            now_inside: bool,
        ) -> Option<uc_engine::scheduler::WindowEventType> {
            if now_inside != last_inside {
                if now_inside {
                    Some(uc_engine::scheduler::WindowEventType::Opened)
                } else {
                    Some(uc_engine::scheduler::WindowEventType::Closed)
                }
            } else {
                None
            }
        }

        #[test]
        fn transition_outside_to_inside_is_opened() {
            let event = transition_event(false, true);
            assert_eq!(event, Some(uc_engine::scheduler::WindowEventType::Opened));
        }

        #[test]
        fn transition_inside_to_outside_is_closed() {
            let event = transition_event(true, false);
            assert_eq!(event, Some(uc_engine::scheduler::WindowEventType::Closed));
        }

        #[test]
        fn no_transition_stays_inside() {
            let event = transition_event(true, true);
            assert_eq!(event, None);
        }

        #[test]
        fn no_transition_stays_outside() {
            let event = transition_event(false, false);
            assert_eq!(event, None);
        }

        // ── NightWindow is_within_window transition simulation ────────
        //
        // Use a real NightWindow to verify that is_within_window transitions
        // correctly at the window boundaries — this is what the watcher polls.

        #[test]
        fn cross_midnight_window_transition_at_start_boundary() {
            // Window: 22:00-06:00 UTC
            let window = NightWindow::new(
                chrono::NaiveTime::from_hms_opt(22, 0, 0).unwrap(),
                chrono::NaiveTime::from_hms_opt(6, 0, 0).unwrap(),
                Tz::UTC,
            );

            // 21:59 → outside, 22:00 → inside (transition: Opened)
            let before = Tz::UTC.with_ymd_and_hms(2024, 1, 15, 21, 59, 0).unwrap();
            let at_start = Tz::UTC.with_ymd_and_hms(2024, 1, 15, 22, 0, 0).unwrap();

            let last_inside = window.is_within_window(before);
            let now_inside = window.is_within_window(at_start);

            assert!(!last_inside, "21:59 should be outside");
            assert!(now_inside, "22:00 should be inside");
            assert_eq!(
                transition_event(last_inside, now_inside),
                Some(uc_engine::scheduler::WindowEventType::Opened)
            );
        }

        #[test]
        fn cross_midnight_window_transition_at_end_boundary() {
            // Window: 22:00-06:00 UTC
            let window = NightWindow::new(
                chrono::NaiveTime::from_hms_opt(22, 0, 0).unwrap(),
                chrono::NaiveTime::from_hms_opt(6, 0, 0).unwrap(),
                Tz::UTC,
            );

            // 05:59 → inside, 06:00 → outside (transition: Closed)
            let before = Tz::UTC.with_ymd_and_hms(2024, 1, 15, 5, 59, 0).unwrap();
            let at_end = Tz::UTC.with_ymd_and_hms(2024, 1, 15, 6, 0, 0).unwrap();

            let last_inside = window.is_within_window(before);
            let now_inside = window.is_within_window(at_end);

            assert!(last_inside, "05:59 should be inside");
            assert!(!now_inside, "06:00 should be outside");
            assert_eq!(
                transition_event(last_inside, now_inside),
                Some(uc_engine::scheduler::WindowEventType::Closed)
            );
        }

        #[test]
        fn same_day_window_no_transition_mid_window() {
            // Window: 09:00-17:00 UTC
            let window = NightWindow::new(
                chrono::NaiveTime::from_hms_opt(9, 0, 0).unwrap(),
                chrono::NaiveTime::from_hms_opt(17, 0, 0).unwrap(),
                Tz::UTC,
            );

            // 12:00 → inside, 13:00 → inside (no transition)
            let t1 = Tz::UTC.with_ymd_and_hms(2024, 1, 15, 12, 0, 0).unwrap();
            let t2 = Tz::UTC.with_ymd_and_hms(2024, 1, 15, 13, 0, 0).unwrap();

            let last_inside = window.is_within_window(t1);
            let now_inside = window.is_within_window(t2);

            assert!(last_inside);
            assert!(now_inside);
            assert_eq!(transition_event(last_inside, now_inside), None);
        }

        // ── No-op path: no night window configured ────────────────────
        //
        // Verify that start_window_watcher returns quickly (does not block)
        // when no night window is configured. The spawned task should check
        // get_night_window_config(), find None, and return immediately.

        #[tokio::test]
        async fn watcher_noop_when_no_night_window() {
            // Create a scheduler service with NO night window configured.
            let scheduler = Arc::new(SchedulerService::new());

            // Verify no window is configured.
            assert!(scheduler.get_night_window_config().await.is_none());

            // We can't easily call start_window_watcher without a real NATS client,
            // but we CAN verify the no-op condition: the watcher's first step is
            // to check get_night_window_config() and return if None. Since that's
            // None here, the watcher would not enter the polling loop.
            //
            // This test documents the no-op contract: no night window = no watcher.
            let has_window = scheduler.get_night_window_config().await.is_some();
            assert!(!has_window, "No night window → watcher would be a no-op");
        }

        #[tokio::test]
        async fn watcher_would_start_when_night_window_configured() {
            // Create a scheduler service WITH a night window configured.
            let scheduler = Arc::new(SchedulerService::new());
            let config = NightWindowConfig::new(
                chrono::NaiveTime::from_hms_opt(22, 0, 0).unwrap(),
                chrono::NaiveTime::from_hms_opt(6, 0, 0).unwrap(),
                "UTC".to_string(),
            );
            scheduler.set_night_window(&config).await.unwrap();

            // Verify a window IS configured — the watcher would proceed past
            // the no-op check and enter the polling loop.
            let has_window = scheduler.get_night_window_config().await.is_some();
            assert!(has_window, "Night window configured → watcher would start");

            // Also verify the NightWindow can be built from the config
            // (the watcher does this before entering the loop).
            let nw_config = scheduler.get_night_window_config().await.unwrap();
            let window = NightWindow::from_config(&nw_config);
            assert!(window.is_ok(), "NightWindow::from_config should succeed");
        }

        // ── Distributed lock dedup tests ──────────────────────────────
        //
        // The watcher's transition-publish path acquires a per-transition lock
        // before publishing. These tests verify the lock-decision logic:
        // - Lock acquired → publish proceeds (existing behavior)
        // - Lock failed → publish skipped, last_inside still updated (no re-attempt)
        // - NoOpLockProvider → always acquires (single-instance fallback)
        //
        // We test the decision logic directly (not the spawned task) to avoid
        // needing a real NATS server. The logic mirrors the watcher's loop:
        // on transition, try_acquire; if false, skip publish + update last_inside.

        /// Build the lock key for a window transition, mirroring the watcher's loop.
        fn build_window_lock_key(event_name: &str, tick_ts: i64) -> String {
            format!("window:{}:{}", event_name, tick_ts)
        }

        /// A mock lock provider that returns a configurable result.
        struct MockLockProvider {
            acquire_result: bool,
            /// Records the keys passed to try_acquire for assertion.
            acquired_keys: std::sync::Mutex<Vec<String>>,
        }

        impl MockLockProvider {
            fn new(acquire_result: bool) -> Self {
                Self {
                    acquire_result,
                    acquired_keys: std::sync::Mutex::new(Vec::new()),
                }
            }

            fn keys(&self) -> Vec<String> {
                self.acquired_keys.lock().unwrap().clone()
            }
        }

        impl LockProvider for MockLockProvider {
            fn try_acquire(&self, key: &str, _ttl: Duration) -> bool {
                self.acquired_keys.lock().unwrap().push(key.to_string());
                self.acquire_result
            }
        }

        /// Simulate the watcher's transition-publish decision.
        ///
        /// Returns (published, new_last_inside) — mirrors the watcher loop:
        /// if transition detected → try_acquire → if true, publish; if false,
        /// skip publish but update last_inside (no re-attempt next tick).
        fn simulate_transition(
            last_inside: bool,
            now_inside: bool,
            lock_provider: &dyn LockProvider,
            event_name: &str,
            tick_ts: i64,
        ) -> (bool, bool) {
            if now_inside == last_inside {
                // No transition — no publish, no change.
                return (false, last_inside);
            }

            let lock_key = build_window_lock_key(event_name, tick_ts);
            if !lock_provider.try_acquire(&lock_key, WINDOW_LOCK_TTL) {
                // Lock failed — skip publish, but update last_inside (no re-attempt).
                return (false, now_inside);
            }

            // Lock acquired — publish proceeds.
            (true, now_inside)
        }

        #[test]
        fn lock_acquired_publishes_transition() {
            // When the lock is acquired, the publish proceeds (existing behavior).
            let provider = MockLockProvider::new(true);
            let (published, new_last_inside) =
                simulate_transition(false, true, &provider, "opened", 1722873600);

            assert!(published, "Lock acquired → publish should proceed");
            assert!(
                new_last_inside,
                "last_inside should update to now_inside after transition"
            );
            assert_eq!(provider.keys().len(), 1, "try_acquire called once");
            assert_eq!(
                provider.keys()[0],
                "window:opened:1722873600",
                "lock key format: window:{{event}}:{{tick}}"
            );
        }

        #[test]
        fn lock_failed_skips_publish_but_updates_last_inside() {
            // When the lock is NOT acquired (another instance holds it), the
            // publish is skipped BUT last_inside is still updated — this prevents
            // re-attempting the same transition on the next tick.
            let provider = MockLockProvider::new(false);
            let (published, new_last_inside) =
                simulate_transition(false, true, &provider, "opened", 1722873600);

            assert!(
                !published,
                "Lock failed → publish should be skipped (dedup)"
            );
            assert!(
                new_last_inside,
                "last_inside must still update — no re-attempt next tick"
            );
            assert_eq!(provider.keys().len(), 1, "try_acquire called once");
        }

        #[test]
        fn lock_failed_closed_transition_skips_publish() {
            // Same as above but for the Closed transition (inside → outside).
            let provider = MockLockProvider::new(false);
            let (published, new_last_inside) =
                simulate_transition(true, false, &provider, "closed", 1722873600);

            assert!(!published, "Lock failed → publish skipped (dedup)");
            assert!(
                !new_last_inside,
                "last_inside must update to false — no re-attempt next tick"
            );
        }

        #[test]
        fn noop_lock_provider_always_acquires_for_window_events() {
            // NoOpLockProvider is the single-instance fallback — it always
            // acquires, so the publish always proceeds (no dedup, no regression).
            let noop = uc_engine::scheduler::NoOpLockProvider;
            let (published, new_last_inside) =
                simulate_transition(false, true, &noop, "opened", 1722873600);

            assert!(published, "NoOp always acquires → publish proceeds");
            assert!(new_last_inside, "last_inside updates normally");
        }

        #[test]
        fn no_transition_no_lock_attempt() {
            // When there's no transition (now_inside == last_inside), the lock
            // is never acquired — no unnecessary KV operations.
            let provider = MockLockProvider::new(true);
            let (published, new_last_inside) =
                simulate_transition(true, true, &provider, "opened", 1722873600);

            assert!(!published, "No transition → no publish");
            assert!(new_last_inside, "last_inside unchanged");
            assert_eq!(
                provider.keys().len(),
                0,
                "No lock attempt when no transition"
            );
        }

        #[test]
        fn window_lock_key_format() {
            // Verify the lock key format: window:{opened|closed}:{timestamp}
            assert_eq!(
                build_window_lock_key("opened", 1722873600),
                "window:opened:1722873600"
            );
            assert_eq!(
                build_window_lock_key("closed", 1722873601),
                "window:closed:1722873601"
            );
        }

        #[test]
        fn window_lock_key_per_transition_distinct() {
            // Opened and Closed at the same tick get distinct keys.
            let opened_key = build_window_lock_key("opened", 1722873600);
            let closed_key = build_window_lock_key("closed", 1722873600);
            assert_ne!(opened_key, closed_key, "opened vs closed keys are distinct");
        }

        #[test]
        fn window_lock_key_per_tick_distinct() {
            // Same event type at different ticks gets distinct keys.
            let k1 = build_window_lock_key("opened", 1722873600);
            let k2 = build_window_lock_key("opened", 1722873601);
            assert_ne!(k1, k2, "different ticks → different keys");
        }

        #[tokio::test]
        async fn noop_lock_provider_trait_object_works() {
            // Verify NoOpLockProvider can be used as Arc<dyn LockProvider> —
            // this is how start_window_watcher receives it (the param type).
            use uc_engine::scheduler::NoOpLockProvider;
            let provider: Arc<dyn LockProvider> = Arc::new(NoOpLockProvider);
            assert!(
                provider.try_acquire("window:opened:123", WINDOW_LOCK_TTL),
                "NoOp via trait object always acquires"
            );
        }
    }
}

// Re-export start_window_watcher from the messaging_impl module when messaging is on.
#[cfg(feature = "messaging")]
pub use messaging_impl::start_window_watcher;
