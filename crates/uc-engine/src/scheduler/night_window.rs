//! Night-time execution window logic.
//!
//! Determines whether the current time falls within a configured night window,
//! and calculates the next window start time. Supports cross-midnight windows
//! (e.g., 22:00-06:00).

use chrono::{DateTime, NaiveTime};
use chrono_tz::Tz;
use std::fmt;

/// Night-time execution window.
///
/// Encapsulates the time window configuration and provides methods
/// to check if a given time is within the window and to calculate
/// the next window start time.
#[derive(Debug, Clone)]
pub struct NightWindow {
    /// Start of the night window (e.g., 22:00).
    pub start: NaiveTime,
    /// End of the night window (e.g., 06:00).
    pub end: NaiveTime,
    /// IANA timezone for evaluating the window.
    pub tz: Tz,
}

impl NightWindow {
    /// Create a new night window.
    pub fn new(start: NaiveTime, end: NaiveTime, tz: Tz) -> Self {
        Self { start, end, tz }
    }

    /// Create from a `NightWindowConfig` (from uc-types).
    pub fn from_config(config: &uc_types::NightWindowConfig) -> Result<Self, NightWindowError> {
        let tz: Tz = config
            .timezone
            .parse()
            .map_err(|_| NightWindowError::InvalidTimezone(config.timezone.clone()))?;
        Ok(Self::new(config.start, config.end, tz))
    }

    /// Whether the window crosses midnight (start > end).
    pub fn crosses_midnight(&self) -> bool {
        self.start > self.end
    }

    /// Check if the given time is within the night window.
    ///
    /// Handles cross-midnight windows correctly:
    /// - For 22:00-06:00: times from 22:00 to 23:59:59 and 00:00 to 05:59:59 are within window
    /// - For 20:00-23:00: times from 20:00 to 22:59:59 are within window
    pub fn is_within_window(&self, now: DateTime<Tz>) -> bool {
        let time = now.time();

        if self.crosses_midnight() {
            // Cross-midnight: within if time >= start OR time < end
            time >= self.start || time < self.end
        } else {
            // Same-day window: within if start <= time < end
            time >= self.start && time < self.end
        }
    }

    /// Calculate the next window start time after the given time.
    ///
    /// If `now` is before the start time today, the next window starts today.
    /// If `now` is after the start time today (or within the window), the next
    /// window starts tomorrow.
    pub fn next_window_start(&self, now: DateTime<Tz>) -> DateTime<Tz> {
        let today_start = self.today_window_start(now);
        if now < today_start {
            today_start
        } else {
            // Already past today's start; next window is tomorrow
            self.tomorrow_window_start(now)
        }
    }

    /// Calculate when the current (or next) window ends.
    ///
    /// If currently within the window, returns when it ends.
    /// If outside the window, returns when the next window will end.
    pub fn next_window_end(&self, now: DateTime<Tz>) -> DateTime<Tz> {
        if self.is_within_window(now) {
            self.current_window_end(now)
        } else {
            self.current_window_end(self.next_window_start(now))
        }
    }

    /// Get today's window start datetime.
    ///
    /// Returns the earliest valid instant for today's window start.
    /// Handles DST ambiguity by picking the earliest occurrence.
    fn today_window_start(&self, now: DateTime<Tz>) -> DateTime<Tz> {
        now.date_naive()
            .and_time(self.start)
            .and_local_timezone(self.tz)
            .earliest()
            .unwrap_or(now)
    }

    /// Get tomorrow's window start datetime.
    ///
    /// Returns the earliest valid instant for tomorrow's window start.
    /// Handles DST ambiguity by picking the earliest occurrence.
    fn tomorrow_window_start(&self, now: DateTime<Tz>) -> DateTime<Tz> {
        let tomorrow = now.date_naive() + chrono::Duration::days(1);
        tomorrow
            .and_time(self.start)
            .and_local_timezone(self.tz)
            .earliest()
            .unwrap_or(now)
    }

    /// Get the end datetime of the window that starts on `window_start`'s date.
    ///
    /// For cross-midnight windows, the end is on the following day.
    /// Handles DST ambiguity by picking the latest occurrence (to maximize
    /// the window duration).
    fn current_window_end(&self, window_start: DateTime<Tz>) -> DateTime<Tz> {
        let end_date = if self.crosses_midnight() {
            window_start.date_naive() + chrono::Duration::days(1)
        } else {
            window_start.date_naive()
        };
        end_date
            .and_time(self.end)
            .and_local_timezone(self.tz)
            .latest()
            .unwrap_or(window_start)
    }
}

impl fmt::Display for NightWindow {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "NightWindow({} - {} {})",
            self.start.format("%H:%M"),
            self.end.format("%H:%M"),
            self.tz
        )
    }
}

/// Errors that can occur when working with night windows.
#[derive(Debug, thiserror::Error)]
pub enum NightWindowError {
    #[error("Invalid timezone: {0}")]
    InvalidTimezone(String),
    #[error("Ambiguous time (DST transition): {0}")]
    AmbiguousTime(String),
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone;

    fn make_window(start_h: u32, start_m: u32, end_h: u32, end_m: u32, tz: Tz) -> NightWindow {
        NightWindow::new(
            NaiveTime::from_hms_opt(start_h, start_m, 0).unwrap(),
            NaiveTime::from_hms_opt(end_h, end_m, 0).unwrap(),
            tz,
        )
    }

    // ── Cross-midnight window tests ──────────────────────────────

    #[test]
    fn cross_midnight_within_window_before_midnight() {
        let window = make_window(22, 0, 6, 0, Tz::UTC);
        // 23:00 is within 22:00-06:00
        let now = Tz::UTC.with_ymd_and_hms(2024, 1, 15, 23, 0, 0).unwrap();
        assert!(window.is_within_window(now));
    }

    #[test]
    fn cross_midnight_within_window_after_midnight() {
        let window = make_window(22, 0, 6, 0, Tz::UTC);
        // 03:00 is within 22:00-06:00
        let now = Tz::UTC.with_ymd_and_hms(2024, 1, 15, 3, 0, 0).unwrap();
        assert!(window.is_within_window(now));
    }

    #[test]
    fn cross_midnight_outside_window_daytime() {
        let window = make_window(22, 0, 6, 0, Tz::UTC);
        // 12:00 is NOT within 22:00-06:00
        let now = Tz::UTC.with_ymd_and_hms(2024, 1, 15, 12, 0, 0).unwrap();
        assert!(!window.is_within_window(now));
    }

    #[test]
    fn cross_midnight_exact_boundary_start() {
        let window = make_window(22, 0, 6, 0, Tz::UTC);
        // Exactly 22:00 is within window
        let now = Tz::UTC.with_ymd_and_hms(2024, 1, 15, 22, 0, 0).unwrap();
        assert!(window.is_within_window(now));
    }

    #[test]
    fn cross_midnight_exact_boundary_end() {
        let window = make_window(22, 0, 6, 0, Tz::UTC);
        // Exactly 06:00 is NOT within window (end is exclusive)
        let now = Tz::UTC.with_ymd_and_hms(2024, 1, 15, 6, 0, 0).unwrap();
        assert!(!window.is_within_window(now));
    }

    #[test]
    fn cross_midnight_just_before_end() {
        let window = make_window(22, 0, 6, 0, Tz::UTC);
        // 05:59:59 is within window
        let now = Tz::UTC.with_ymd_and_hms(2024, 1, 15, 5, 59, 59).unwrap();
        assert!(window.is_within_window(now));
    }

    #[test]
    fn cross_midnight_just_after_start() {
        let window = make_window(22, 0, 6, 0, Tz::UTC);
        // 22:00:01 is within window
        let now = Tz::UTC.with_ymd_and_hms(2024, 1, 15, 22, 0, 1).unwrap();
        assert!(window.is_within_window(now));
    }

    #[test]
    fn cross_midnight_just_before_start() {
        let window = make_window(22, 0, 6, 0, Tz::UTC);
        // 21:59:59 is NOT within window
        let now = Tz::UTC.with_ymd_and_hms(2024, 1, 15, 21, 59, 59).unwrap();
        assert!(!window.is_within_window(now));
    }

    // ── Same-day window tests ──────────────────────────────────────

    #[test]
    fn same_day_within_window() {
        let window = make_window(20, 0, 23, 0, Tz::UTC);
        // 21:00 is within 20:00-23:00
        let now = Tz::UTC.with_ymd_and_hms(2024, 1, 15, 21, 0, 0).unwrap();
        assert!(window.is_within_window(now));
    }

    #[test]
    fn same_day_outside_window() {
        let window = make_window(20, 0, 23, 0, Tz::UTC);
        // 18:00 is NOT within 20:00-23:00
        let now = Tz::UTC.with_ymd_and_hms(2024, 1, 15, 18, 0, 0).unwrap();
        assert!(!window.is_within_window(now));
    }

    #[test]
    fn same_day_exact_boundary_start() {
        let window = make_window(20, 0, 23, 0, Tz::UTC);
        // Exactly 20:00 is within window
        let now = Tz::UTC.with_ymd_and_hms(2024, 1, 15, 20, 0, 0).unwrap();
        assert!(window.is_within_window(now));
    }

    #[test]
    fn same_day_exact_boundary_end() {
        let window = make_window(20, 0, 23, 0, Tz::UTC);
        // Exactly 23:00 is NOT within window (end is exclusive)
        let now = Tz::UTC.with_ymd_and_hms(2024, 1, 15, 23, 0, 0).unwrap();
        assert!(!window.is_within_window(now));
    }

    // ── Next window start tests ──────────────────────────────────────

    #[test]
    fn next_window_start_before_today_window() {
        let window = make_window(22, 0, 6, 0, Tz::UTC);
        // At 18:00, next window starts at 22:00 today
        let now = Tz::UTC.with_ymd_and_hms(2024, 1, 15, 18, 0, 0).unwrap();
        let next = window.next_window_start(now);
        assert_eq!(
            next,
            Tz::UTC.with_ymd_and_hms(2024, 1, 15, 22, 0, 0).unwrap()
        );
    }

    #[test]
    fn next_window_start_within_window() {
        let window = make_window(22, 0, 6, 0, Tz::UTC);
        // At 23:00 (within window), next window starts tomorrow 22:00
        let now = Tz::UTC.with_ymd_and_hms(2024, 1, 15, 23, 0, 0).unwrap();
        let next = window.next_window_start(now);
        assert_eq!(
            next,
            Tz::UTC.with_ymd_and_hms(2024, 1, 16, 22, 0, 0).unwrap()
        );
    }

    #[test]
    fn next_window_start_after_midnight_within_window() {
        let window = make_window(22, 0, 6, 0, Tz::UTC);
        // At 03:00 (within window, after midnight), next window starts today 22:00
        // (we are in the window that started yesterday, so the next one is tonight)
        let now = Tz::UTC.with_ymd_and_hms(2024, 1, 15, 3, 0, 0).unwrap();
        let next = window.next_window_start(now);
        assert_eq!(
            next,
            Tz::UTC.with_ymd_and_hms(2024, 1, 15, 22, 0, 0).unwrap()
        );
    }

    #[test]
    fn next_window_start_daytime_after_window() {
        let window = make_window(22, 0, 6, 0, Tz::UTC);
        // At 12:00 (after window ended), next window starts today 22:00
        let now = Tz::UTC.with_ymd_and_hms(2024, 1, 15, 12, 0, 0).unwrap();
        let next = window.next_window_start(now);
        assert_eq!(
            next,
            Tz::UTC.with_ymd_and_hms(2024, 1, 15, 22, 0, 0).unwrap()
        );
    }

    #[test]
    fn next_window_start_same_day_window() {
        let window = make_window(20, 0, 23, 0, Tz::UTC);
        // At 18:00, next window starts at 20:00 today
        let now = Tz::UTC.with_ymd_and_hms(2024, 1, 15, 18, 0, 0).unwrap();
        let next = window.next_window_start(now);
        assert_eq!(
            next,
            Tz::UTC.with_ymd_and_hms(2024, 1, 15, 20, 0, 0).unwrap()
        );
    }

    #[test]
    fn next_window_start_same_day_within_window() {
        let window = make_window(20, 0, 23, 0, Tz::UTC);
        // At 21:00 (within window), next window starts tomorrow 20:00
        let now = Tz::UTC.with_ymd_and_hms(2024, 1, 15, 21, 0, 0).unwrap();
        let next = window.next_window_start(now);
        assert_eq!(
            next,
            Tz::UTC.with_ymd_and_hms(2024, 1, 16, 20, 0, 0).unwrap()
        );
    }

    // ── Next window end tests ──────────────────────────────────────

    #[test]
    fn next_window_end_outside_window() {
        let window = make_window(22, 0, 6, 0, Tz::UTC);
        // At 18:00, next window ends tomorrow 06:00
        let now = Tz::UTC.with_ymd_and_hms(2024, 1, 15, 18, 0, 0).unwrap();
        let end = window.next_window_end(now);
        assert_eq!(end, Tz::UTC.with_ymd_and_hms(2024, 1, 16, 6, 0, 0).unwrap());
    }

    #[test]
    fn next_window_end_within_window() {
        let window = make_window(22, 0, 6, 0, Tz::UTC);
        // At 23:00, current window ends tomorrow 06:00
        let now = Tz::UTC.with_ymd_and_hms(2024, 1, 15, 23, 0, 0).unwrap();
        let end = window.next_window_end(now);
        assert_eq!(end, Tz::UTC.with_ymd_and_hms(2024, 1, 16, 6, 0, 0).unwrap());
    }

    #[test]
    fn next_window_end_same_day_window() {
        let window = make_window(20, 0, 23, 0, Tz::UTC);
        // At 18:00, next window ends today 23:00
        let now = Tz::UTC.with_ymd_and_hms(2024, 1, 15, 18, 0, 0).unwrap();
        let end = window.next_window_end(now);
        assert_eq!(
            end,
            Tz::UTC.with_ymd_and_hms(2024, 1, 15, 23, 0, 0).unwrap()
        );
    }

    // ── Timezone tests ──────────────────────────────────────────────

    #[test]
    fn timezone_aware_shanghai() {
        let window = make_window(22, 0, 6, 0, Tz::Asia__Shanghai);
        // 23:00 CST is within 22:00-06:00
        let now = Tz::Asia__Shanghai
            .with_ymd_and_hms(2024, 1, 15, 23, 0, 0)
            .unwrap();
        assert!(window.is_within_window(now));
    }

    #[test]
    fn timezone_aware_new_york() {
        let window = make_window(22, 0, 6, 0, Tz::America__New_York);
        // 10:00 EST is NOT within 22:00-06:00
        let now = Tz::America__New_York
            .with_ymd_and_hms(2024, 1, 15, 10, 0, 0)
            .unwrap();
        assert!(!window.is_within_window(now));
    }

    #[test]
    fn from_config_valid() {
        let config = uc_types::NightWindowConfig::new(
            NaiveTime::from_hms_opt(22, 0, 0).unwrap(),
            NaiveTime::from_hms_opt(6, 0, 0).unwrap(),
            "Asia/Shanghai".to_string(),
        );
        let window = NightWindow::from_config(&config).unwrap();
        assert!(window.crosses_midnight());
        assert_eq!(window.tz, Tz::Asia__Shanghai);
    }

    #[test]
    fn from_config_invalid_timezone() {
        let config = uc_types::NightWindowConfig::new(
            NaiveTime::from_hms_opt(22, 0, 0).unwrap(),
            NaiveTime::from_hms_opt(6, 0, 0).unwrap(),
            "Invalid/Timezone".to_string(),
        );
        let result = NightWindow::from_config(&config);
        assert!(result.is_err());
    }

    // ── Display test ──────────────────────────────────────────────

    #[test]
    fn display_format() {
        let window = make_window(22, 0, 6, 0, Tz::UTC);
        assert_eq!(window.to_string(), "NightWindow(22:00 - 06:00 UTC)");
    }

    // ── Edge case: midnight-to-midnight window ──────────────────────

    #[test]
    fn full_day_window() {
        // 00:00 to 00:00 means the entire day (same start and end)
        // In this case crosses_midnight is false, but the window covers
        // from midnight to just before midnight.
        let window = make_window(0, 0, 0, 0, Tz::UTC);
        assert!(!window.crosses_midnight());
        // With same start and end (both 00:00), the window is effectively empty
        // because the condition is start <= time < end and 00:00 < 00:00 is false.
        let midnight = Tz::UTC.with_ymd_and_hms(2024, 1, 15, 0, 0, 0).unwrap();
        assert!(!window.is_within_window(midnight));
    }

    #[test]
    fn midnight_to_midnight_cross() {
        // 00:00 to 23:59 — essentially all day (same-day)
        let window = make_window(0, 0, 23, 59, Tz::UTC);
        assert!(!window.crosses_midnight());
        let noon = Tz::UTC.with_ymd_and_hms(2024, 1, 15, 12, 0, 0).unwrap();
        assert!(window.is_within_window(noon));
    }
}
