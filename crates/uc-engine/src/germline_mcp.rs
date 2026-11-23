//! MCP germline data isolation (ledger item #98).
//!
//! A signed tool can still be malicious. Every invocation therefore forks a
//! transient copy from a data-free germline process and injects only the
//! payload labels authorised for that invocation's sink. The physical process
//! is torn down after execution.

use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};
use thiserror::Error;

pub const DEFAULT_POOL_SIZE: usize = 8;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum SinkKind {
    Filesystem,
    Network,
    Stdout,
    Database,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ToolInvocation {
    pub tool: String,
    pub sink: SinkKind,
    pub payload: BTreeMap<String, String>,
}

impl ToolInvocation {
    pub fn new(
        tool: impl Into<String>,
        sink: SinkKind,
        payload: BTreeMap<String, String>,
    ) -> Result<Self, GermlineError> {
        let tool = tool.into();
        if tool.trim().is_empty() {
            return Err(GermlineError::EmptyTool);
        }
        if payload.is_empty() {
            return Err(GermlineError::EmptyPayload);
        }
        Ok(Self {
            tool,
            sink,
            payload,
        })
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct GermlineExecution {
    pub fork_id: u64,
    pub tool: String,
    pub sink: SinkKind,
    pub delivered: BTreeMap<String, String>,
    pub denied_keys: Vec<String>,
    pub isolated: bool,
}

impl GermlineExecution {
    pub fn delivered_keys(&self) -> Vec<&str> {
        self.delivered.keys().map(String::as_str).collect()
    }
}

#[derive(Debug, Clone, Error, PartialEq, Eq)]
pub enum GermlineError {
    #[error("tool name must not be empty")]
    EmptyTool,
    #[error("payload must not be empty")]
    EmptyPayload,
    #[error("germline pool exhausted")]
    PoolExhausted,
    #[error("unknown fork id: {0}")]
    UnknownFork(u64),
    #[error("pool size must be positive")]
    InvalidPoolSize,
}

#[derive(Debug, Clone)]
pub struct GermlinePool {
    capacity: usize,
    next_id: u64,
    prewarmed: usize,
    active: BTreeSet<u64>,
}

impl Default for GermlinePool {
    fn default() -> Self {
        Self::new(DEFAULT_POOL_SIZE)
    }
}

impl GermlinePool {
    pub fn new(capacity: usize) -> Self {
        Self {
            capacity: capacity.max(1),
            next_id: 1,
            prewarmed: 0,
            active: BTreeSet::new(),
        }
    }

    pub fn capacity(&self) -> usize {
        self.capacity
    }

    pub fn prewarmed(&self) -> usize {
        self.prewarmed
    }

    pub fn active_count(&self) -> usize {
        self.active.len()
    }

    /// Warm idle germline processes. Returns how many were newly warmed.
    pub fn prewarm(&mut self, count: usize) -> usize {
        let available = self.capacity.saturating_sub(self.prewarmed);
        let warmed = count.min(available);
        self.prewarmed += warmed;
        warmed
    }

    /// Fork a transient copy, inject only `allowed_labels`, and return the
    /// filtered execution plan. The caller must call `release` when done.
    pub fn execute(
        &mut self,
        invocation: &ToolInvocation,
        allowed_labels: &[String],
    ) -> Result<GermlineExecution, GermlineError> {
        if self.active.len() >= self.capacity {
            return Err(GermlineError::PoolExhausted);
        }
        let allowed: BTreeSet<&str> = allowed_labels.iter().map(String::as_str).collect();
        let mut delivered = BTreeMap::new();
        let mut denied_keys = Vec::new();
        for (key, value) in &invocation.payload {
            if allowed.contains(key.as_str()) {
                delivered.insert(key.clone(), value.clone());
            } else {
                denied_keys.push(key.clone());
            }
        }
        let fork_id = self.next_id;
        self.next_id += 1;
        self.active.insert(fork_id);
        if self.prewarmed > 0 {
            self.prewarmed -= 1;
        }
        Ok(GermlineExecution {
            fork_id,
            tool: invocation.tool.clone(),
            sink: invocation.sink,
            delivered,
            denied_keys,
            isolated: true,
        })
    }

    pub fn release(&mut self, fork_id: u64) -> bool {
        self.active.remove(&fork_id)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn payload(entries: &[(&str, &str)]) -> BTreeMap<String, String> {
        entries
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect()
    }

    #[test]
    fn prewarm_is_bounded_by_capacity() {
        let mut pool = GermlinePool::new(2);
        assert_eq!(pool.prewarm(5), 2);
        assert_eq!(pool.prewarmed(), 2);
    }

    #[test]
    fn execute_filters_unauthorised_labels() {
        let mut pool = GermlinePool::default();
        let invocation = ToolInvocation::new(
            "fs.read",
            SinkKind::Filesystem,
            payload(&[("path", "/tmp/a"), ("secret", "nope")]),
        )
        .unwrap();
        let execution = pool.execute(&invocation, &["path".to_string()]).unwrap();
        assert_eq!(execution.delivered_keys(), vec!["path"]);
        assert_eq!(execution.denied_keys, vec!["secret"]);
        assert!(execution.isolated);
    }

    #[test]
    fn every_execution_has_a_distinct_fork() {
        let mut pool = GermlinePool::default();
        let invocation = ToolInvocation::new(
            "net.get",
            SinkKind::Network,
            payload(&[("url", "https://example.test")]),
        )
        .unwrap();
        let first = pool.execute(&invocation, &["url".to_string()]).unwrap();
        let second = pool.execute(&invocation, &["url".to_string()]).unwrap();
        assert_ne!(first.fork_id, second.fork_id);
        assert_eq!(pool.active_count(), 2);
    }

    #[test]
    fn pool_exhaustion_is_reported() {
        let mut pool = GermlinePool::new(1);
        let invocation = ToolInvocation::new(
            "fs.write",
            SinkKind::Filesystem,
            payload(&[("path", "/tmp/a")]),
        )
        .unwrap();
        let _first = pool.execute(&invocation, &["path".to_string()]).unwrap();
        assert_eq!(
            pool.execute(&invocation, &["path".to_string()]),
            Err(GermlineError::PoolExhausted)
        );
    }

    #[test]
    fn release_reclaims_a_fork() {
        let mut pool = GermlinePool::default();
        let invocation = ToolInvocation::new(
            "fs.write",
            SinkKind::Filesystem,
            payload(&[("path", "/tmp/a")]),
        )
        .unwrap();
        let execution = pool.execute(&invocation, &["path".to_string()]).unwrap();
        assert!(pool.release(execution.fork_id));
        assert_eq!(pool.active_count(), 0);
        assert!(!pool.release(999));
    }

    #[test]
    fn empty_tool_is_rejected() {
        assert_eq!(
            ToolInvocation::new(" ", SinkKind::Stdout, payload(&[("a", "b")])),
            Err(GermlineError::EmptyTool)
        );
    }

    #[test]
    fn empty_payload_is_rejected() {
        assert_eq!(
            ToolInvocation::new("fs.read", SinkKind::Filesystem, BTreeMap::new()),
            Err(GermlineError::EmptyPayload)
        );
    }

    #[test]
    fn sink_is_preserved() {
        let mut pool = GermlinePool::default();
        let invocation = ToolInvocation::new(
            "db.query",
            SinkKind::Database,
            payload(&[("sql", "select 1")]),
        )
        .unwrap();
        let execution = pool.execute(&invocation, &["sql".to_string()]).unwrap();
        assert_eq!(execution.sink, SinkKind::Database);
    }

    #[test]
    fn prewarmed_process_is_consumed() {
        let mut pool = GermlinePool::new(2);
        assert_eq!(pool.prewarm(1), 1);
        let invocation = ToolInvocation::new(
            "net.get",
            SinkKind::Network,
            payload(&[("url", "https://example.test")]),
        )
        .unwrap();
        let _ = pool.execute(&invocation, &["url".to_string()]).unwrap();
        assert_eq!(pool.prewarmed(), 0);
    }
}
