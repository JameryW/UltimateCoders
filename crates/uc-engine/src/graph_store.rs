//! Graph-state row tables + one-time backfillers (T2, tracker #638).
//!
//!规约 the "whole-Task JSONB blob" into relational graph tables so T3's
//! state machine has a schema and a data substrate. **Behavior is unchanged**:
//! the gateway's in-memory HashMap stays the runtime authority. This module
//! only owns (a) the table schema, (b) the startup backfillers, (c) the
//! shadow write/read paths gated by `UC_GRAPH_SHADOW`.
//!
//! # Tables
//!
//! | Table | Key | Purpose |
//! |---|---|---|
//! | `execution_graphs` | `graph_id` PK (= task id) | graph row: status, version, `root_scope` reserved, `imported` marker |
//! | `graph_nodes` | (`node_id`, `graph_id`) composite PK | node row: type/state, `dependencies` JSONB, `effect_class` default `'requires_worker'` |
//! | `task_attempts` | `attempt_id` PK, `UNIQUE(node_id, retry_no)` | attempt rows incl. `worker_epoch` (T1 envelope field) |
//! | `execution_events` | `seq BIGSERIAL` | event rows; **reserved** `cost` / `tokens` / `duration_ms` columns |
//! | `node_completions` | `node_id` PK (the commit-once unique constraint) | schema only in T2 — the commit-once *logic* is T3 |
//!
//! # Migrations
//!
//! Strictly the `scheduler/migration.rs` template: hold the application-wide
//! schema-migrations advisory lock with `hold_schema_migrations_lock(pool,
//! "graph")` (#631 — a dedicated connection, session-scoped lock, 30s bounded
//! wait), then `CREATE TABLE IF NOT EXISTS` + an index loop. A non-storage
//! `run_migrations()` stub keeps the call shape for builds without the
//! `storage` feature.
//!
//! # Environment
//!
//! - `UC_DATABASE_URL` — the graph store connects to the **same database as
//!   the task backend**. NOTE the existing split: `metadata` uses
//!   `UC_PG_URL` while the task/schedule backends use `UC_DATABASE_URL`;
//!   unifying them is tracked separately (P1), not here.
//! - `UC_GRAPH_IMPORT_DIR` — path to a `.uc` directory (containing `tasks/`
//!   and optionally `checkpoints/`). Source-B import runs **only** when this
//!   is explicitly set AND the graph is not already present in
//!   `execution_graphs` (D2 constraint 1: never bidirectional — PG is the
//!   source of truth once populated).
//! - `UC_GRAPH_SHADOW` — `"on"` (exact value) enables the shadow write beside
//!   `persist_task` and the startup shadow-read diff. Default: off. Neither
//!   ever changes a read path — diffs are warn-only.
//!
//! # Status mapping (import + shadow, single source of truth below)
//!
//! Node states (`graph_nodes.state`):
//!
//! | input (Rust Debug / TS lowercase / camelCase) | state |
//! |---|---|
//! | `Pending` / `pending` | `READY` |
//! | `Assigned` / `InProgress` / `in_progress` / `running` / `reviewing` | `RUNNING` |
//! | `Completed` / `completed` | `SUCCEEDED` |
//! | `Failed` / `failed` | `FAILED` |
//! | `Cancelled` / `cancelled` | `CANCELLED` |
//! | `Conflicted` / `conflicted` | `FAILED` |
//! | `Paused` / `paused` | `PAUSED` |
//!
//! Graph states (`execution_graphs.status`) additionally map
//! `Created → CREATED` and `Planning → PLANNING`.
//!
//! Per the mapping, a `Completed` node also gets an attempt row
//! (`SUCCEEDED`) **and** a `node_completions` row — written in the same
//! transaction as the node row. An `InProgress`/`Assigned` node gets an
//! attempt row carrying `started_at`. Imported graphs start at `version = 1`
//! with `imported = TRUE` (version is only bumped by T3's CAS; this ticket
//! never writes another value). Attempt ids are deterministic
//! (`<graph_id>:<node_id>:<retry_no>`), which is what makes the importers
//! idempotent (`ON CONFLICT DO NOTHING`) and byte-stable across runs.
//!
//! # T3 (#639): real transition logic on the graph plane
//!
//! [`GraphStore`] now owns the attempt-lifecycle verbs — each is ONE
//! transaction doing version-CAS node updates (`UPDATE … WHERE version = $n`,
//! guarded by [`uc_types::can_transition`]), an append of `execution_events`
//! rows (event_type + graph_version; the reserved cost/tokens columns stay
//! NULL), and `node_completions` commit-once via `INSERT … ON CONFLICT
//! DO NOTHING` rows-affected. Fencing rides the per-node `worker_epoch`
//! (each new attempt carries `max(epoch)+1`): a late commit/fail for an
//! attempt that is no longer `RUNNING` never changes node state and only
//! appends a `late_result` event.
//!
//! The legacy HashMap reaper is **untouched** (dual-plane ruling, research
//! §3): `uc-grpc` fans the same moments out through the new
//! [`GraphShadowSink`] verbs (`on_schedule` / `on_heartbeat` / `on_commit` /
//! `on_fail`, all default no-ops so the always-compiled surface — and every
//! T2 fake — keeps compiling). The sink plane grows the *correct* logic now;
//! T6 deletes the legacy side.

use serde::Deserialize;
use uc_types::{can_transition, ExecutionEnvelope, NodeStatus, Subtask, Task, TaskStatus};

// ── Status token mapping (pure, always compiled) ─────────────────────

/// Normalize a status string from any legacy vocabulary (Rust `Debug`
/// PascalCase, TS camelCase, TS snake_case) into lowercase snake.
pub fn normalize_status_token(raw: &str) -> String {
    let mut out = String::with_capacity(raw.len() + 4);
    for ch in raw.chars() {
        if ch.is_ascii_uppercase() {
            if !out.is_empty() {
                out.push('_');
            }
            out.push(ch.to_ascii_lowercase());
        } else if ch == '-' || ch == ' ' {
            out.push('_');
        } else {
            out.push(ch.to_ascii_lowercase());
        }
    }
    out
}

/// Map any task/subtask-ish status string to a graph-node state token.
/// Unknown inputs pass through as uppercase (documented, never dropped —
/// the diff/import must not silently invent `READY`).
pub fn node_state_token(raw: &str) -> String {
    match normalize_status_token(raw).as_str() {
        "pending" | "ready" => "READY".to_string(),
        "assigned" | "in_progress" | "running" | "reviewing" => "RUNNING".to_string(),
        "completed" | "succeeded" => "SUCCEEDED".to_string(),
        "failed" => "FAILED".to_string(),
        "conflicted" => "FAILED".to_string(),
        "cancelled" | "canceled" => "CANCELLED".to_string(),
        "paused" => "PAUSED".to_string(),
        other => other.to_ascii_uppercase(),
    }
}

/// Map any task-level status string to an `execution_graphs.status` token.
pub fn graph_status_token(raw: &str) -> String {
    match normalize_status_token(raw).as_str() {
        "created" => "CREATED".to_string(),
        "planning" => "PLANNING".to_string(),
        other => node_state_token(other),
    }
}

/// Task-level `TaskStatus` → token (via Debug so the enum stays the single
/// definition — no parallel match table to drift).
pub fn graph_status_of_task_status(status: &TaskStatus) -> String {
    graph_status_token(&format!("{status:?}"))
}

fn node_status_of_subtask(subtask: &Subtask) -> String {
    node_state_token(&format!("{:?}", subtask.status))
}

/// Whether a `from → to` move between raw state tokens is legal per the
/// 9-state machine in `uc_types`. Unknown tokens (the T2 passthrough
/// vocabulary — `PAUSED`, `PLANNING`, anything unrecognized) never allow a
/// transition: the graph plane refuses to move rows it does not understand.
pub fn transition_ok(from: &str, to: &str) -> bool {
    match (NodeStatus::from_token(from), NodeStatus::from_token(to)) {
        (Some(f), Some(t)) => can_transition(f, t),
        _ => false,
    }
}

// ── Projection shapes (pure) ─────────────────────────────────────────

/// One deterministic attempt id per (graph, node, retry) — the id formula is
/// the idempotency key for `task_attempts` inserts (plus the schema's
/// `UNIQUE(node_id, retry_no)`).
pub fn attempt_id(graph_id: &str, node_id: &str, retry_no: i32) -> String {
    format!("{graph_id}:{node_id}:{retry_no}")
}

#[derive(Debug, Clone)]
pub struct NodeRow {
    pub node_id: String,
    pub state: String,
    pub dependencies: serde_json::Value,
    pub required_capabilities: serde_json::Value,
    /// D4 #633 Q2: governs local-execution eligibility on transport loss.
    /// Legacy projections default to 'requires_worker'.
    pub effect_class: String,
}

#[derive(Debug, Clone)]
pub struct AttemptRow {
    pub attempt_id: String,
    pub node_id: String,
    pub worker_id: Option<String>,
    pub status: String,
    pub retry_no: i32,
    pub started_at: Option<chrono::DateTime<chrono::Utc>>,
    pub finished_at: Option<chrono::DateTime<chrono::Utc>>,
    pub result_ref: Option<String>,
}

#[derive(Debug, Clone)]
pub struct CompletionRow {
    pub node_id: String,
    pub winning_attempt_id: String,
    pub result_ref: Option<String>,
    pub committed_at: Option<chrono::DateTime<chrono::Utc>>,
}

/// A Task projected into the five-table shape. Produced identically from the
/// Rust `Task` (live HashMap / PG `tasks` rows) and from the TS
/// `PersistedTask` JSON — that convergence is what makes the import
/// deterministic and cross-backend comparable.
#[derive(Debug, Clone)]
pub struct GraphProjection {
    pub graph_id: String,
    pub project_id: String,
    pub status: String,
    pub created_at: chrono::DateTime<chrono::Utc>,
    pub updated_at: chrono::DateTime<chrono::Utc>,
    pub nodes: Vec<NodeRow>,
    pub attempts: Vec<AttemptRow>,
    pub completions: Vec<CompletionRow>,
}

fn ms_to_ts(ms: Option<i64>) -> Option<chrono::DateTime<chrono::Utc>> {
    ms.and_then(chrono::DateTime::from_timestamp_millis)
}

fn attempt_and_completion(
    graph_id: &str,
    node_id: &str,
    state: &str,
    retry_count: u32,
    worker_id: Option<String>,
    started_at: Option<chrono::DateTime<chrono::Utc>>,
    finished_at: Option<chrono::DateTime<chrono::Utc>>,
) -> (Option<AttemptRow>, Option<CompletionRow>) {
    // READY (pending) nodes have never been attempted — no rows.
    if state == "READY" {
        return (None, None);
    }
    let retry_no = retry_count.min(i32::MAX as u32) as i32;
    let id = attempt_id(graph_id, node_id, retry_no);
    let result_ref = format!("{graph_id}/{node_id}");
    let attempt = AttemptRow {
        attempt_id: id.clone(),
        node_id: node_id.to_string(),
        worker_id,
        status: state.to_string(),
        retry_no,
        started_at,
        finished_at,
        result_ref: Some(result_ref.clone()),
    };
    // Completed→SUCCEEDED additionally commits a node_completions row in the
    // same transaction (the "same transaction" clause of the T2 mapping).
    let completion = (state == "SUCCEEDED").then(|| CompletionRow {
        node_id: node_id.to_string(),
        winning_attempt_id: id,
        result_ref: Some(result_ref),
        committed_at: finished_at,
    });
    (Some(attempt), completion)
}

/// Project a live Rust `Task` (gateway HashMap shape / PG `tasks` rows
/// after serde).
pub fn project_task(task: &Task) -> GraphProjection {
    let graph_id = task.id.0.clone();
    let mut nodes = Vec::with_capacity(task.subtasks.len());
    let mut attempts = Vec::new();
    let mut completions = Vec::new();
    for st in &task.subtasks {
        let node_id = st.id.0.clone();
        let state = node_status_of_subtask(st);
        nodes.push(NodeRow {
            node_id: node_id.clone(),
            state: state.clone(),
            dependencies: serde_json::Value::Array(
                st.depends_on
                    .iter()
                    .map(|d| serde_json::Value::String(d.0.clone()))
                    .collect(),
            ),
            required_capabilities: serde_json::json!(st.required_capabilities),
            effect_class: st.effect_class.as_str().to_string(),
        });
        let worker_id = st
            .result
            .as_ref()
            .map(|r| r.worker_id.0.clone())
            .or_else(|| st.assigned_worker.as_ref().map(|w| w.0.clone()));
        let (started, finished) = match &st.result {
            // InProgress/Assigned→RUNNING attempt rows carry started_at; with
            // no per-subtask timestamp on the Rust shape, the task's last
            // update is the honest lower bound.
            Some(r) => (Some(r.completed_at), Some(r.completed_at)),
            None if state == "RUNNING" => (Some(task.updated_at), None),
            None => (None, None),
        };
        let (attempt, completion) = attempt_and_completion(
            &graph_id,
            &node_id,
            &state,
            st.retry_count,
            worker_id,
            started,
            finished,
        );
        attempts.extend(attempt);
        completions.extend(completion);
    }
    GraphProjection {
        graph_id,
        project_id: task.project_id.clone(),
        status: graph_status_of_task_status(&task.status),
        created_at: task.created_at,
        updated_at: task.updated_at,
        nodes,
        attempts,
        completions,
    }
}

// ── TS PersistedTask (`.uc/tasks/*.json` + `.uc/checkpoints/*.snap.json`) ──

/// Field aliases: the TS files are camelCase (`rename_all`), and unknown keys
/// (description, controlState, review, …) are ignored — the importer only
/// needs the columns the graph tables actually carry. Source A's Rust-
/// serde-shaped JSONB is parsed into the typed `Subtask` directly, not here.
#[derive(Debug, Clone, Deserialize, Default)]
#[serde(rename_all = "camelCase")]
pub struct TsPersistedTask {
    #[serde(default)]
    pub id: String,
    #[serde(default)]
    pub status: String,
    #[serde(default)]
    pub project_id: Option<String>,
    #[serde(default)]
    pub saved_at: Option<i64>,
    #[serde(default)]
    pub created_at: Option<i64>,
    #[serde(default)]
    pub subtasks: Vec<TsSubtask>,
}

#[derive(Debug, Clone, Deserialize, Default)]
#[serde(rename_all = "camelCase")]
pub struct TsSubtask {
    #[serde(default)]
    pub id: String,
    #[serde(default)]
    pub status: String,
    #[serde(default)]
    pub depends_on: Vec<String>,
    #[serde(default)]
    pub started_at: Option<i64>,
    #[serde(default)]
    pub completed_at: Option<i64>,
    #[serde(default)]
    pub retry_count: Option<u32>,
    #[serde(default)]
    pub required_capabilities: Option<Vec<String>>,
    /// D4 #633 Q2 — additive; absent in legacy TS snapshots.
    #[serde(default)]
    pub effect_class: Option<String>,
}

/// Project a TS `PersistedTask` (camelCase) into the row shape.
pub fn project_ts_task(task: &TsPersistedTask) -> GraphProjection {
    let graph_id = task.id.clone();
    let created = ms_to_ts(task.created_at)
        .or_else(|| ms_to_ts(task.saved_at))
        .unwrap_or_else(|| chrono::DateTime::from_timestamp(0, 0).expect("epoch"));
    let updated = ms_to_ts(task.saved_at).unwrap_or(created);
    let mut nodes = Vec::with_capacity(task.subtasks.len());
    let mut attempts = Vec::new();
    let mut completions = Vec::new();
    for st in &task.subtasks {
        let node_id = st.id.clone();
        let state = node_state_token(&st.status);
        nodes.push(NodeRow {
            node_id: node_id.clone(),
            state: state.clone(),
            dependencies: serde_json::Value::Array(
                st.depends_on
                    .iter()
                    .map(|d| serde_json::Value::String(d.clone()))
                    .collect(),
            ),
            required_capabilities: serde_json::json!(st
                .required_capabilities
                .clone()
                .unwrap_or_default()),
            // TS planner does not emit effect_class yet (additive field);
            // legacy projections stay 'requires_worker'.
            effect_class: st
                .effect_class
                .clone()
                .unwrap_or_else(|| "requires_worker".to_string()),
        });
        let (attempt, completion) = attempt_and_completion(
            &graph_id,
            &node_id,
            &state,
            st.retry_count.unwrap_or(0),
            None,
            ms_to_ts(st.started_at),
            ms_to_ts(st.completed_at),
        );
        attempts.extend(attempt);
        completions.extend(completion);
    }
    GraphProjection {
        graph_id,
        project_id: task.project_id.clone().unwrap_or_default(),
        status: graph_status_token(&task.status),
        created_at: created,
        updated_at: updated,
        nodes,
        attempts,
        completions,
    }
}

/// savedAt newer-wins rule (mirrors `TaskStore.restore` F46 in TS): when a
/// checkpoint exists, its content replaces the task file's **only** if its
/// `savedAt` is newer; a missing `savedAt` counts as 0 (legacy file).
pub fn pick_newer_saved(
    task_file: TsPersistedTask,
    checkpoint: Option<TsPersistedTask>,
) -> TsPersistedTask {
    match checkpoint {
        Some(cp) if cp.saved_at.unwrap_or(0) > task_file.saved_at.unwrap_or(0) => cp,
        _ => task_file,
    }
}

// ── Always-compiled shadow sink surface ──────────────────────────────

/// Object-safe sink the gateway calls beside `persist_task` when graph
/// shadow mode is on. Defined (and consumed) without the `storage` feature
/// so `uc-grpc` — which compiles `uc-engine` with `default-features =
/// false` — can hold an `Option<Arc<dyn GraphShadowSink>>` with **no**
/// feature of its own. Implementations must be warn-only internally:
/// the shadow path can never change primary-path behavior.
///
/// T3 (#639) adds the attempt-lifecycle verbs. New parameters ride the T1
/// [`ExecutionEnvelope`] identity (`graph_id = task_id`, `node_id =
/// subtask_id`, `attempt_id = dispatch_retry_count`).
///
/// One attempt reaped by the graph-plane timeout sweep ([`GraphStore::
/// timeout_sweep`], surfaced to the monitor through the [`GraphShadowSink::
/// sweep_timeouts`] verb). Fenced no-ops (unknown / already-terminal attempt,
/// node already committed) are excluded by the sweep itself — every returned
/// attempt actually moved.
#[derive(Debug, Clone)]
pub struct SweptAttempt {
    /// Graph (task) the swept attempt belonged to (`graph_id == task_id`).
    pub graph_id: String,
    /// Node (subtask) the swept attempt belonged to (`node_id == subtask_id`).
    pub node_id: String,
    /// The swept (now FAILED) attempt id.
    pub attempt_id: String,
    /// `true` = the node was re-armed to READY (retry budget left);
    /// `false` = the budget was exhausted and the node itself FAILED.
    pub rearmed: bool,
}

/// Subtask-plane write verbs fanned out fire-and-forget from the legacy
/// mutation waypoints (T3 #639), plus the graph-plane reaper (T6 #642).
///
/// The envelope is the transitional identity mapping (`graph_id =
/// subtask_id`, `attempt_id = dispatch_retry_count`), so the always-
/// compiled surface carries no storage types. All verbs have **no-op
/// default implementations** — a shadow-only sink (and every T2 fake)
/// keeps compiling untouched; the real-graph verbs are opt-in per
/// implementation. Callers fan these out fire-and-forget: a verb can never
/// fail the legacy path.
#[async_trait::async_trait]
pub trait GraphShadowSink: Send + Sync {
    /// Upsert the task's graph/node rows (shadow copy; never authoritative).
    async fn shadow_persist(&self, task: &Task);

    /// A node was dispatched (legacy `Assigned` marking at the publish /
    /// dispatch waypoints). Graph plane: `READY → SCHEDULED → RUNNING` plus
    /// a fresh attempt row. `worker_id` is `None` at publish time (queue
    /// group dispatch — the picking worker is not yet known).
    async fn on_schedule(&self, _envelope: &ExecutionEnvelope, _worker_id: Option<&str>) {}

    /// The worker driving this node's attempt is alive (legacy update
    /// surfaced `InProgress`). Graph plane: refresh `heartbeat_at` on the
    /// attempt — the datum `timeout_sweep` judges staleness by.
    async fn on_heartbeat(&self, _envelope: &ExecutionEnvelope) {}

    /// A node reached a terminal-success derivation (legacy `Completed`).
    /// Graph plane: commit-once into `node_completions`; only the winner
    /// moves the node to `SUCCEEDED` and recomputes downstream `READY`.
    async fn on_commit(&self, _envelope: &ExecutionEnvelope, _result_ref: Option<&str>) {}

    /// A node reached a terminal-failure derivation (legacy `Failed` /
    /// `Conflicted` on the NATS update path). Graph plane: attempt `FAILED`
    /// + fence; node back to `READY` while the retry budget lasts, else
    /// `FAILED`. `reason` is a free-form diagnostic carried into the event
    /// payload.
    async fn on_fail(&self, _envelope: &ExecutionEnvelope, _reason: &str) {}

    /// Graph-plane reaper (T6 #642): sweep every `RUNNING` attempt whose
    /// heartbeat (`heartbeat_at`, falling back to `started_at`) is older
    /// than `heartbeat_timeout` through the fail path — attempt `FAILED` +
    /// fence, node re-armed to `READY` while the retry budget lasts, else
    /// the node itself `FAILED`. Replaces both legacy TaskStore reassign
    /// reapers (dead-worker AND Assigned-never-picked-up: the attempt row
    /// is created `RUNNING` at schedule time, so `started_at` ages past
    /// the window in either case). The heartbeat monitor bridges each
    /// swept attempt back into the legacy store and re-dispatches. No-op
    /// default (shadow-only sinks and test fakes sweep nothing).
    async fn sweep_timeouts(
        &self,
        _heartbeat_timeout: std::time::Duration,
    ) -> Vec<SweptAttempt> {
        Vec::new()
    }
}

// ── Migrations (storage) ─────────────────────────────────────────────

#[cfg(feature = "storage")]
use sqlx::postgres::PgPool;
#[cfg(feature = "storage")]
use std::sync::Arc;
#[cfg(feature = "storage")]
use uc_types::EngineError;

/// Run graph-table migrations.
///
/// Strictly the `scheduler/migration.rs` template: the advisory lock is held
/// for the rest of the function (the guard owns a dedicated connection, so
/// every `?` path releases it), DDL is `IF NOT EXISTS`, indexes run in a
/// loop.
#[cfg(feature = "storage")]
pub async fn run_migrations(pool: &Arc<PgPool>) -> Result<(), EngineError> {
    // The guard is held for the rest of this function; any `?` drop path ends
    // the locking session and releases the advisory lock with it (#631).
    let _migrations = crate::migration_lock::hold_schema_migrations_lock(pool, "graph").await?;

    sqlx::query(
        r#"
        CREATE TABLE IF NOT EXISTS execution_graphs (
            graph_id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            version BIGINT NOT NULL DEFAULT 1,
            root_scope TEXT NOT NULL DEFAULT '',
            imported BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        "#,
    )
    .execute(pool.as_ref())
    .await
    .map_err(|e| {
        EngineError::ConnectionError(format!("Migration error (execution_graphs): {}", e))
    })?;

    sqlx::query(
        r#"
        CREATE TABLE IF NOT EXISTS graph_nodes (
            graph_id TEXT NOT NULL REFERENCES execution_graphs(graph_id) ON DELETE CASCADE,
            node_id TEXT NOT NULL,
            type TEXT NOT NULL DEFAULT 'subtask',
            state TEXT NOT NULL,
            dependencies JSONB NOT NULL DEFAULT '[]',
            dependency_policy TEXT NOT NULL DEFAULT 'finish_to_start',
            scope_id TEXT NOT NULL DEFAULT '',
            input_refs JSONB NOT NULL DEFAULT '[]',
            output_refs JSONB NOT NULL DEFAULT '[]',
            priority INTEGER NOT NULL DEFAULT 0,
            deadline TIMESTAMPTZ,
            optional BOOLEAN NOT NULL DEFAULT FALSE,
            effect_class TEXT NOT NULL DEFAULT 'requires_worker',
            required_capabilities JSONB NOT NULL DEFAULT '[]',
            version BIGINT NOT NULL DEFAULT 1,
            PRIMARY KEY (node_id, graph_id)
        )
        "#,
    )
    .execute(pool.as_ref())
    .await
    .map_err(|e| EngineError::ConnectionError(format!("Migration error (graph_nodes): {}", e)))?;

    sqlx::query(
        r#"
        CREATE TABLE IF NOT EXISTS task_attempts (
            attempt_id TEXT PRIMARY KEY,
            graph_id TEXT NOT NULL,
            node_id TEXT NOT NULL,
            worker_id TEXT,
            worker_epoch BIGINT,
            status TEXT NOT NULL,
            retry_no INTEGER NOT NULL DEFAULT 0,
            started_at TIMESTAMPTZ,
            heartbeat_at TIMESTAMPTZ,
            finished_at TIMESTAMPTZ,
            result_ref TEXT,
            UNIQUE (node_id, retry_no),
            FOREIGN KEY (node_id, graph_id) REFERENCES graph_nodes (node_id, graph_id) ON DELETE CASCADE
        )
        "#,
    )
    .execute(pool.as_ref())
    .await
    .map_err(|e| {
        EngineError::ConnectionError(format!("Migration error (task_attempts): {}", e))
    })?;

    // execution_events: `cost` / `tokens` / `duration_ms` are the reserved
    // billing columns that close the assessment's leftover-risk. No writer
    // exists in T2 (T3/T5 emit events); the schema lands now so T3 never
    // needs a widening migration.
    sqlx::query(
        r#"
        CREATE TABLE IF NOT EXISTS execution_events (
            seq BIGSERIAL PRIMARY KEY,
            graph_id TEXT NOT NULL,
            node_id TEXT,
            attempt_id TEXT,
            graph_version BIGINT,
            event_type TEXT NOT NULL,
            payload JSONB NOT NULL DEFAULT '{}',
            cost NUMERIC(18, 6),
            tokens BIGINT,
            duration_ms BIGINT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        "#,
    )
    .execute(pool.as_ref())
    .await
    .map_err(|e| {
        EngineError::ConnectionError(format!("Migration error (execution_events): {}", e))
    })?;

    // node_completions: schema + unique constraint ONLY. `node_id` as PK is
    // the commit-once guarantee; the commit logic itself is T3.
    sqlx::query(
        r#"
        CREATE TABLE IF NOT EXISTS node_completions (
            node_id TEXT PRIMARY KEY,
            graph_id TEXT NOT NULL,
            winning_attempt_id TEXT NOT NULL,
            result_ref TEXT,
            committed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            FOREIGN KEY (node_id, graph_id) REFERENCES graph_nodes (node_id, graph_id) ON DELETE CASCADE
        )
        "#,
    )
    .execute(pool.as_ref())
    .await
    .map_err(|e| {
        EngineError::ConnectionError(format!("Migration error (node_completions): {}", e))
    })?;

    let indexes = [
        "CREATE INDEX IF NOT EXISTS idx_execution_graphs_project ON execution_graphs(project_id)",
        "CREATE INDEX IF NOT EXISTS idx_execution_graphs_created ON execution_graphs(created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_execution_graphs_status ON execution_graphs(status)",
        "CREATE INDEX IF NOT EXISTS idx_graph_nodes_graph ON graph_nodes(graph_id)",
        "CREATE INDEX IF NOT EXISTS idx_graph_nodes_state ON graph_nodes(state)",
        "CREATE INDEX IF NOT EXISTS idx_task_attempts_graph ON task_attempts(graph_id)",
        "CREATE INDEX IF NOT EXISTS idx_task_attempts_status ON task_attempts(status)",
        "CREATE INDEX IF NOT EXISTS idx_execution_events_graph_seq ON execution_events(graph_id, seq)",
        "CREATE INDEX IF NOT EXISTS idx_execution_events_type ON execution_events(event_type)",
    ];

    for idx_sql in &indexes {
        sqlx::query(idx_sql)
            .execute(pool.as_ref())
            .await
            .map_err(|e| EngineError::ConnectionError(format!("Index creation error: {}", e)))?;
    }

    tracing::info!("Graph-table database migrations completed");
    Ok(())
}

/// Run graph migrations (no-op when storage feature is disabled).
#[cfg(not(feature = "storage"))]
pub async fn run_migrations() -> Result<(), uc_types::EngineError> {
    Ok(())
}

// ── GraphStore (storage) ─────────────────────────────────────────────

/// Row counts touched by a backfill/import run (`inserted` counts the rows
/// this run actually wrote — `ON CONFLICT DO NOTHING` means re-runs report
/// zeros).
#[cfg(feature = "storage")]
#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
pub struct BackfillStats {
    pub graphs: u64,
    pub nodes: u64,
    pub attempts: u64,
    pub completions: u64,
    pub skipped_existing: u64,
}

/// PostgreSQL-backed graph store.
///
/// `connect` follows `PostgresScheduleStore::connect`: a dedicated pool and
/// `Err` on an unreachable database (the startup assembly point warns +
/// degrades; there is no meaningful in-memory stand-in for row tables).
/// `is_connected()` is the live-DB guard every integration test must assert
/// so a dead Postgres can never produce a false green.
#[cfg(feature = "storage")]
pub struct GraphStore {
    pool: Arc<PgPool>,
}

#[cfg(feature = "storage")]
impl GraphStore {
    /// Connect to PostgreSQL, run the graph migrations, and return the store.
    pub async fn connect(database_url: &str) -> Result<Self, EngineError> {
        let pool = sqlx::postgres::PgPoolOptions::new()
            .max_connections(5)
            .acquire_timeout(std::time::Duration::from_secs(10))
            .connect(database_url)
            .await
            .map_err(|e| {
                EngineError::ConnectionError(format!(
                    "Failed to connect to PostgreSQL for graph store: {}",
                    e
                ))
            })?;
        let pool = Arc::new(pool);
        run_migrations(&pool).await?;
        tracing::info!("Connected to PostgreSQL for graph-state tables");
        Ok(Self { pool })
    }

    /// Wrap an existing pool (tests / DI). Does not run migrations.
    pub fn with_pool(pool: Arc<PgPool>) -> Self {
        Self { pool }
    }

    pub fn pool(&self) -> &Arc<PgPool> {
        &self.pool
    }

    /// Ping the database. False green guard for integration tests.
    pub async fn is_connected(&self) -> bool {
        sqlx::query("SELECT 1")
            .execute(self.pool.as_ref())
            .await
            .is_ok()
    }

    /// Begin one transition transaction (T3 verbs). The `what` label only
    /// serves the error message — a dropped (un-committed) transaction is
    /// the rollback path every `?` early-return relies on.
    async fn begin_tx(
        &self,
        what: &str,
    ) -> Result<sqlx::Transaction<'_, sqlx::Postgres>, EngineError> {
        self.pool
            .begin()
            .await
            .map_err(|e| EngineError::StorageError(format!("{what} tx begin: {}", e)))
    }

    async fn graph_exists(&self, graph_id: &str) -> Result<bool, EngineError> {
        let row: Option<(String,)> =
            sqlx::query_as("SELECT graph_id FROM execution_graphs WHERE graph_id = $1")
                .bind(graph_id)
                .fetch_optional(self.pool.as_ref())
                .await
                .map_err(|e| EngineError::StorageError(format!("graph exists check: {}", e)))?;
        Ok(row.is_some())
    }

    /// Write one projected graph. `shadow` switches the graph/node clauses
    /// from `DO NOTHING` (import: never clobbers a newer shadow write) to
    /// `DO UPDATE` (shadow: mirrors the authoritative HashMap). attempts and
    /// completions are always `DO NOTHING` — they are append-only rows with
    /// deterministic ids. The five statements run in ONE transaction, which
    /// is where "Completed→SUCCEEDED + node_completions row same
    /// transaction" is realized.
    async fn write_projection(
        &self,
        p: &GraphProjection,
        shadow: bool,
        imported: bool,
    ) -> Result<BackfillStats, EngineError> {
        let mut tx = self
            .pool
            .begin()
            .await
            .map_err(|e| EngineError::StorageError(format!("graph tx begin: {}", e)))?;

        let graph_sql = if shadow {
            r#"INSERT INTO execution_graphs (graph_id, project_id, status, version, imported, created_at, updated_at)
               VALUES ($1, $2, $3, 1, $4, $5, $6)
               ON CONFLICT (graph_id) DO UPDATE SET
                   status = EXCLUDED.status,
                   project_id = EXCLUDED.project_id,
                   updated_at = EXCLUDED.updated_at"#
        } else {
            r#"INSERT INTO execution_graphs (graph_id, project_id, status, version, imported, created_at, updated_at)
               VALUES ($1, $2, $3, 1, $4, $5, $6)
               ON CONFLICT (graph_id) DO NOTHING"#
        };
        let mut stats = BackfillStats::default();
        let res = sqlx::query(graph_sql)
            .bind(&p.graph_id)
            .bind(&p.project_id)
            .bind(&p.status)
            .bind(imported)
            .bind(p.created_at)
            .bind(p.updated_at)
            .execute(&mut *tx)
            .await
            .map_err(|e| EngineError::StorageError(format!("graph insert: {}", e)))?;
        stats.graphs = res.rows_affected();

        let node_sql = if shadow {
            r#"INSERT INTO graph_nodes (graph_id, node_id, state, dependencies, required_capabilities, effect_class)
               VALUES ($1, $2, $3, $4, $5, $6)
               ON CONFLICT (node_id, graph_id) DO UPDATE SET
                   state = EXCLUDED.state,
                   dependencies = EXCLUDED.dependencies,
                   required_capabilities = EXCLUDED.required_capabilities,
                   effect_class = EXCLUDED.effect_class"#
        } else {
            r#"INSERT INTO graph_nodes (graph_id, node_id, state, dependencies, required_capabilities, effect_class)
               VALUES ($1, $2, $3, $4, $5, $6)
               ON CONFLICT (node_id, graph_id) DO NOTHING"#
        };
        for node in &p.nodes {
            let res = sqlx::query(node_sql)
                .bind(&p.graph_id)
                .bind(&node.node_id)
                .bind(&node.state)
                .bind(&node.dependencies)
                .bind(&node.required_capabilities)
                .bind(&node.effect_class)
                .execute(&mut *tx)
                .await
                .map_err(|e| EngineError::StorageError(format!("node insert: {}", e)))?;
            stats.nodes += res.rows_affected();
        }

        // Attempts / completions: append-only, always DO NOTHING. Bare
        // `ON CONFLICT DO NOTHING` (no target) also covers
        // UNIQUE(node_id, retry_no) from a differently-id'd rival.
        for attempt in &p.attempts {
            let res = sqlx::query(
                r#"INSERT INTO task_attempts
                       (attempt_id, graph_id, node_id, worker_id, status, retry_no, started_at, finished_at, result_ref)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                   ON CONFLICT DO NOTHING"#,
            )
            .bind(&attempt.attempt_id)
            .bind(&p.graph_id)
            .bind(&attempt.node_id)
            .bind(&attempt.worker_id)
            .bind(&attempt.status)
            .bind(attempt.retry_no)
            .bind(attempt.started_at)
            .bind(attempt.finished_at)
            .bind(&attempt.result_ref)
            .execute(&mut *tx)
            .await
            .map_err(|e| EngineError::StorageError(format!("attempt insert: {}", e)))?;
            stats.attempts += res.rows_affected();
        }

        for completion in &p.completions {
            // Deterministic committed_at fallback: a legacy node can be
            // SUCCEEDED without a completion timestamp; NOW() would make the
            // import byte-unstable across databases, so fall back to the
            // projection's own updated_at instead.
            let res = sqlx::query(
                r#"INSERT INTO node_completions (node_id, graph_id, winning_attempt_id, result_ref, committed_at)
                   VALUES ($1, $2, $3, $4, COALESCE($5, $6))
                   ON CONFLICT DO NOTHING"#,
            )
            .bind(&completion.node_id)
            .bind(&p.graph_id)
            .bind(&completion.winning_attempt_id)
            .bind(&completion.result_ref)
            .bind(completion.committed_at)
            .bind(p.updated_at)
            .execute(&mut *tx)
            .await
            .map_err(|e| EngineError::StorageError(format!("completion insert: {}", e)))?;
            stats.completions += res.rows_affected();
        }

        tx.commit()
            .await
            .map_err(|e| EngineError::StorageError(format!("graph tx commit: {}", e)))?;
        Ok(stats)
    }

    /// Source A: backfill the graph tables from the PG `tasks` table (the
    /// whole-Task JSONB rows). Idempotent (`ON CONFLICT DO NOTHING`) and run
    /// automatically after every graph migration by the startup assembly, so
    /// a re-run is a no-op. Missing `tasks` table (memory task backend) is
    /// not an error — the backfill just reports zeros.
    pub async fn backfill_from_tasks_table(&self) -> Result<BackfillStats, EngineError> {
        let table: Option<(Option<String>,)> = sqlx::query_as("SELECT to_regclass('tasks')::text")
            .fetch_optional(self.pool.as_ref())
            .await
            .map_err(|e| EngineError::StorageError(format!("tasks table probe: {}", e)))?;
        if table.and_then(|t| t.0).is_none() {
            tracing::info!("No `tasks` table — graph backfill source A skipped (memory backend?)");
            return Ok(BackfillStats::default());
        }

        let rows = sqlx::query_as::<_, (String, String, String, String, serde_json::Value, chrono::DateTime<chrono::Utc>, chrono::DateTime<chrono::Utc>)>(
            "SELECT id, description, project_id, status, subtasks, created_at, updated_at FROM tasks ORDER BY created_at DESC, id"
        )
        .fetch_all(self.pool.as_ref())
        .await
        .map_err(|e| EngineError::StorageError(format!("tasks scan: {}", e)))?;

        let mut total = BackfillStats::default();
        for (id, _desc, project_id, status, subtasks_json, created_at, updated_at) in rows {
            if self.graph_exists(&id).await? {
                total.skipped_existing += 1;
                continue;
            }
            let subtasks: Vec<Subtask> = match serde_json::from_value(subtasks_json) {
                Ok(subtasks) => subtasks,
                Err(e) => {
                    // One bad legacy row must not block graph population —
                    // same policy as parse_task_status and source B's
                    // unparseable-file handling: warn and skip the row.
                    tracing::warn!(
                        graph_id = %id,
                        error = %e,
                        "Skipping tasks row with unparseable subtasks JSONB (graph backfill source A)"
                    );
                    continue;
                }
            };
            let task = Task {
                id: uc_types::TaskId(id),
                description: String::new(),
                project_id,
                status: parse_task_status(&status),
                subtasks,
                created_at,
                updated_at,
            };
            let projection = project_task(&task);
            let stats = self.write_projection(&projection, false, true).await?;
            total.graphs += stats.graphs;
            total.nodes += stats.nodes;
            total.attempts += stats.attempts;
            total.completions += stats.completions;
        }
        tracing::info!(
            graphs = total.graphs,
            nodes = total.nodes,
            attempts = total.attempts,
            completions = total.completions,
            skipped = total.skipped_existing,
            "Graph backfill source A (tasks table) complete"
        );
        Ok(total)
    }

    /// Source B: one-time import of a `.uc` directory (`<dir>/tasks/*.json`,
    /// newer `savedAt` checkpoint from `<dir>/checkpoints/*.snap.json` wins).
    /// Called ONLY when `UC_GRAPH_IMPORT_DIR` is explicitly set, and a file
    /// is skipped when its graph already exists in PG — the direction is
    /// files→graph once, never graph→files (D2 constraint 1).
    pub async fn import_tasks_dir(
        &self,
        dir: &std::path::Path,
    ) -> Result<BackfillStats, EngineError> {
        let tasks_dir = dir.join("tasks");
        let entries = match std::fs::read_dir(&tasks_dir) {
            Ok(entries) => entries,
            Err(e) => {
                tracing::warn!(
                    path = %tasks_dir.display(),
                    error = %e,
                    "UC_GRAPH_IMPORT_DIR has no readable tasks/ directory — source B import skipped"
                );
                return Ok(BackfillStats::default());
            }
        };
        let mut files: Vec<std::path::PathBuf> = entries
            .map(|e| e.map_err(|e| EngineError::StorageError(format!("read_dir: {}", e))))
            .collect::<Result<Vec<_>, _>>()?
            .into_iter()
            .map(|e| e.path())
            .filter(|p| p.extension().is_some_and(|x| x == "json"))
            .collect();
        // Deterministic order regardless of the filesystem.
        files.sort();

        let mut total = BackfillStats::default();
        for path in files {
            let text = std::fs::read_to_string(&path).map_err(|e| {
                EngineError::StorageError(format!("read {}: {}", path.display(), e))
            })?;
            let task: TsPersistedTask = match serde_json::from_str(&text) {
                Ok(t) => t,
                Err(e) => {
                    tracing::warn!(path = %path.display(), error = %e, "Skipping unparseable task JSON");
                    continue;
                }
            };
            if task.id.is_empty() {
                tracing::warn!(path = %path.display(), "Skipping task JSON without an id");
                continue;
            }
            if self.graph_exists(&task.id).await? {
                total.skipped_existing += 1;
                continue;
            }
            // Checkpoint wins only when strictly newer (savedAt rule, F46).
            let cp_path = dir
                .join("checkpoints")
                .join(format!("{}.snap.json", task.id));
            let checkpoint: Option<TsPersistedTask> = match std::fs::read_to_string(&cp_path) {
                Ok(text) => match serde_json::from_str(&text) {
                    Ok(t) => Some(t),
                    Err(e) => {
                        tracing::warn!(path = %cp_path.display(), error = %e, "Ignoring unparseable checkpoint");
                        None
                    }
                },
                Err(_) => None,
            };
            let winner = pick_newer_saved(task, checkpoint);
            let projection = project_ts_task(&winner);
            let stats = self.write_projection(&projection, false, true).await?;
            total.graphs += stats.graphs;
            total.nodes += stats.nodes;
            total.attempts += stats.attempts;
            total.completions += stats.completions;
        }
        tracing::info!(
            graphs = total.graphs,
            nodes = total.nodes,
            skipped = total.skipped_existing,
            "Graph import source B (.uc tasks dir) complete"
        );
        Ok(total)
    }

    /// Shadow upsert beside `persist_task`: mirror the authoritative HashMap
    /// entry into the graph tables. Never touches `imported` on conflict (the
    /// DO UPDATE clause only sets status/project/updated_at), so an imported
    /// graph keeps its provenance marker.
    pub async fn upsert_task_shadow(&self, task: &Task) -> Result<BackfillStats, EngineError> {
        let projection = project_task(task);
        self.write_projection(&projection, true, false).await
    }

    /// Startup shadow-read diff: compare the row-table projection of `task`
    /// against the in-memory HashMap entry. Returns human-readable diffs;
    /// the caller only ever `tracing::warn`s them (never rewrites, never
    /// changes a read path).
    pub async fn shadow_diff(&self, task: &Task) -> Result<Vec<String>, EngineError> {
        let mut diffs = Vec::new();
        let graph_row: Option<(String, String)> =
            sqlx::query_as("SELECT status, project_id FROM execution_graphs WHERE graph_id = $1")
                .bind(&task.id.0)
                .fetch_optional(self.pool.as_ref())
                .await
                .map_err(|e| EngineError::StorageError(format!("shadow diff graph: {}", e)))?;
        let Some((db_status, db_project)) = graph_row else {
            diffs.push(format!("graph {} missing from row tables", task.id.0));
            return Ok(diffs);
        };
        let want_status = graph_status_of_task_status(&task.status);
        if db_status != want_status {
            diffs.push(format!(
                "graph {} status: rows={} memory={}",
                task.id.0, db_status, want_status
            ));
        }
        if db_project != task.project_id {
            diffs.push(format!(
                "graph {} project_id: rows={} memory={}",
                task.id.0, db_project, task.project_id
            ));
        }
        let nodes: Vec<(String, String)> =
            sqlx::query_as("SELECT node_id, state FROM graph_nodes WHERE graph_id = $1")
                .bind(&task.id.0)
                .fetch_all(self.pool.as_ref())
                .await
                .map_err(|e| EngineError::StorageError(format!("shadow diff nodes: {}", e)))?;
        let mut db_states: Vec<(String, String)> = nodes;
        db_states.sort();
        let mut mem: Vec<(String, String)> = task
            .subtasks
            .iter()
            .map(|st| (st.id.0.clone(), node_status_of_subtask(st)))
            .collect();
        mem.sort();
        for (id, state) in &mem {
            match db_states.iter().find(|(nid, _)| nid == id) {
                Some((_, db_state)) if db_state == state => {}
                Some((_, db_state)) => diffs.push(format!(
                    "node {} state: rows={} memory={}",
                    id, db_state, state
                )),
                None => diffs.push(format!("node {} missing from row tables", id)),
            }
        }
        for (id, _) in &db_states {
            if !mem.iter().any(|(mid, _)| mid == id) {
                diffs.push(format!("node {} in row tables but not in memory", id));
            }
        }
        Ok(diffs)
    }

    /// Row-table view of graph ids, newest first. The `graph_id` tie-break
    /// makes the order total (the tasks table's `ORDER BY created_at DESC`
    /// is not) — the cross-backend ordering test pins the two together.
    pub async fn list_graph_ids_newest_first(&self) -> Result<Vec<String>, EngineError> {
        let rows: Vec<(String,)> = sqlx::query_as(
            "SELECT graph_id FROM execution_graphs ORDER BY created_at DESC, graph_id",
        )
        .fetch_all(self.pool.as_ref())
        .await
        .map_err(|e| EngineError::StorageError(format!("graph list: {}", e)))?;
        Ok(rows.into_iter().map(|(id,)| id).collect())
    }

    // ── T3 (#639): attempt-lifecycle transitions ─────────────────────
    //
    // Every method below is exactly ONE transaction: node rows are moved by
    // a version-CAS `UPDATE … WHERE version = $n` (guarded by
    // `transition_ok`, i.e. `uc_types::can_transition` on the 9-state
    // machine), each state move appends `execution_events` rows carrying
    // `event_type` + `graph_version` (the reserved cost/tokens columns stay
    // NULL — P2 Optimizer owns writers), and commit-once rides the
    // `node_completions` PK via `INSERT … ON CONFLICT DO NOTHING` rows
    // affected. Lock order everywhere is graph → node → attempt
    // (`SELECT … FOR UPDATE`), so the mutation methods serialize per node
    // instead of dead-locking against each other. The legacy HashMap paths
    // are untouched: this is the graph plane growing the real logic beside
    // it (dual-plane ruling, research §3).

    /// Dispatch `(graph, node)` in one transaction: `READY → SCHEDULED →
    /// RUNNING` ("派发即跑" — the two edges share one tx), insert a fresh
    /// `RUNNING` attempt row whose `retry_no` is `max(retry_no)+1` per node
    /// (0 for a never-attempted node) and whose `worker_epoch` is
    /// `max(epoch)+1` — the monotonic fence token. Returns the new attempt
    /// id, or `None` when the node is absent from the row tables or not
    /// `READY` (CAS lost / shadow-mirror still catching up): a no-op, never
    /// an error on the fire-and-forget path.
    pub async fn schedule_attempt(
        &self,
        graph_id: &str,
        node_id: &str,
        worker_id: Option<&str>,
    ) -> Result<Option<String>, EngineError> {
        let mut tx = self.begin_tx("schedule_attempt").await?;

        let node: Option<(String, i64)> = sqlx::query_as(
            "SELECT state, version FROM graph_nodes WHERE graph_id = $1 AND node_id = $2 FOR UPDATE",
        )
        .bind(graph_id)
        .bind(node_id)
        .fetch_optional(&mut *tx)
        .await
        .map_err(|e| EngineError::StorageError(format!("schedule: node lock: {}", e)))?;
        let Some((state, version)) = node else {
            return Ok(None); // node not in the row tables yet — no-op
        };
        if state != NodeStatus::Ready.as_str()
            || !transition_ok(state.as_str(), NodeStatus::Scheduled.as_str())
            || !transition_ok(NodeStatus::Scheduled.as_str(), NodeStatus::Running.as_str())
        {
            return Ok(None);
        }

        let (retry_no, worker_epoch): (i32, i64) = sqlx::query_as(
            "SELECT COALESCE(MAX(retry_no), -1) + 1, COALESCE(MAX(worker_epoch), 0) + 1 \
             FROM task_attempts WHERE graph_id = $1 AND node_id = $2",
        )
        .bind(graph_id)
        .bind(node_id)
        .fetch_one(&mut *tx)
        .await
        .map_err(|e| EngineError::StorageError(format!("schedule: retry probe: {}", e)))?;

        // READY → SCHEDULED → RUNNING: two CAS moves, both inside this tx.
        let gv = bump_graph_version_tx(&mut tx, graph_id).await?;
        let scheduled = cas_node_state_tx(
            &mut tx,
            graph_id,
            node_id,
            NodeStatus::Ready.as_str(),
            version,
            NodeStatus::Scheduled.as_str(),
        )
        .await?;
        if !scheduled {
            // Version moved under us despite the FOR UPDATE (impossible
            // against this store, kept honest for trigger-mutated schemas):
            // roll the tx back by dropping without commit.
            return Ok(None);
        }
        append_event_tx(
            &mut tx,
            graph_id,
            Some(node_id),
            None,
            Some(gv),
            "node_scheduled",
            serde_json::json!({ "worker_id": worker_id, "retry_no": retry_no }),
        )
        .await?;
        let running = cas_node_state_tx(
            &mut tx,
            graph_id,
            node_id,
            NodeStatus::Scheduled.as_str(),
            version + 1,
            NodeStatus::Running.as_str(),
        )
        .await?;
        if !running {
            return Ok(None);
        }
        append_event_tx(
            &mut tx,
            graph_id,
            Some(node_id),
            None,
            Some(gv),
            "node_running",
            serde_json::json!({ "worker_id": worker_id, "retry_no": retry_no }),
        )
        .await?;

        let attempt = attempt_id(graph_id, node_id, retry_no);
        sqlx::query(
            "INSERT INTO task_attempts \
                 (attempt_id, graph_id, node_id, worker_id, worker_epoch, status, retry_no, \
                  started_at, heartbeat_at, result_ref) \
             VALUES ($1, $2, $3, $4, $5, 'RUNNING', $6, NOW(), NOW(), $7)",
        )
        .bind(&attempt)
        .bind(graph_id)
        .bind(node_id)
        .bind(worker_id)
        .bind(worker_epoch)
        .bind(retry_no)
        .bind(format!("{graph_id}/{node_id}/{retry_no}"))
        .execute(&mut *tx)
        .await
        .map_err(|e| EngineError::StorageError(format!("schedule: attempt insert: {}", e)))?;
        append_event_tx(
            &mut tx,
            graph_id,
            Some(node_id),
            Some(&attempt),
            Some(gv),
            "attempt_started",
            serde_json::json!({ "worker_id": worker_id, "worker_epoch": worker_epoch }),
        )
        .await?;
        tx.commit()
            .await
            .map_err(|e| EngineError::StorageError(format!("schedule tx commit: {}", e)))?;
        Ok(Some(attempt))
    }

    /// Refresh `heartbeat_at = NOW()` on a still-`RUNNING` attempt. Returns
    /// false (no state written) for unknown or already-terminal attempts —
    /// the fence makes heartbeats from revoked workers harmless.
    pub async fn heartbeat_attempt(&self, attempt_id: &str) -> Result<bool, EngineError> {
        let mut tx = self.begin_tx("heartbeat_attempt").await?;
        let row: Option<(String, String)> = sqlx::query_as(
            "SELECT graph_id, node_id FROM task_attempts WHERE attempt_id = $1 FOR UPDATE",
        )
        .bind(attempt_id)
        .fetch_optional(&mut *tx)
        .await
        .map_err(|e| EngineError::StorageError(format!("heartbeat: attempt lock: {}", e)))?;
        let Some((graph_id, node_id)) = row else {
            return Ok(false);
        };
        let res = sqlx::query("UPDATE task_attempts SET heartbeat_at = NOW() WHERE attempt_id = $1 AND status = 'RUNNING'")
            .bind(attempt_id)
            .execute(&mut *tx)
            .await
            .map_err(|e| EngineError::StorageError(format!("heartbeat update: {}", e)))?;
        if res.rows_affected() == 1 {
            let (graph_version,): (i64,) =
                sqlx::query_as("SELECT version FROM execution_graphs WHERE graph_id = $1")
                    .bind(&graph_id)
                    .fetch_optional(&mut *tx)
                    .await
                    .map_err(|e| {
                        EngineError::StorageError(format!("heartbeat: version read: {}", e))
                    })?
                    .unwrap_or((0,));
            append_event_tx(
                &mut tx,
                &graph_id,
                Some(&node_id),
                Some(attempt_id),
                Some(graph_version),
                "attempt_heartbeat",
                serde_json::json!({}),
            )
            .await?;
        }
        tx.commit()
            .await
            .map_err(|e| EngineError::StorageError(format!("heartbeat tx commit: {}", e)))?;
        Ok(res.rows_affected() == 1)
    }

    /// The node's currently-`RUNNING` attempt id (highest `retry_no`), when
    /// one exists. Sink-verb resolution helper: the gateway envelope carries
    /// the dispatch-level counter, the graph plane owns the execution
    /// numbering, so commit/fail heartbeats resolve identity through this.
    pub async fn running_attempt_id(
        &self,
        graph_id: &str,
        node_id: &str,
    ) -> Result<Option<String>, EngineError> {
        let row: Option<(String,)> = sqlx::query_as(
            "SELECT attempt_id FROM task_attempts \
             WHERE graph_id = $1 AND node_id = $2 AND status = 'RUNNING' \
             ORDER BY retry_no DESC LIMIT 1",
        )
        .bind(graph_id)
        .bind(node_id)
        .fetch_optional(self.pool.as_ref())
        .await
        .map_err(|e| EngineError::StorageError(format!("running attempt probe: {}", e)))?;
        Ok(row.map(|(id,)| id))
    }

    /// Commit-once for `(graph, node)` attributed to `winning_attempt_id`.
    ///
    /// Winner path (returns `true`): `INSERT INTO node_completions … ON
    /// CONFLICT (node_id) DO NOTHING` rows-affected == 1 → attempt marked
    /// `SUCCEEDED`, node CAS `RUNNING → SUCCEEDED`, downstream
    /// `recompute_ready` flips newly-unblocked `CREATED` nodes to
    /// `READY` — all in the same transaction. Loser / fenced / late path
    /// (returns `false`): the attempt row is unknown or no longer `RUNNING`
    /// (fence via the monotonic `worker_epoch` bump) or another attempt
    /// already owns the completion — node state is NOT touched and only a
    /// `late_result` event is appended. The dual-write race this closes:
    /// two concurrent commits for one node can never both win.
    pub async fn commit_once(
        &self,
        graph_id: &str,
        node_id: &str,
        winning_attempt_id: &str,
        result_ref: Option<&str>,
    ) -> Result<bool, EngineError> {
        let mut tx = self.begin_tx("commit_once").await?;

        let node: Option<(String, i64)> = sqlx::query_as(
            "SELECT state, version FROM graph_nodes WHERE graph_id = $1 AND node_id = $2 FOR UPDATE",
        )
        .bind(graph_id)
        .bind(node_id)
        .fetch_optional(&mut *tx)
        .await
        .map_err(|e| EngineError::StorageError(format!("commit: node lock: {}", e)))?;
        let attempt_status: Option<(String,)> =
            sqlx::query_as("SELECT status FROM task_attempts WHERE attempt_id = $1 FOR UPDATE")
                .bind(winning_attempt_id)
                .fetch_optional(&mut *tx)
                .await
                .map_err(|e| EngineError::StorageError(format!("commit: attempt lock: {}", e)))?;

        let fenced = match (&node, &attempt_status) {
            (Some((state, _)), Some((attempt,))) => {
                state.as_str() == NodeStatus::Succeeded.as_str() || attempt.as_str() != "RUNNING"
            }
            _ => true, // node/attempt unknown to the graph plane
        };
        if fenced {
            append_event_tx(
                &mut tx,
                graph_id,
                Some(node_id),
                Some(winning_attempt_id),
                None,
                "late_result",
                serde_json::json!({ "reason": "fenced_or_unknown_attempt" }),
            )
            .await?;
            tx.commit()
                .await
                .map_err(|e| EngineError::StorageError(format!("commit tx commit: {}", e)))?;
            return Ok(false);
        }
        let (node_state, node_version) = node.expect("fenced matched above");

        let res = sqlx::query(
            "INSERT INTO node_completions (node_id, graph_id, winning_attempt_id, result_ref) \
             VALUES ($1, $2, $3, $4) ON CONFLICT (node_id) DO NOTHING",
        )
        .bind(node_id)
        .bind(graph_id)
        .bind(winning_attempt_id)
        .bind(result_ref)
        .execute(&mut *tx)
        .await
        .map_err(|e| EngineError::StorageError(format!("commit: completion insert: {}", e)))?;
        if res.rows_affected() == 0 {
            // Commit-once loser: someone else owns the completion row.
            append_event_tx(
                &mut tx,
                graph_id,
                Some(node_id),
                Some(winning_attempt_id),
                None,
                "late_result",
                serde_json::json!({ "reason": "lost_commit_once" }),
            )
            .await?;
            tx.commit()
                .await
                .map_err(|e| EngineError::StorageError(format!("commit tx commit: {}", e)))?;
            return Ok(false);
        }

        let gv = bump_graph_version_tx(&mut tx, graph_id).await?;
        sqlx::query(
            "UPDATE task_attempts SET status = 'SUCCEEDED', finished_at = NOW(), \
                 result_ref = COALESCE($2, result_ref) \
             WHERE attempt_id = $1 AND status = 'RUNNING'",
        )
        .bind(winning_attempt_id)
        .bind(result_ref)
        .execute(&mut *tx)
        .await
        .map_err(|e| EngineError::StorageError(format!("commit: attempt update: {}", e)))?;
        if transition_ok(&node_state, NodeStatus::Succeeded.as_str()) {
            let moved = cas_node_state_tx(
                &mut tx,
                graph_id,
                node_id,
                &node_state,
                node_version,
                NodeStatus::Succeeded.as_str(),
            )
            .await?;
            if !moved {
                tracing::warn!(
                    graph_id,
                    node_id,
                    "commit_once won the completion but the node CAS missed (state {:?}) — dual-plane shadow clobber?",
                    node_state
                );
            }
        } else {
            tracing::warn!(
                graph_id,
                node_id,
                state = %node_state,
                "commit_once won with a node in a non-transitionable state — completion recorded, state untouched"
            );
        }
        append_event_tx(
            &mut tx,
            graph_id,
            Some(node_id),
            Some(winning_attempt_id),
            Some(gv),
            "node_succeeded",
            serde_json::json!({ "result_ref": result_ref }),
        )
        .await?;
        // Downstream dependency recompute rides the same transaction (the
        // winner's graph version for its `node_ready` events).
        recompute_ready_tx(&mut tx, graph_id, Some(gv)).await?;
        tx.commit()
            .await
            .map_err(|e| EngineError::StorageError(format!("commit tx commit: {}", e)))?;
        Ok(true)
    }

    /// Fail one attempt: `RUNNING → FAILED` on the attempt row plus the
    /// fence (the next `schedule_attempt` carries `max(worker_epoch)+1` —
    /// monotonic epoch bump), then the node follows the retry budget:
    /// `retry_no < max_attempts - 1` re-arms the node to `READY`
    /// (timeout/dispatch retry), else the node itself goes `FAILED`.
    ///
    /// Late or already-terminal attempts (and any fail against a node that
    /// already `SUCCEEDED` — the committed winner is immutable) return
    /// [`FailOutcome::Fenced`]: state is NOT touched, only a `late_result`
    /// event is appended.
    pub async fn fail_attempt(
        &self,
        graph_id: &str,
        node_id: &str,
        attempt_id: &str,
        max_attempts: i32,
        reason: &str,
    ) -> Result<FailOutcome, EngineError> {
        let mut tx = self.begin_tx("fail_attempt").await?;
        let outcome =
            fail_attempt_tx(&mut tx, graph_id, node_id, attempt_id, max_attempts, reason).await?;
        tx.commit()
            .await
            .map_err(|e| EngineError::StorageError(format!("fail tx commit: {}", e)))?;
        Ok(outcome)
    }

    /// Graph-plane reaper: every `RUNNING` attempt whose `heartbeat_at`
    /// (falling back to `started_at`) is older than `heartbeat_timeout`
    /// goes through the [`Self::fail_attempt`] path (attempt `FAILED` +
    /// fence via the next attempt's epoch bump + node back to `READY` while
    /// budget lasts, else `FAILED`). Returns the attempts actually swept
    /// (fenced no-ops are excluded) with their outcomes, so the monitor can
    /// bridge each one back into the legacy store (T6 #642).
    pub async fn timeout_sweep(
        &self,
        heartbeat_timeout: std::time::Duration,
        max_attempts: i32,
    ) -> Result<Vec<SweptAttempt>, EngineError> {
        let secs = heartbeat_timeout.as_secs_f64();
        let stale: Vec<(String, String, String)> = sqlx::query_as(
            "SELECT attempt_id, graph_id, node_id FROM task_attempts \
             WHERE status = 'RUNNING' \
               AND COALESCE(heartbeat_at, started_at) < NOW() - ($1::double precision * INTERVAL '1 second') \
             ORDER BY attempt_id",
        )
        .bind(secs)
        .fetch_all(self.pool.as_ref())
        .await
        .map_err(|e| EngineError::StorageError(format!("timeout sweep scan: {}", e)))?;
        let mut swept = Vec::new();
        for (attempt, graph_id, node_id) in stale {
            let outcome = self
                .fail_attempt(
                    &graph_id,
                    &node_id,
                    &attempt,
                    max_attempts,
                    "heartbeat_timeout",
                )
                .await?;
            if outcome != FailOutcome::Fenced {
                swept.push(SweptAttempt {
                    graph_id,
                    node_id,
                    attempt_id: attempt,
                    rearmed: outcome == FailOutcome::RearmedToReady,
                });
            }
        }
        Ok(swept)
    }

    /// Dependency recomputation at the node layer (absorbs the semantics of
    /// the legacy `get_ready_subtasks` onto the graph rows — the legacy
    /// function itself is untouched): every `CREATED` node whose
    /// dependencies are all `SUCCEEDED` (or `SKIPPED` **and** `optional`)
    /// flips to `READY`. Idempotent — a node already `READY` is not a
    /// candidate, and a second sweep finds nothing new. Returns the flipped
    /// node ids.
    pub async fn recompute_ready(&self, graph_id: &str) -> Result<Vec<String>, EngineError> {
        let mut tx = self.begin_tx("recompute_ready").await?;
        let flipped = recompute_ready_tx(&mut tx, graph_id, None).await?;
        tx.commit()
            .await
            .map_err(|e| EngineError::StorageError(format!("recompute tx commit: {}", e)))?;
        Ok(flipped)
    }
}

/// Default attempt budget for the graph plane's `fail_attempt` path —
/// mirrors the legacy gateway's dispatch-retry cap of 3 (`Remote` mode /
/// stale-`Assigned` revert storm guard), so the two planes converge on the
/// same "give up" point while T6 does the authority switch.
#[cfg(feature = "storage")]
pub const DEFAULT_MAX_ATTEMPTS: i32 = 3;

/// Result of [`GraphStore::fail_attempt`] / the timeout sweep per attempt.
#[cfg(feature = "storage")]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FailOutcome {
    /// The attempt was unknown / already terminal / the node already
    /// committed: no state change, only a `late_result` event was appended.
    Fenced,
    /// Retry budget left: attempt `FAILED`, node re-armed to `READY`
    /// (fence rides the next attempt's `worker_epoch` bump).
    RearmedToReady,
    /// Budget exhausted: attempt `FAILED` and the node itself `FAILED`.
    NodeFailed,
}

/// Bump (or lazily materialize) the graph row's version and return it. The
/// insert-on-conflict shape keeps the T3 verbs working against graphs the
/// shadow write has not mirrored yet — the mirror overwrites `status` on
/// its next pass anyway.
#[cfg(feature = "storage")]
async fn bump_graph_version_tx(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    graph_id: &str,
) -> Result<i64, EngineError> {
    let (version,): (i64,) = sqlx::query_as(
        "INSERT INTO execution_graphs (graph_id, status, version) VALUES ($1, 'RUNNING', 1) \
         ON CONFLICT (graph_id) DO UPDATE SET version = execution_graphs.version + 1, \
             updated_at = NOW() \
         RETURNING version",
    )
    .bind(graph_id)
    .fetch_one(&mut **tx)
    .await
    .map_err(|e| EngineError::StorageError(format!("graph version bump: {}", e)))?;
    Ok(version)
}

/// Append one `execution_events` row. `cost` / `tokens` / `duration_ms` are
/// deliberately never bound — they stay NULL for their reserved writers.
#[cfg(feature = "storage")]
#[allow(clippy::too_many_arguments)]
async fn append_event_tx(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    graph_id: &str,
    node_id: Option<&str>,
    attempt_id: Option<&str>,
    graph_version: Option<i64>,
    event_type: &str,
    payload: serde_json::Value,
) -> Result<(), EngineError> {
    sqlx::query(
        "INSERT INTO execution_events (graph_id, node_id, attempt_id, graph_version, event_type, payload) \
         VALUES ($1, $2, $3, $4, $5, $6)",
    )
    .bind(graph_id)
    .bind(node_id)
    .bind(attempt_id)
    .bind(graph_version)
    .bind(event_type)
    .bind(payload)
    .execute(&mut **tx)
    .await
    .map_err(|e| EngineError::StorageError(format!("event append ({event_type}): {}", e)))?;
    Ok(())
}

/// Version-CAS node state move: succeeds only while the row still carries
/// the expected state AND version (the caller holds the row lock, so a miss
/// means a concurrent writer — the loser returns without mutating).
#[cfg(feature = "storage")]
async fn cas_node_state_tx(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    graph_id: &str,
    node_id: &str,
    expect_state: &str,
    expect_version: i64,
    new_state: &str,
) -> Result<bool, EngineError> {
    debug_assert!(
        transition_ok(expect_state, new_state),
        "cas {expect_state} -> {new_state} bypasses the transition table — caller must gate"
    );
    if !transition_ok(expect_state, new_state) {
        return Err(uc_types::EngineError::InvalidOperation(format!(
            "illegal node transition {expect_state} -> {new_state}"
        )));
    }
    let res = sqlx::query(
        "UPDATE graph_nodes SET state = $3, version = version + 1 \
         WHERE graph_id = $1 AND node_id = $2 AND state = $4 AND version = $5",
    )
    .bind(graph_id)
    .bind(node_id)
    .bind(new_state)
    .bind(expect_state)
    .bind(expect_version)
    .execute(&mut **tx)
    .await
    .map_err(|e| EngineError::StorageError(format!("node CAS update: {}", e)))?;
    Ok(res.rows_affected() == 1)
}

/// The shared body of [`GraphStore::fail_attempt`] (runs inside the caller's
/// transaction; lock order node → attempt, matching every other verb).
#[cfg(feature = "storage")]
#[allow(clippy::too_many_arguments)]
async fn fail_attempt_tx(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    graph_id: &str,
    node_id: &str,
    attempt_id: &str,
    max_attempts: i32,
    reason: &str,
) -> Result<FailOutcome, EngineError> {
    let node: Option<(String, i64)> = sqlx::query_as(
        "SELECT state, version FROM graph_nodes WHERE graph_id = $1 AND node_id = $2 FOR UPDATE",
    )
    .bind(graph_id)
    .bind(node_id)
    .fetch_optional(&mut **tx)
    .await
    .map_err(|e| EngineError::StorageError(format!("fail: node lock: {}", e)))?;
    let attempt: Option<(String, i32)> = sqlx::query_as(
        "SELECT status, retry_no FROM task_attempts WHERE attempt_id = $1 FOR UPDATE",
    )
    .bind(attempt_id)
    .fetch_optional(&mut **tx)
    .await
    .map_err(|e| EngineError::StorageError(format!("fail: attempt lock: {}", e)))?;

    let live = match (&node, &attempt) {
        (Some((state, _)), Some((status, _))) => {
            status.as_str() == "RUNNING" && state.as_str() != NodeStatus::Succeeded.as_str()
        }
        _ => false,
    };
    if !live {
        // Late fail (after commit, after its own terminal write, or for an
        // attempt the graph plane never saw): the only trace is the event.
        append_event_tx(
            tx,
            graph_id,
            Some(node_id),
            Some(attempt_id),
            None,
            "late_result",
            serde_json::json!({ "reason": format!("late_fail: {reason}") }),
        )
        .await?;
        return Ok(FailOutcome::Fenced);
    }
    let (node_state, node_version) = node.expect("live matched above");
    let (_status, retry_no) = attempt.expect("live matched above");

    sqlx::query(
        "UPDATE task_attempts SET status = 'FAILED', finished_at = NOW() WHERE attempt_id = $1",
    )
    .bind(attempt_id)
    .execute(&mut **tx)
    .await
    .map_err(|e| EngineError::StorageError(format!("fail: attempt update: {}", e)))?;
    let gv = bump_graph_version_tx(tx, graph_id).await?;
    append_event_tx(
        tx,
        graph_id,
        Some(node_id),
        Some(attempt_id),
        Some(gv),
        "attempt_failed",
        serde_json::json!({ "reason": reason, "retry_no": retry_no }),
    )
    .await?;

    let target = if retry_no < max_attempts.saturating_sub(1) {
        NodeStatus::Ready.as_str()
    } else {
        NodeStatus::Failed.as_str()
    };
    let outcome = if target == NodeStatus::Ready.as_str() {
        FailOutcome::RearmedToReady
    } else {
        FailOutcome::NodeFailed
    };
    if transition_ok(&node_state, target) {
        let moved =
            cas_node_state_tx(tx, graph_id, node_id, &node_state, node_version, target).await?;
        if !moved {
            tracing::warn!(
                graph_id,
                node_id,
                "fail_attempt node CAS missed (version moved under the lock)"
            );
        }
        append_event_tx(
            tx,
            graph_id,
            Some(node_id),
            Some(attempt_id),
            Some(gv),
            if outcome == FailOutcome::RearmedToReady {
                "node_ready"
            } else {
                "node_failed"
            },
            serde_json::json!({ "reason": reason }),
        )
        .await?;
    } else {
        tracing::warn!(
            graph_id,
            node_id,
            state = %node_state,
            target,
            "fail_attempt kept the node state — transition not legal from it"
        );
    }
    Ok(outcome)
}

/// Shared body of [`GraphStore::recompute_ready`] and the winner path of
/// [`GraphStore::commit_once`]. Flips every `CREATED` node whose
/// dependencies are all satisfied (`SUCCEEDED`, or `SKIPPED` + `optional`)
/// to `READY`, appending one `node_ready` event each. `graph_version` is
/// supplied when the caller already bumped the graph row in this tx; else a
/// bump happens here, but only when at least one node actually flips — the
/// no-candidate path writes nothing, which is what makes the method
/// idempotent and version-stable across repeat sweeps.
#[cfg(feature = "storage")]
async fn recompute_ready_tx(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    graph_id: &str,
    graph_version: Option<i64>,
) -> Result<Vec<String>, EngineError> {
    let candidates: Vec<(String, serde_json::Value, i64)> = sqlx::query_as(
        "SELECT node_id, dependencies, version FROM graph_nodes \
         WHERE graph_id = $1 AND state = 'CREATED' ORDER BY node_id FOR UPDATE",
    )
    .bind(graph_id)
    .fetch_all(&mut **tx)
    .await
    .map_err(|e| EngineError::StorageError(format!("recompute: candidate scan: {}", e)))?;
    if candidates.is_empty() {
        return Ok(Vec::new());
    }
    let satisfied_rows: Vec<(String, String, bool)> = sqlx::query_as(
        "SELECT node_id, state, optional FROM graph_nodes \
         WHERE graph_id = $1 AND state IN ('SUCCEEDED', 'SKIPPED')",
    )
    .bind(graph_id)
    .fetch_all(&mut **tx)
    .await
    .map_err(|e| EngineError::StorageError(format!("recompute: done-set scan: {}", e)))?;

    let mut gv = graph_version;
    let mut flipped = Vec::new();
    for (node_id, deps, version) in candidates {
        let dep_ids: Vec<String> = deps
            .as_array()
            .map(|arr| {
                arr.iter()
                    .filter_map(|v| v.as_str().map(String::from))
                    .collect()
            })
            .unwrap_or_default();
        let all_satisfied = dep_ids.iter().all(|dep| {
            satisfied_rows
                .iter()
                .any(|(id, state, optional)| id == dep && (state == "SUCCEEDED" || *optional))
        });
        if !all_satisfied {
            continue;
        }
        let moved = cas_node_state_tx(
            tx,
            graph_id,
            &node_id,
            NodeStatus::Created.as_str(),
            version,
            NodeStatus::Ready.as_str(),
        )
        .await?;
        if !moved {
            continue;
        }
        // The graph row is bumped lazily — a sweep that finds satisfied
        // candidates writes a version, an unsatisfied one writes nothing.
        let version_for_event = match gv {
            Some(v) => v,
            None => {
                let v = bump_graph_version_tx(tx, graph_id).await?;
                gv = Some(v);
                v
            }
        };
        append_event_tx(
            tx,
            graph_id,
            Some(&node_id),
            None,
            Some(version_for_event),
            "node_ready",
            serde_json::json!({ "dependencies_satisfied": dep_ids }),
        )
        .await?;
        flipped.push(node_id);
    }
    Ok(flipped)
}

/// `tasks.status` TEXT → `TaskStatus` (same mapping as
/// `PostgresTaskBackend::row_to_task`; unknown values fall back to
/// `Created` with a warn rather than failing the whole backfill — one bad
/// legacy row must not block graph population).
#[cfg(feature = "storage")]
fn parse_task_status(status: &str) -> TaskStatus {
    let parsed = match status {
        "Created" => TaskStatus::Created,
        "Planning" => TaskStatus::Planning,
        "InProgress" => TaskStatus::InProgress,
        "Paused" => TaskStatus::Paused,
        "Completed" => TaskStatus::Completed,
        "Failed" => TaskStatus::Failed,
        _ => {
            tracing::warn!(
                "Unknown task status '{status}' during graph backfill (mapped to Created)"
            );
            TaskStatus::Created
        }
    };
    parsed
}

#[cfg(feature = "storage")]
#[async_trait::async_trait]
impl GraphShadowSink for GraphStore {
    async fn shadow_persist(&self, task: &Task) {
        if let Err(e) = self.upsert_task_shadow(task).await {
            // Warn-only: the shadow path can never alter primary-path behavior.
            tracing::warn!("graph shadow persist failed: {}", e);
        }
    }

    /// Graph-plane verb: dispatch = `READY → SCHEDULED → RUNNING` + fresh
    /// attempt row. The envelope's transitional `attempt_id` is a
    /// *dispatch-level* counter; execution numbering is owned by the graph
    /// plane (`max(retry_no)+1`), so it is intentionally not consulted.
    /// Every failure mode is a no-op or a warn — the legacy path can never
    /// be failed by this fan-out.
    async fn on_schedule(&self, envelope: &ExecutionEnvelope, worker_id: Option<&str>) {
        match self
            .schedule_attempt(&envelope.graph_id, &envelope.node_id, worker_id)
            .await
        {
            Ok(Some(_)) => {}
            Ok(None) => tracing::debug!(
                graph_id = %envelope.graph_id,
                node_id = %envelope.node_id,
                "graph on_schedule no-op (node absent or not READY — shadow mirror may lag)"
            ),
            Err(e) => tracing::warn!("graph on_schedule failed: {}", e),
        }
    }

    async fn on_heartbeat(&self, envelope: &ExecutionEnvelope) {
        match self
            .running_attempt_id(&envelope.graph_id, &envelope.node_id)
            .await
        {
            Ok(Some(attempt)) => {
                if let Err(e) = self.heartbeat_attempt(&attempt).await {
                    tracing::warn!("graph on_heartbeat failed: {}", e);
                }
            }
            Ok(None) => tracing::debug!(
                graph_id = %envelope.graph_id,
                node_id = %envelope.node_id,
                "graph on_heartbeat no-op (no running attempt)"
            ),
            Err(e) => tracing::warn!("graph on_heartbeat probe failed: {}", e),
        }
    }

    async fn on_commit(&self, envelope: &ExecutionEnvelope, result_ref: Option<&str>) {
        // The winning attempt is the node's current RUNNING attempt (the
        // graph plane's own numbering); a commit with nothing running is
        // already fenced and commit_once records it as a `late_result`.
        let attempt = match self
            .running_attempt_id(&envelope.graph_id, &envelope.node_id)
            .await
        {
            Ok(Some(attempt)) => attempt,
            Ok(None) => attempt_id(&envelope.graph_id, &envelope.node_id, -1),
            Err(e) => {
                tracing::warn!("graph on_commit probe failed: {}", e);
                return;
            }
        };
        match self
            .commit_once(&envelope.graph_id, &envelope.node_id, &attempt, result_ref)
            .await
        {
            Ok(true) => {}
            Ok(false) => tracing::debug!(
                graph_id = %envelope.graph_id,
                node_id = %envelope.node_id,
                "graph on_commit fenced (late or lost commit-once) — late_result event recorded"
            ),
            Err(e) => tracing::warn!("graph on_commit failed: {}", e),
        }
    }

    async fn on_fail(&self, envelope: &ExecutionEnvelope, reason: &str) {
        let attempt = match self
            .running_attempt_id(&envelope.graph_id, &envelope.node_id)
            .await
        {
            Ok(Some(attempt)) => attempt,
            Ok(None) => attempt_id(&envelope.graph_id, &envelope.node_id, -1),
            Err(e) => {
                tracing::warn!("graph on_fail probe failed: {}", e);
                return;
            }
        };
        match self
            .fail_attempt(
                &envelope.graph_id,
                &envelope.node_id,
                &attempt,
                DEFAULT_MAX_ATTEMPTS,
                reason,
            )
            .await
        {
            Ok(_) => {}
            Err(e) => tracing::warn!("graph on_fail failed: {}", e),
        }
    }

    /// The real sweep: stale RUNNING attempts through the fail path with
    /// the shared default attempt budget (converges with the legacy
    /// dispatch-retry cap of 3 while T6 flips the authority). Warn-only on
    /// error — the reaper can never fail the monitor tick.
    async fn sweep_timeouts(
        &self,
        heartbeat_timeout: std::time::Duration,
    ) -> Vec<SweptAttempt> {
        match GraphStore::timeout_sweep(self, heartbeat_timeout, DEFAULT_MAX_ATTEMPTS).await {
            Ok(swept) => swept,
            Err(e) => {
                tracing::warn!("graph sweep_timeouts failed: {}", e);
                Vec::new()
            }
        }
    }
}

// ── Pure unit tests (no PG required) ─────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use uc_types::TaskId;

    #[test]
    fn status_tokens_cover_documented_mapping() {
        // Node states (PRD #638 mapping).
        assert_eq!(node_state_token("Pending"), "READY");
        assert_eq!(node_state_token("pending"), "READY");
        assert_eq!(node_state_token("Assigned"), "RUNNING");
        assert_eq!(node_state_token("InProgress"), "RUNNING");
        assert_eq!(node_state_token("in_progress"), "RUNNING");
        assert_eq!(node_state_token("running"), "RUNNING");
        assert_eq!(node_state_token("reviewing"), "RUNNING");
        assert_eq!(node_state_token("Completed"), "SUCCEEDED");
        assert_eq!(node_state_token("completed"), "SUCCEEDED");
        assert_eq!(node_state_token("Failed"), "FAILED");
        assert_eq!(node_state_token("cancelled"), "CANCELLED");
        assert_eq!(node_state_token("Conflicted"), "FAILED");
        // Graph statuses additionally:
        assert_eq!(graph_status_token("Created"), "CREATED");
        assert_eq!(graph_status_token("planning"), "PLANNING");
        assert_eq!(graph_status_token("Completed"), "SUCCEEDED");
        assert_eq!(graph_status_token("Cancelled"), "CANCELLED");
        // Unknown passthrough (never silently invented).
        assert_eq!(node_state_token("weird_state"), "WEIRD_STATE");
    }

    #[test]
    fn graph_status_of_rust_task_status_agrees_with_token_fn() {
        assert_eq!(
            graph_status_of_task_status(&TaskStatus::Completed),
            "SUCCEEDED"
        );
        assert_eq!(
            graph_status_of_task_status(&TaskStatus::InProgress),
            "RUNNING"
        );
        assert_eq!(
            graph_status_of_task_status(&TaskStatus::Planning),
            "PLANNING"
        );
        assert_eq!(graph_status_of_task_status(&TaskStatus::Paused), "PAUSED");
    }

    #[test]
    fn attempt_ids_are_deterministic() {
        assert_eq!(attempt_id("g1", "n1", 0), "g1:n1:0");
        assert_eq!(attempt_id("g1", "n1", 2), "g1:n1:2");
    }

    #[test]
    fn transition_ok_wires_the_state_machine_to_tokens() {
        // Legal edges in the exact tokens the row tables carry.
        assert!(transition_ok("CREATED", "READY"));
        assert!(transition_ok("READY", "SCHEDULED"));
        assert!(transition_ok("SCHEDULED", "RUNNING"));
        assert!(transition_ok("RUNNING", "SUCCEEDED"));
        assert!(transition_ok("RUNNING", "READY"), "fence re-arm edge");
        assert!(transition_ok("RUNNING", "FAILED"));
        assert!(transition_ok("FAILED", "READY"));
        // Illegal edges rejected.
        assert!(!transition_ok("CREATED", "SUCCEEDED"));
        assert!(!transition_ok("SUCCEEDED", "RUNNING"), "no resurrection");
        assert!(!transition_ok("READY", "READY"), "no self-loop");
        // T2-only shadow tokens and unknown passthroughs never transition —
        // the state machine refuses to move rows it does not understand.
        assert!(!transition_ok("PAUSED", "READY"));
        assert!(!transition_ok("WEIRD_STATE", "READY"));
        assert!(!transition_ok("READY", "PAUSED"));
        // lowercase (legacy wire form) is not the DB token vocabulary.
        assert!(!transition_ok("ready", "running"));
    }

    /// The T3 sink verbs are default no-ops: a shadow-only implementation
    /// (every T2 fake) keeps compiling and running unchanged, and calling
    /// the verbs is structurally incapable of failing the legacy path.
    #[derive(Default)]
    struct ShadowOnlySink {
        persisted: std::sync::Mutex<usize>,
    }

    #[async_trait::async_trait]
    impl GraphShadowSink for ShadowOnlySink {
        async fn shadow_persist(&self, _task: &uc_types::Task) {
            *self.persisted.lock().unwrap() += 1;
        }
    }

    #[tokio::test]
    async fn sink_verbs_default_to_noop() {
        let sink = ShadowOnlySink::default();
        let env = uc_types::ExecutionEnvelope::for_dispatch("g", "n", 0);
        // All four verbs resolve to their no-op defaults — nothing to
        // assert beyond "compiles and returns" (that IS the contract:
        // no graph plane required to keep the gateway compiling).
        sink.on_schedule(&env, Some("w1")).await;
        sink.on_heartbeat(&env).await;
        sink.on_commit(&env, Some("r")).await;
        sink.on_fail(&env, "boom").await;
        assert_eq!(*sink.persisted.lock().unwrap(), 0);
    }

    fn sample_task() -> Task {
        let task_id = TaskId("uc-1-t1".to_string());
        let done = Subtask {
            id: TaskId("uc-1-t1-f1".to_string()),
            parent_id: task_id.clone(),
            description: "write".into(),
            status: uc_types::SubtaskStatus::Completed,
            assigned_worker: Some(uc_types::WorkerId("w1".into())),
            depends_on: Vec::new(),
            file_constraints: Vec::new(),
            expected_output: String::new(),
            result: Some(uc_types::SubtaskResult {
                subtask_id: TaskId("uc-1-t1-f1".to_string()),
                worker_id: uc_types::WorkerId("w1".into()),
                modified_files: Vec::new(),
                summary: "did it".into(),
                success: true,
                completed_at: chrono::DateTime::from_timestamp(1717171717, 0).unwrap(),
                result: None,
            }),
            dispatch_mode: uc_types::DispatchMode::default(),
            effect_class: uc_types::EffectClass::default(),
            dispatch_retry_count: 0,
            retry_count: 1,
            required_capabilities: vec!["rust".into()],
            agent_config_json: None,
            steps: Vec::new(),
        };
        let running = Subtask {
            id: TaskId("uc-1-t1-f2".to_string()),
            parent_id: task_id.clone(),
            description: "review".into(),
            status: uc_types::SubtaskStatus::Assigned,
            assigned_worker: Some(uc_types::WorkerId("w2".into())),
            depends_on: vec![TaskId("uc-1-t1-f1".to_string())],
            file_constraints: Vec::new(),
            expected_output: String::new(),
            result: None,
            dispatch_mode: uc_types::DispatchMode::default(),
            effect_class: uc_types::EffectClass::default(),
            dispatch_retry_count: 0,
            retry_count: 0,
            required_capabilities: Vec::new(),
            agent_config_json: None,
            steps: Vec::new(),
        };
        Task {
            id: task_id.clone(),
            description: "demo".into(),
            project_id: "p1".into(),
            status: TaskStatus::InProgress,
            subtasks: vec![done, running],
            created_at: chrono::DateTime::from_timestamp(1717171000, 0).unwrap(),
            updated_at: chrono::DateTime::from_timestamp(1717171500, 0).unwrap(),
        }
    }

    #[test]
    fn project_task_maps_states_and_rows() {
        let p = project_task(&sample_task());
        assert_eq!(p.graph_id, "uc-1-t1");
        assert_eq!(p.status, "RUNNING");
        assert_eq!(p.nodes.len(), 2);
        assert_eq!(p.nodes[0].state, "SUCCEEDED");
        assert_eq!(p.nodes[1].state, "RUNNING");
        assert_eq!(p.nodes[1].dependencies, serde_json::json!(["uc-1-t1-f1"]));
        assert_eq!(
            p.nodes[0].required_capabilities,
            serde_json::json!(["rust"])
        );
        // Completed gets attempt @retry_no=1 + completion; Assigned gets attempt;
        // both in the same projection the writer commits transactionally.
        assert_eq!(p.attempts.len(), 2);
        assert_eq!(
            p.attempts[0].attempt_id,
            attempt_id("uc-1-t1", "uc-1-t1-f1", 1)
        );
        assert_eq!(p.attempts[0].status, "SUCCEEDED");
        assert!(p.attempts[0].finished_at.is_some());
        assert_eq!(p.attempts[1].status, "RUNNING");
        assert!(
            p.attempts[1].started_at.is_some(),
            "InProgress/Assigned→RUNNING attempt row with started_at"
        );
        assert_eq!(p.completions.len(), 1);
        assert_eq!(p.completions[0].node_id, "uc-1-t1-f1");
        assert_eq!(
            p.completions[0].winning_attempt_id,
            attempt_id("uc-1-t1", "uc-1-t1-f1", 1)
        );
    }

    #[test]
    fn ts_task_projects_like_its_rust_equivalent() {
        // The same logical graph expressed as TS PersistedTask JSON must
        // produce identical node ids/states/attempt ids (the camelCase→
        // snake conversion lives here, in the importer).
        let json = r#"{
            "id": "uc-1-t1",
            "description": "demo",
            "status": "in_progress",
            "controlState": "running",
            "projectId": "p1",
            "savedAt": 1717171500000,
            "createdAt": 1717171000000,
            "subtasks": [
                {"id": "uc-1-t1-f1", "description": "write", "status": "completed",
                 "dependsOn": [], "result": "did it", "retryCount": 1,
                 "completedAt": 1717171717000, "requiredCapabilities": ["rust"]},
                {"id": "uc-1-t1-f2", "description": "review", "status": "assigned",
                 "dependsOn": ["uc-1-t1-f1"], "startedAt": 1717171500000}
            ]
        }"#;
        let ts: TsPersistedTask = serde_json::from_str(json).unwrap();
        let p = project_ts_task(&ts);
        assert_eq!(p.status, "RUNNING");
        assert_eq!(p.project_id, "p1");
        let states: Vec<(&str, &str)> = p
            .nodes
            .iter()
            .map(|n| (n.node_id.as_str(), n.state.as_str()))
            .collect();
        assert_eq!(
            states,
            vec![("uc-1-t1-f1", "SUCCEEDED"), ("uc-1-t1-f2", "RUNNING"),]
        );
        assert_eq!(p.attempts.len(), 2);
        assert_eq!(
            p.attempts[0].attempt_id,
            attempt_id("uc-1-t1", "uc-1-t1-f1", 1)
        );
        assert_eq!(p.completions.len(), 1);
        assert_eq!(p.nodes[1].dependencies, serde_json::json!(["uc-1-t1-f1"]));
        assert_eq!(
            p.nodes[0].required_capabilities,
            serde_json::json!(["rust"])
        );
    }

    #[test]
    fn checkpoint_newer_savedat_wins_otherwise_task_file() {
        let base = || TsPersistedTask {
            id: "t".into(),
            status: "in_progress".into(),
            saved_at: Some(100),
            ..Default::default()
        };
        let cp = |saved: Option<i64>| TsPersistedTask {
            id: "t".into(),
            status: "completed".into(),
            saved_at: saved,
            ..Default::default()
        };
        // Newer checkpoint wins.
        assert_eq!(
            pick_newer_saved(base(), Some(cp(Some(200)))).status,
            "completed"
        );
        // Older/equal checkpoint loses (task file is the newer write).
        assert_eq!(
            pick_newer_saved(base(), Some(cp(Some(100)))).status,
            "in_progress"
        );
        assert_eq!(
            pick_newer_saved(base(), Some(cp(Some(99)))).status,
            "in_progress"
        );
        // Legacy checkpoint without savedAt counts as 0 → task file wins.
        assert_eq!(
            pick_newer_saved(base(), Some(cp(None))).status,
            "in_progress"
        );
        // No checkpoint → task file.
        assert_eq!(pick_newer_saved(base(), None).status, "in_progress");
    }

    #[test]
    fn pending_nodes_get_no_attempt_rows() {
        let task_id = TaskId("t".to_string());
        let st = Subtask {
            id: TaskId("n".to_string()),
            parent_id: task_id.clone(),
            description: String::new(),
            status: uc_types::SubtaskStatus::Pending,
            assigned_worker: None,
            depends_on: Vec::new(),
            file_constraints: Vec::new(),
            expected_output: String::new(),
            result: None,
            dispatch_mode: uc_types::DispatchMode::default(),
            effect_class: uc_types::EffectClass::default(),
            dispatch_retry_count: 0,
            retry_count: 0,
            required_capabilities: Vec::new(),
            agent_config_json: None,
            steps: Vec::new(),
        };
        let task = Task {
            id: task_id,
            description: String::new(),
            project_id: "p".into(),
            status: TaskStatus::Planning,
            subtasks: vec![st],
            created_at: chrono::Utc::now(),
            updated_at: chrono::Utc::now(),
        };
        let p = project_task(&task);
        assert_eq!(p.nodes[0].state, "READY");
        assert!(p.attempts.is_empty());
        assert!(p.completions.is_empty());
    }
}
