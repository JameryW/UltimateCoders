//! Affinity placement scoring (T12 #654, D12 #649).
//!
//! Dispatch used to be capability-gated only: the JetStream queue hands a
//! message to whoever claims it first, so a node that touches `src/auth.rs`
//! could land on a worker that has never seen that file. This module is the
//! gateway-side score that decides **where a node goes first** — a soft
//! preference, never a gate.
//!
//! # Placement dimensions (in precedence order)
//!
//! 1. **capability match** — hard gate, unchanged (applied by
//!    [`crate::worker_service::WorkerRegistry::dispatch_gate`] before scoring).
//! 2. **scope** — hard gate, T8 #650 (same gate).
//! 3. **file affinity** — `|node.file_constraints ∩ worker.recent_files|`,
//!    more is better. Paths are normalised (`\` → `/`, `./` stripped) so a
//!    Windows-reporting worker and a POSIX-reported node still match.
//! 4. **load** — `current_load / max_capacity`, lower is better.
//! 5. **locality** — same host as the workers already running this task's
//!    sibling nodes (host read from the registration metadata's `hostname`
//!    key, a stable API key since #607). Same host wins.
//!
//! `worker_id` ascending is the final tie-break so the choice is
//! deterministic — `WorkerRegistry` iterates a `HashMap`, and without a
//! total order two equal-scoring candidates would alternate between ticks.
//!
//! Because load is compared **before** locality, the lowest-load candidate
//! wins among equally affine workers — this is precisely D12's "tie-break
//! lowest load".
//!
//! # Soft semantics
//!
//! [`place`] returns `None` whenever no candidate clears
//! [`MIN_AFFINITY_HITS`] (or none declared a per-worker topic / none is
//! available). The caller then publishes to the shared subject — every node
//! stays dispatchable through overflow, and a scoring outage, a stale
//! heartbeat, or a legacy worker degrades to exactly the pre-T12 behavior.
//! A node is never stranded by a score.

/// Subject prefix for a worker's dedicated dispatch subject.
pub const PER_WORKER_SUBJECT_PREFIX: &str = "uc.subtask.execute.w.";

/// Stream filter covering every per-worker subject.
///
/// The `UC_SUBTASKS` stream must list this **in addition to** the bare
/// `uc.subtask.execute`: `.>` requires at least one further token, so it
/// never matches the shared subject itself.
///
/// The consumer side of the same contract lives in the worker
/// (`NatsWorker._ensure_subtask_transport`): on a work-queue stream JetStream
/// admits exactly one unfiltered consumer and refuses a filtered consumer
/// that overlaps one already present, so the shared overflow consumer must
/// be pinned to the bare `uc.subtask.execute`. Left unfiltered it covers
/// this wildcard too — which both makes every per-worker consumer illegal
/// and lets the overflow queue take targeted messages. Getting that wrong is
/// silent: the worker logs a warning, declares `per_worker_topic = false`
/// forever, and [`crate::worker_service::WorkerRegistry::placement_target`]
/// then finds no candidate, degrading every dispatch to the shared subject.
pub const PER_WORKER_SUBJECT_WILDCARD: &str = "uc.subtask.execute.w.>";

/// Upper bound applied to a worker's advertised recent-files list.
pub const MAX_RECENT_FILES: usize = 64;

/// Minimum file overlap for a node to be targeted at a worker at all.
///
/// Zero overlap means affinity gives no reason to prefer one worker over
/// another, and the shared queue is strictly better than picking a random
/// worker (it cannot strand the node on a worker that dies between the
/// score and the fetch). Load and locality therefore decide *among*
/// affinity-bearing candidates.
pub const MIN_AFFINITY_HITS: usize = 1;

/// The dispatch subject reserved for one worker.
pub fn per_worker_subject(worker_id: &str) -> String {
    format!("{PER_WORKER_SUBJECT_PREFIX}{worker_id}")
}

/// Normalize one path for affinity comparison.
///
/// Backslashes become forward slashes (a worker on Windows reports
/// `src\auth.rs`) and a leading `./` is stripped. Everything else is kept
/// byte-for-byte — no canonicalisation, no case folding: two spellings of
/// the same file are the caller's problem, and guessing wrong is worse than
/// missing a hit.
pub fn normalize_path(path: &str) -> String {
    let replaced = path.trim().replace('\\', "/");
    let mut out = replaced.as_str();
    while let Some(rest) = out.strip_prefix("./") {
        out = rest;
    }
    out.to_string()
}

/// Bound + de-duplicate a worker's advertised recent files.
///
/// Trims, drops blanks, removes duplicates (first-seen order preserved) and
/// truncates to [`MAX_RECENT_FILES`]. The wire is untrusted input: a worker
/// could advertise an unbounded list and make every dispatch O(files ×
/// candidates).
pub fn normalize_recent_files(raw: &[String]) -> Vec<String> {
    let mut out: Vec<String> = Vec::with_capacity(raw.len().min(MAX_RECENT_FILES));
    for entry in raw {
        if out.len() >= MAX_RECENT_FILES {
            break;
        }
        let norm = normalize_path(entry);
        if norm.is_empty() || out.iter().any(|e| e == &norm) {
            continue;
        }
        out.push(norm);
    }
    out
}

/// How many of the node's file constraints the worker has recently touched.
///
/// Counts DISTINCT constraints: the score doubles as an ordering key, so a
/// caller that repeats a path (or a node that declares both `src/a.rs` and
/// `./src/a.rs`) must not inflate it.
pub fn affinity_hits(file_constraints: &[String], recent_files: &[String]) -> usize {
    if file_constraints.is_empty() || recent_files.is_empty() {
        return 0;
    }
    let mut matched: Vec<String> = Vec::new();
    for constraint in file_constraints {
        let norm = normalize_path(constraint);
        if norm.is_empty() || matched.iter().any(|m| m == &norm) {
            continue;
        }
        if recent_files.iter().any(|r| r == &norm) {
            matched.push(norm);
        }
    }
    matched.len()
}

/// Host of a worker, read from its registration metadata JSON.
///
/// The key is the stable `hostname` field Python's `_registration_metadata`
/// has always emitted. A worker that never registered it (or sent
/// non-JSON) contributes no locality signal — `None`, never an error.
pub fn host_from_metadata(metadata: &str) -> Option<String> {
    if metadata.is_empty() {
        return None;
    }
    let value: serde_json::Value = serde_json::from_str(metadata).ok()?;
    let host = value.get("hostname")?.as_str()?.trim();
    if host.is_empty() {
        None
    } else {
        Some(host.to_string())
    }
}

/// One dispatch candidate, reduced to exactly what scoring reads.
#[derive(Debug, Clone)]
pub struct PlacementCandidate<'a> {
    pub worker_id: &'a str,
    /// Registration metadata (parsed lazily for the `hostname` key).
    pub metadata: &'a str,
    pub recent_files: &'a [String],
    pub current_load: u32,
    pub max_capacity: u32,
}

impl PlacementCandidate<'_> {
    /// Load as a percentage of capacity (mirrors the registry's accounting:
    /// a zero-capacity worker counts as fully loaded).
    pub fn load_percent(&self) -> u32 {
        if self.max_capacity == 0 {
            return 100;
        }
        (self.current_load * 100) / self.max_capacity
    }
}

/// The chosen target for one node.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Placement {
    pub worker_id: String,
    /// Subject the caller must publish to.
    pub subject: String,
    /// File-overlap count that justified the choice (≥ [`MIN_AFFINITY_HITS`]).
    pub affinity_hits: usize,
    pub load_percent: u32,
    /// Whether the worker shares a host with a sibling node's worker.
    pub same_host: bool,
}

/// Pick where a node should go first, or `None` to use the shared subject.
///
/// `file_constraints` is the node's own declared file set;
/// `sibling_hosts` are the hosts of the workers already running this task's
/// other nodes (empty when nothing is placed yet — locality is then neutral).
pub fn place(
    candidates: &[PlacementCandidate<'_>],
    file_constraints: &[String],
    sibling_hosts: &std::collections::HashSet<String>,
) -> Option<Placement> {
    let sibling_hosts: std::collections::HashSet<&str> =
        sibling_hosts.iter().map(|h| h.as_str()).collect();

    candidates
        .iter()
        .filter_map(|c| {
            let hits = affinity_hits(file_constraints, c.recent_files);
            if hits < MIN_AFFINITY_HITS {
                return None;
            }
            let host = host_from_metadata(c.metadata);
            Some(Placement {
                worker_id: c.worker_id.to_string(),
                subject: per_worker_subject(c.worker_id),
                affinity_hits: hits,
                load_percent: c.load_percent(),
                same_host: host.as_deref().is_some_and(|h| sibling_hosts.contains(h)),
            })
        })
        .min_by(|a, b| {
            // affinity desc → load asc → locality desc → worker_id asc.
            b.affinity_hits
                .cmp(&a.affinity_hits)
                .then_with(|| a.load_percent.cmp(&b.load_percent))
                .then_with(|| b.same_host.cmp(&a.same_host))
                .then_with(|| a.worker_id.cmp(&b.worker_id))
        })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashSet;

    fn files(items: &[&str]) -> Vec<String> {
        items.iter().map(|s| s.to_string()).collect()
    }

    fn meta(host: &str) -> String {
        format!(r#"{{"hostname":"{host}","pid":1}}"#)
    }

    #[test]
    fn subject_format_is_pinned() {
        // Cross-language contract: the Python worker builds the same string
        // for its consumer's filter_subject (pytest pins the twin golden).
        assert_eq!(
            per_worker_subject("worker-7"),
            "uc.subtask.execute.w.worker-7"
        );
        assert_eq!(PER_WORKER_SUBJECT_PREFIX, "uc.subtask.execute.w.");
        assert_eq!(PER_WORKER_SUBJECT_WILDCARD, "uc.subtask.execute.w.>");
    }

    #[test]
    fn wildcard_is_a_strict_extension_of_the_shared_subject() {
        // A NATS `>` needs at least one further token, so `.w.>` never
        // captures `uc.subtask.execute` — the stream must list BOTH, or
        // overflow messages fall outside the stream entirely.
        const SHARED: &str = "uc.subtask.execute";
        assert_eq!(per_worker_subject("w1"), format!("{SHARED}.w.w1"));
        assert_eq!(PER_WORKER_SUBJECT_WILDCARD, format!("{SHARED}.w.>"));
        assert!(per_worker_subject("w1").starts_with(&format!("{SHARED}.")));
        assert!(
            !SHARED.contains(".w."),
            "shared subject is not a per-worker one"
        );
    }

    #[test]
    fn path_normalization_tolerates_windows_separators() {
        assert_eq!(
            normalize_path("src\\agent\\worker.py"),
            "src/agent/worker.py"
        );
        assert_eq!(normalize_path("./src/main.rs"), "src/main.rs");
        assert_eq!(normalize_path("  src/main.rs  "), "src/main.rs");
    }

    #[test]
    fn affinity_matches_across_separator_styles() {
        let recent = normalize_recent_files(&files(&["src\\auth.rs"]));
        assert_eq!(affinity_hits(&files(&["src/auth.rs"]), &recent), 1);
    }

    #[test]
    fn affinity_counts_distinct_constraints_only() {
        let recent = normalize_recent_files(&files(&["src/auth.rs", "src/lib.rs"]));
        assert_eq!(
            affinity_hits(
                &files(&["src/auth.rs", "src/auth.rs", "src/lib.rs", "other.rs"]),
                &recent
            ),
            2
        );
    }

    #[test]
    fn affinity_is_zero_for_empty_inputs() {
        assert_eq!(affinity_hits(&[], &files(&["a"])), 0);
        assert_eq!(affinity_hits(&files(&["a"]), &[]), 0);
    }

    #[test]
    fn recent_files_are_bounded_deduped_and_cleaned() {
        let mut raw = files(&["a.rs", " a.rs ", "", "  ", "b.rs", "./c.rs"]);
        raw.extend((0..MAX_RECENT_FILES + 10).map(|i| format!("gen{i}.rs")));
        let norm = normalize_recent_files(&raw);
        assert_eq!(norm.len(), MAX_RECENT_FILES, "bounded");
        assert_eq!(norm[0], "a.rs");
        assert_eq!(norm[1], "b.rs");
        assert_eq!(norm[2], "c.rs");
    }

    #[test]
    fn host_parsed_from_registration_metadata() {
        assert_eq!(host_from_metadata(&meta("box-a")).as_deref(), Some("box-a"));
        assert_eq!(host_from_metadata(""), None);
        assert_eq!(host_from_metadata("not json"), None);
        assert_eq!(host_from_metadata("{}"), None);
        assert_eq!(host_from_metadata(r#"{"hostname":"  "}"#), None);
    }

    fn one_worker<'a>(recent: &'a [String], metadata: &'a str) -> Vec<PlacementCandidate<'a>> {
        vec![PlacementCandidate {
            worker_id: "w1",
            metadata,
            recent_files: recent,
            current_load: 1,
            max_capacity: 4,
        }]
    }

    #[test]
    fn overlapping_worker_is_targeted() {
        let metadata = meta("box-a");
        let recent = normalize_recent_files(&files(&["src/auth.rs"]));
        let candidates = one_worker(&recent, &metadata);
        let picked = place(&candidates, &files(&["src/auth.rs"]), &HashSet::new())
            .expect("overlap above threshold must be targeted");
        assert_eq!(picked.worker_id, "w1");
        assert_eq!(picked.subject, "uc.subtask.execute.w.w1");
        assert_eq!(picked.affinity_hits, 1);
    }

    #[test]
    fn zero_overlap_falls_back_to_overflow() {
        let metadata = meta("box-a");
        let recent = normalize_recent_files(&files(&["src/unrelated.rs"]));
        let candidates = one_worker(&recent, &metadata);
        assert_eq!(
            place(&candidates, &files(&["src/auth.rs"]), &HashSet::new()),
            None
        );
    }

    #[test]
    fn node_without_file_constraints_is_never_targeted() {
        let metadata = meta("box-a");
        let recent = normalize_recent_files(&files(&["src/auth.rs"]));
        let candidates = one_worker(&recent, &metadata);
        assert_eq!(place(&candidates, &[], &HashSet::new()), None);
    }

    #[test]
    fn no_candidates_falls_back_to_overflow() {
        assert_eq!(place(&[], &files(&["src/auth.rs"]), &HashSet::new()), None);
    }

    #[test]
    fn higher_affinity_wins_over_lower_load() {
        let (ma, mb) = (meta("box-a"), meta("box-b"));
        let (ra, rb) = (
            normalize_recent_files(&files(&["src/auth.rs", "src/lib.rs"])),
            normalize_recent_files(&files(&["src/auth.rs"])),
        );
        let candidates = vec![
            PlacementCandidate {
                worker_id: "busy-but-relevant",
                metadata: &ma,
                recent_files: &ra,
                current_load: 3,
                max_capacity: 4,
            },
            PlacementCandidate {
                worker_id: "idle-but-less-relevant",
                metadata: &mb,
                recent_files: &rb,
                current_load: 0,
                max_capacity: 4,
            },
        ];
        let picked = place(
            &candidates,
            &files(&["src/auth.rs", "src/lib.rs"]),
            &HashSet::new(),
        )
        .unwrap();
        assert_eq!(picked.worker_id, "busy-but-relevant");
        assert_eq!(picked.affinity_hits, 2);
    }

    #[test]
    fn load_breaks_ties_before_locality() {
        // D12 "tie-break lowest load": equal affinity → the lower-load
        // worker wins even when the other one shares a sibling host.
        let (ma, mb) = (meta("box-a"), meta("box-b"));
        let ra = normalize_recent_files(&files(&["src/auth.rs"]));
        let rb = ra.clone();
        let candidates = vec![
            PlacementCandidate {
                worker_id: "local-but-loaded",
                metadata: &ma,
                recent_files: &ra,
                current_load: 3,
                max_capacity: 4,
            },
            PlacementCandidate {
                worker_id: "remote-but-idle",
                metadata: &mb,
                recent_files: &rb,
                current_load: 0,
                max_capacity: 4,
            },
        ];
        let mut siblings = HashSet::new();
        siblings.insert("box-a".to_string());
        let picked = place(&candidates, &files(&["src/auth.rs"]), &siblings).unwrap();
        assert_eq!(picked.worker_id, "remote-but-idle");
        assert!(!picked.same_host);
    }

    #[test]
    fn locality_breaks_ties_when_load_is_equal() {
        let (ma, mb) = (meta("box-a"), meta("box-b"));
        let ra = normalize_recent_files(&files(&["src/auth.rs"]));
        let rb = ra.clone();
        let candidates = vec![
            PlacementCandidate {
                worker_id: "elsewhere",
                metadata: &ma,
                recent_files: &ra,
                current_load: 1,
                max_capacity: 4,
            },
            PlacementCandidate {
                worker_id: "same-host",
                metadata: &mb,
                recent_files: &rb,
                current_load: 1,
                max_capacity: 4,
            },
        ];
        let mut siblings = HashSet::new();
        siblings.insert("box-b".to_string());
        let picked = place(&candidates, &files(&["src/auth.rs"]), &siblings).unwrap();
        assert_eq!(picked.worker_id, "same-host");
        assert!(picked.same_host);
    }

    #[test]
    fn identical_scores_break_on_worker_id_for_determinism() {
        let (ma, mb) = (meta("box-a"), meta("box-a"));
        let ra = normalize_recent_files(&files(&["src/auth.rs"]));
        let rb = ra.clone();
        // Deliberately ordered worst-first: the winner must not depend on
        // the caller's iteration order.
        let candidates = vec![
            PlacementCandidate {
                worker_id: "w-z",
                metadata: &mb,
                recent_files: &rb,
                current_load: 2,
                max_capacity: 4,
            },
            PlacementCandidate {
                worker_id: "w-a",
                metadata: &ma,
                recent_files: &ra,
                current_load: 2,
                max_capacity: 4,
            },
        ];
        let picked = place(&candidates, &files(&["src/auth.rs"]), &HashSet::new()).unwrap();
        assert_eq!(picked.worker_id, "w-a");
    }

    #[test]
    fn zero_capacity_candidate_counts_as_fully_loaded() {
        let metadata = meta("box-a");
        let recent = normalize_recent_files(&files(&["src/auth.rs"]));
        let candidates = vec![PlacementCandidate {
            worker_id: "w0",
            metadata: &metadata,
            recent_files: &recent,
            current_load: 0,
            max_capacity: 0,
        }];
        let picked = place(&candidates, &files(&["src/auth.rs"]), &HashSet::new()).unwrap();
        assert_eq!(picked.load_percent, 100);
    }

    #[test]
    fn worker_without_host_metadata_still_placed() {
        let recent = normalize_recent_files(&files(&["src/auth.rs"]));
        let candidates = vec![PlacementCandidate {
            worker_id: "w1",
            metadata: "",
            recent_files: &recent,
            current_load: 1,
            max_capacity: 4,
        }];
        let picked = place(&candidates, &files(&["src/auth.rs"]), &HashSet::new()).unwrap();
        assert!(!picked.same_host);
    }
}
