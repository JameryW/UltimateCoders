//! `uc.scheduler.yaml` loader — declarative cron jobs + night window.
//!
//! Loaded by the gateway at startup (mirrors the `uc.repos.yaml` pattern).
//! The parsed tasks feed `SchedulerService::add_cron_job` /
//! `add_one_shot_job`; the night window feeds `set_night_window`.
//!
//! A job entry is either `cron: "..."` (recurring) or `execute_after: "..."
//! ` (one-shot). `night_window` is optional — when absent the default
//! `NightWindowConfig` (always-within) applies and jobs fire any time.

use chrono::{DateTime, NaiveTime, Utc};
use serde::Deserialize;
use std::path::Path;

use uc_types::NightWindowConfig;

/// Top-level `uc.scheduler.yaml` shape.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct SchedulerFileConfig {
    /// Optional night window applied to all jobs (overridable per-job).
    #[serde(default)]
    pub night_window: Option<NightWindowFile>,
    /// Declared jobs.
    #[serde(default)]
    pub jobs: Vec<JobFile>,
}

/// Night window declaration (HH:MM + IANA tz).
#[derive(Debug, Clone, Deserialize)]
pub struct NightWindowFile {
    /// Start, e.g. "22:00".
    pub start: String,
    /// End, e.g. "06:00".
    pub end: String,
    /// IANA timezone, e.g. "Asia/Shanghai". Defaults to "UTC".
    #[serde(default = "default_tz")]
    pub timezone: String,
}

fn default_tz() -> String {
    "UTC".to_string()
}

/// A declared job — either cron (recurring) or execute_after (one-shot).
#[derive(Debug, Clone, Deserialize)]
pub struct JobFile {
    /// Human-readable description (becomes the task description submitted on fire).
    pub description: String,
    /// Project/repository context.
    #[serde(default)]
    pub project_id: String,
    /// Cron expression for recurring jobs (e.g. "0 22 * * *"). Mutually
    /// exclusive with `execute_after`.
    pub cron: Option<String>,
    /// RFC-3339 timestamp for a one-shot delayed job. Mutually exclusive
    /// with `cron`.
    pub execute_after: Option<String>,
    /// Per-job night window override (defaults to the top-level window).
    #[serde(default)]
    pub night_window: Option<NightWindowFile>,
    /// Enabled flag (default true).
    #[serde(default = "default_true")]
    pub enabled: bool,
}

fn default_true() -> bool {
    true
}

/// A parsed job ready to hand to `SchedulerService`.
#[derive(Debug, Clone)]
pub struct ParsedJob {
    pub description: String,
    pub project_id: String,
    pub cron_expression: Option<String>,
    pub execute_after: Option<DateTime<Utc>>,
    /// Per-job night window. `None` when neither a per-job nor top-level
    /// window was declared — the job fires any time.
    pub night_window: Option<NightWindowConfig>,
    pub enabled: bool,
}

/// Parsed `uc.scheduler.yaml` — jobs + a default night window.
///
/// `default_night_window` is `None` when the user did not specify a top-level
/// `night_window` in the YAML — in that case jobs fire any time (no constraint).
/// When `Some`, it applies to all jobs that don't override it per-job.
#[derive(Debug, Clone, Default)]
pub struct ParsedSchedulerConfig {
    pub default_night_window: Option<NightWindowConfig>,
    pub jobs: Vec<ParsedJob>,
}

impl SchedulerFileConfig {
    /// Load + parse from a YAML file path.
    pub fn load(path: &Path) -> Result<Self, SchedulerConfigError> {
        let content =
            std::fs::read_to_string(path).map_err(|e| SchedulerConfigError::Io(e.to_string()))?;
        Self::parse(&content)
    }

    /// Parse from YAML text.
    pub fn parse(text: &str) -> Result<Self, SchedulerConfigError> {
        serde_yaml::from_str(text).map_err(|e| SchedulerConfigError::Parse(e.to_string()))
    }

    /// Resolve into typed config: parse HH:MM times + RFC-3339 timestamps,
    /// apply the default night window to jobs without their own.
    pub fn resolve(self) -> Result<ParsedSchedulerConfig, SchedulerConfigError> {
        // Only set a default night window if the user explicitly declared one
        // at the top level. When absent, default_night_window is None → jobs
        // fire any time (no constraint). This matches the PRD requirement:
        // "default-off (opt-in via config), no behavior change for existing deploys".
        let default_nw = self
            .night_window
            .as_ref()
            .map(parse_night_window)
            .transpose()?;

        let mut jobs = Vec::with_capacity(self.jobs.len());
        for j in self.jobs {
            if j.cron.is_none() && j.execute_after.is_none() {
                return Err(SchedulerConfigError::JobMissingSchedule(j.description));
            }
            if j.cron.is_some() && j.execute_after.is_some() {
                return Err(SchedulerConfigError::JobBothSchedules(j.description));
            }
            let execute_after = match j.execute_after {
                Some(s) => Some(
                    DateTime::parse_from_rfc3339(&s)
                        .map_err(|e| {
                            SchedulerConfigError::BadTimestamp(j.description.clone(), e.to_string())
                        })?
                        .with_timezone(&Utc),
                ),
                None => None,
            };
            // Per-job window overrides the default; if neither is set, use None
            // (fire any time). Only fall back to default_nw if it's Some.
            let nw = match j.night_window.as_ref() {
                Some(n) => Some(parse_night_window(n)?),
                None => default_nw.clone(),
            };
            jobs.push(ParsedJob {
                description: j.description,
                project_id: j.project_id,
                cron_expression: j.cron,
                execute_after,
                night_window: nw,
                enabled: j.enabled,
            });
        }

        Ok(ParsedSchedulerConfig {
            default_night_window: default_nw,
            jobs,
        })
    }
}

fn parse_night_window(n: &NightWindowFile) -> Result<NightWindowConfig, SchedulerConfigError> {
    let start =
        parse_hhmm(&n.start).ok_or_else(|| SchedulerConfigError::BadTime(n.start.clone()))?;
    let end = parse_hhmm(&n.end).ok_or_else(|| SchedulerConfigError::BadTime(n.end.clone()))?;
    Ok(NightWindowConfig::new(start, end, n.timezone.clone()))
}

fn parse_hhmm(s: &str) -> Option<NaiveTime> {
    NaiveTime::parse_from_str(s, "%H:%M").ok()
}

/// Errors loading `uc.scheduler.yaml`.
#[derive(Debug, thiserror::Error)]
pub enum SchedulerConfigError {
    #[error("scheduler config I/O: {0}")]
    Io(String),
    #[error("scheduler config parse: {0}")]
    Parse(String),
    #[error("job '{0}' has neither cron nor execute_after")]
    JobMissingSchedule(String),
    #[error("job '{0}' has both cron and execute_after")]
    JobBothSchedules(String),
    #[error("job '{0}' bad timestamp: {1}")]
    BadTimestamp(String, String),
    #[error("bad time '{0}' (expected HH:MM)")]
    BadTime(String),
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::Timelike;

    #[test]
    fn parse_minimal_cron_job() {
        let yaml = "jobs:\n  - description: nightly build\n    cron: \"0 22 * * *\"\n";
        let cfg = SchedulerFileConfig::parse(yaml).unwrap().resolve().unwrap();
        assert_eq!(cfg.jobs.len(), 1);
        assert_eq!(cfg.jobs[0].description, "nightly build");
        assert_eq!(cfg.jobs[0].cron_expression.as_deref(), Some("0 22 * * *"));
        assert!(cfg.jobs[0].execute_after.is_none());
        assert!(cfg.jobs[0].enabled);
    }

    #[test]
    fn parse_one_shot_job() {
        let yaml =
            "jobs:\n  - description: reminder\n    execute_after: \"2026-09-01T09:00:00Z\"\n";
        let cfg = SchedulerFileConfig::parse(yaml).unwrap().resolve().unwrap();
        assert_eq!(cfg.jobs.len(), 1);
        assert!(cfg.jobs[0].cron_expression.is_none());
        assert!(cfg.jobs[0].execute_after.is_some());
    }

    #[test]
    fn parse_night_window() {
        let yaml = "night_window:\n  start: \"22:00\"\n  end: \"06:00\"\n  timezone: \"Asia/Shanghai\"\njobs: []\n";
        let cfg = SchedulerFileConfig::parse(yaml).unwrap().resolve().unwrap();
        assert!(cfg.jobs.is_empty());
        let nw = cfg
            .default_night_window
            .expect("night window should be set");
        assert_eq!(nw.timezone, "Asia/Shanghai");
    }

    #[test]
    fn no_night_window_means_none() {
        // When no top-level night_window is specified, default_night_window
        // is None (jobs fire any time). This is the "opt-in" behavior.
        let yaml = "jobs:\n  - description: any-time\n    cron: \"0 12 * * *\"\n";
        let cfg = SchedulerFileConfig::parse(yaml).unwrap().resolve().unwrap();
        assert!(cfg.default_night_window.is_none());
        assert!(cfg.jobs[0].night_window.is_none());
    }

    #[test]
    fn job_without_schedule_is_error() {
        let yaml = "jobs:\n  - description: no schedule\n    project_id: p\n";
        let err = SchedulerFileConfig::parse(yaml)
            .unwrap()
            .resolve()
            .unwrap_err();
        assert!(matches!(err, SchedulerConfigError::JobMissingSchedule(_)));
    }

    #[test]
    fn job_with_both_schedules_is_error() {
        let yaml = "jobs:\n  - description: both\n    cron: \"0 22 * * *\"\n    execute_after: \"2026-09-01T09:00:00Z\"\n";
        let err = SchedulerFileConfig::parse(yaml)
            .unwrap()
            .resolve()
            .unwrap_err();
        assert!(matches!(err, SchedulerConfigError::JobBothSchedules(_)));
    }

    #[test]
    fn bad_time_is_error() {
        let yaml = "night_window:\n  start: \"25:00\"\n  end: \"06:00\"\njobs: []\n";
        let err = SchedulerFileConfig::parse(yaml)
            .unwrap()
            .resolve()
            .unwrap_err();
        assert!(matches!(err, SchedulerConfigError::BadTime(_)));
    }

    #[test]
    fn per_job_window_overrides_default() {
        let yaml = "night_window:\n  start: \"22:00\"\n  end: \"06:00\"\njobs:\n  - description: any-time\n    cron: \"0 12 * * *\"\n    night_window:\n      start: \"12:00\"\n      end: \"13:00\"\n";
        let cfg = SchedulerFileConfig::parse(yaml).unwrap().resolve().unwrap();
        // per-job window is 12:00-13:00, not the default 22:00-06:00
        let nw = cfg.jobs[0]
            .night_window
            .as_ref()
            .expect("per-job window set");
        assert_eq!(nw.start.hour(), 12);
    }

    #[test]
    fn job_inherits_top_level_window() {
        // When a top-level night_window is set and the job has no per-job
        // override, the job inherits the top-level window.
        let yaml = "night_window:\n  start: \"22:00\"\n  end: \"06:00\"\njobs:\n  - description: inherit\n    cron: \"0 23 * * *\"\n";
        let cfg = SchedulerFileConfig::parse(yaml).unwrap().resolve().unwrap();
        let nw = cfg.jobs[0].night_window.as_ref().expect("inherited window");
        assert_eq!(nw.start.hour(), 22);
    }
}
