//! Durable stores for [`WorkCase`] and [`Mandate`].
//!
//! Both are JSON documents in the governance record store:
//!
//! * `work_case/<case_id>` — the case aggregate (status, runs, subscriptions).
//! * `mandate/<mandate_id>` — issued authority, kept forever so revocation leaves
//!   an audit trail.
//!
//! All mutations are compare-and-swap loops. A concurrent writer that loses the
//! race re-reads and retries (bounded) rather than overwriting a newer status — the
//! one thing a governance store must never do is silently revert a transition to
//! `needs_human`.

use std::sync::Arc;

use chrono::{DateTime, Utc};

use uc_types::governance::{
    CaseId, CaseStatus, CaseTransitionError, Mandate, MandateId, MandateRevocation, PolicyVersion,
    Principal, WorkCase, CASE_NAMESPACE,
};

use super::record_store::{NamespacedStore, RecordError, RecordStore};

/// Record-store namespace for mandates.
pub const MANDATE_NAMESPACE: &str = "mandate";

/// Failure modes of the case store.
#[derive(Debug, Clone, PartialEq)]
pub enum CaseStoreError {
    Store(RecordError),
    NotFound {
        case_id: String,
    },
    /// The case state machine refused the transition (illegal edge, terminal,
    /// quiescent, run already open, or no mandate).
    Transition(CaseTransitionError),
    /// A bounded CAS loop never converged; another writer is hammering the case.
    Contended {
        case_id: String,
    },
}

impl From<RecordError> for CaseStoreError {
    fn from(error: RecordError) -> Self {
        CaseStoreError::Store(error)
    }
}

impl std::fmt::Display for CaseStoreError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            CaseStoreError::Store(error) => write!(f, "case store: {error}"),
            CaseStoreError::NotFound { case_id } => write!(f, "case {case_id} not found"),
            CaseStoreError::Transition(error) => write!(f, "case transition rejected: {error}"),
            CaseStoreError::Contended { case_id } => {
                write!(f, "case {case_id} is contended; retry later")
            }
        }
    }
}

impl std::error::Error for CaseStoreError {}

/// Durable work cases.
#[derive(Clone, Debug)]
pub struct CaseStore {
    view: NamespacedStore,
}

impl CaseStore {
    pub fn new(records: Arc<dyn RecordStore>) -> Self {
        Self {
            view: NamespacedStore::new(records, CASE_NAMESPACE),
        }
    }

    pub fn is_durable(&self) -> bool {
        self.view.is_durable()
    }

    /// Create the case unless its id already exists.
    ///
    /// `case.id` is deterministic for a source thread (see
    /// [`CaseId::from_parts`]), so this is the dedup mechanism behind "the same PR
    /// updates the original case instead of creating a new one".
    pub async fn create_unique(
        &self,
        mut case: WorkCase,
    ) -> Result<(WorkCase, bool), CaseStoreError> {
        case.version = 1;
        match self.view.put(case.id.as_str(), &case, Some(0)).await {
            Ok(_) => Ok((case, false)),
            Err(RecordError::Conflict { .. }) => match self.get(case.id.as_str()).await? {
                Some((existing, _version)) => Ok((existing, true)),
                None => Err(CaseStoreError::Store(RecordError::Conflict {
                    expected: 0,
                    actual: None,
                })),
            },
            Err(other) => Err(CaseStoreError::Store(other)),
        }
    }

    pub async fn get(&self, case_id: &str) -> Result<Option<(WorkCase, u64)>, CaseStoreError> {
        Ok(self.view.get::<WorkCase>(case_id).await?)
    }

    pub async fn list(&self, limit: usize) -> Result<Vec<(WorkCase, u64)>, CaseStoreError> {
        Ok(self.view.list(limit).await?)
    }

    /// Find the case tracking a source thread.
    ///
    /// A linear scan is deliberate: the number of *active* cases is small (tens),
    /// and a secondary index would be a second source of truth to keep consistent.
    /// The deterministic id makes the common path a direct `get`; this exists for
    /// legacy cases created before thread-derived ids.
    pub async fn find_by_thread(
        &self,
        thread_key: &str,
        limit: usize,
    ) -> Result<Option<WorkCase>, CaseStoreError> {
        let cases = self.view.list::<WorkCase>(limit).await?;
        Ok(cases
            .into_iter()
            .map(|(case, _version)| case)
            .find(|case| case.origin.thread_key.as_deref() == Some(thread_key)))
    }

    /// Apply a mutation under CAS, retrying a bounded number of times.
    pub async fn update<F, T>(
        &self,
        case_id: &str,
        mutate: F,
    ) -> Result<(WorkCase, T), CaseStoreError>
    where
        F: Fn(&mut WorkCase) -> Result<T, CaseTransitionError>,
    {
        for _ in 0..16 {
            let Some((mut case, version)) = self.get(case_id).await? else {
                return Err(CaseStoreError::NotFound {
                    case_id: case_id.to_string(),
                });
            };
            let outcome = mutate(&mut case).map_err(CaseStoreError::Transition)?;
            case.version = version + 1;
            match self.view.put(case_id, &case, Some(version)).await {
                Ok(_) => return Ok((case, outcome)),
                Err(RecordError::Conflict { .. }) => continue,
                Err(other) => return Err(CaseStoreError::Store(other)),
            }
        }
        Err(CaseStoreError::Contended {
            case_id: case_id.to_string(),
        })
    }

    /// Transition a case and return the new document.
    pub async fn transition(
        &self,
        case_id: &str,
        status: CaseStatus,
        now: DateTime<Utc>,
    ) -> Result<WorkCase, CaseStoreError> {
        self.update(case_id, |case| case.transition(status, now))
            .await
            .map(|(case, _)| case)
    }

    /// Link an execution run to the case.
    pub async fn open_run(
        &self,
        case_id: &str,
        task_id: &str,
        submission_id: Option<String>,
        now: DateTime<Utc>,
    ) -> Result<WorkCase, CaseStoreError> {
        self.update(case_id, |case| {
            // An open run implies the case is executing; promote `open` to
            // `active` first so the state machine stays coherent.
            if case.status == CaseStatus::Open {
                case.transition(CaseStatus::Active, now)?;
            }
            case.open_run(task_id, submission_id.clone(), now)
                .map(|_| ())
        })
        .await
        .map(|(case, _)| case)
    }

    /// Finish a run by id.
    pub async fn finish_run(
        &self,
        case_id: &str,
        run_id: &str,
        outcome: uc_types::governance::CaseRunOutcome,
        detail: Option<String>,
        now: DateTime<Utc>,
    ) -> Result<WorkCase, CaseStoreError> {
        self.update(case_id, |case| {
            let run = case
                .runs
                .iter()
                .find(|run| run.run_id.0 == run_id)
                .map(|run| run.run_id.clone())
                .ok_or(CaseTransitionError::UnknownRun)?;
            case.finish_run(&run, outcome, detail.clone(), now)
        })
        .await
        .map(|(case, _)| case)
    }

    pub async fn delete(&self, case_id: &str) -> Result<bool, CaseStoreError> {
        Ok(self.view.delete(case_id).await?)
    }

    /// Count cases per status — the dashboard's case board.
    pub async fn status_counts(
        &self,
        limit: usize,
    ) -> Result<Vec<(CaseStatus, usize)>, CaseStoreError> {
        let cases = self.view.list::<WorkCase>(limit).await?;
        let mut counts: Vec<(CaseStatus, usize)> = Vec::new();
        for (case, _version) in cases {
            match counts.iter_mut().find(|(status, _)| *status == case.status) {
                Some((_, count)) => *count += 1,
                None => counts.push((case.status, 1)),
            }
        }
        Ok(counts)
    }
}

/// Failure modes of the mandate store.
#[derive(Debug, Clone, PartialEq)]
pub enum MandateStoreError {
    Store(RecordError),
    NotFound { mandate_id: String },
    Contended { mandate_id: String },
}

impl From<RecordError> for MandateStoreError {
    fn from(error: RecordError) -> Self {
        MandateStoreError::Store(error)
    }
}

impl std::fmt::Display for MandateStoreError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            MandateStoreError::Store(error) => write!(f, "mandate store: {error}"),
            MandateStoreError::NotFound { mandate_id } => {
                write!(f, "mandate {mandate_id} not found")
            }
            MandateStoreError::Contended { mandate_id } => {
                write!(f, "mandate {mandate_id} is contended; retry later")
            }
        }
    }
}

impl std::error::Error for MandateStoreError {}

/// Durable mandates.
#[derive(Clone, Debug)]
pub struct MandateStore {
    view: NamespacedStore,
}

impl MandateStore {
    pub fn new(records: Arc<dyn RecordStore>) -> Self {
        Self {
            view: NamespacedStore::new(records, MANDATE_NAMESPACE),
        }
    }

    pub fn is_durable(&self) -> bool {
        self.view.is_durable()
    }

    /// Persist a newly issued mandate. Fails if the id already exists (ids are
    /// random UUIDs, so a collision means a programming error, not a retry).
    pub async fn put_new(&self, mandate: &Mandate) -> Result<(), MandateStoreError> {
        self.view
            .put(mandate.id.as_str(), mandate, Some(0))
            .await
            .map(|_| ())
            .map_err(MandateStoreError::Store)
    }

    pub async fn get(&self, mandate_id: &str) -> Result<Option<(Mandate, u64)>, MandateStoreError> {
        Ok(self.view.get::<Mandate>(mandate_id).await?)
    }

    /// The newest mandate for a case (there is at most one live mandate at a time;
    /// re-issuing supersedes it by being the most recently issued).
    pub async fn latest_for_case(
        &self,
        case_id: &str,
        limit: usize,
    ) -> Result<Option<Mandate>, MandateStoreError> {
        let mandates = self.view.list::<Mandate>(limit).await?;
        Ok(mandates
            .into_iter()
            .map(|(mandate, _version)| mandate)
            .filter(|mandate| mandate.case_id == case_id)
            .max_by_key(|mandate| mandate.issued_at))
    }

    pub async fn list(&self, limit: usize) -> Result<Vec<(Mandate, u64)>, MandateStoreError> {
        Ok(self.view.list(limit).await?)
    }

    /// Revoke in place. Idempotent: an already-revoked mandate keeps its original
    /// revocation record.
    pub async fn revoke(
        &self,
        mandate_id: &str,
        by: Principal,
        reason: &str,
        now: DateTime<Utc>,
    ) -> Result<Mandate, MandateStoreError> {
        for _ in 0..16 {
            let Some((mut mandate, version)) = self.get(mandate_id).await? else {
                return Err(MandateStoreError::NotFound {
                    mandate_id: mandate_id.to_string(),
                });
            };
            if mandate.revoked.is_some() {
                return Ok(mandate);
            }
            mandate.revoke(by.clone(), reason, now);
            match self.view.put(mandate_id, &mandate, Some(version)).await {
                Ok(_) => return Ok(mandate),
                Err(RecordError::Conflict { .. }) => continue,
                Err(other) => return Err(MandateStoreError::Store(other)),
            }
        }
        Err(MandateStoreError::Contended {
            mandate_id: mandate_id.to_string(),
        })
    }

    /// Build a revocation record without touching storage (used by tests and by
    /// the audit view).
    pub fn revocation(by: Principal, reason: &str, at: DateTime<Utc>) -> MandateRevocation {
        MandateRevocation {
            at,
            by,
            reason: reason.to_string(),
        }
    }

    /// Convenience constructor for a human-issued case mandate.
    pub fn human_mandate(
        case_id: &str,
        grantor: Principal,
        scopes: Vec<uc_types::governance::MandateScope>,
        policy_version: PolicyVersion,
        now: DateTime<Utc>,
    ) -> Mandate {
        Mandate::new(
            case_id,
            grantor,
            uc_types::governance::MandateOrigin::HumanIssued,
            scopes,
            policy_version,
            now,
        )
    }
}

/// A case id that has never been used, for callers that do not have one yet.
pub fn new_case_id() -> CaseId {
    CaseId::new()
}

/// The mandate id type, re-exported for call sites that only import this module.
pub type CaseMandateId = MandateId;

#[cfg(test)]
mod tests {
    use super::*;
    use crate::governance::record_store::InMemoryRecordStore;
    use uc_types::governance::{CaseOrigin, CaseRunOutcome, MandateScope};

    fn now() -> DateTime<Utc> {
        DateTime::parse_from_rfc3339("2026-01-01T00:00:00Z")
            .unwrap()
            .with_timezone(&Utc)
    }

    fn stores() -> (CaseStore, MandateStore) {
        let records: Arc<dyn RecordStore> = Arc::new(InMemoryRecordStore::new());
        (CaseStore::new(records.clone()), MandateStore::new(records))
    }

    async fn case_with_mandate(cases: &CaseStore, mandates: &MandateStore) -> WorkCase {
        let case = WorkCase::new("t", "d", "proj", CaseOrigin::manual(), now());
        let (case, _) = cases.create_unique(case).await.unwrap();
        let mandate = MandateStore::human_mandate(
            case.id.as_str(),
            Principal::user("alice"),
            vec![MandateScope::Action {
                class: uc_types::governance::ActionClass::Write,
            }],
            PolicyVersion(1),
            now(),
        );
        mandates.put_new(&mandate).await.unwrap();
        let mut case = case;
        case.mandate_id = Some(mandate.id.0.clone());
        cases
            .update(case.id.as_str(), |stored| {
                stored.mandate_id = Some(mandate.id.0.clone());
                Ok(())
            })
            .await
            .unwrap();
        case
    }

    #[tokio::test]
    async fn create_unique_converges_on_the_same_thread() {
        let (cases, _mandates) = stores();
        let first = WorkCase::new("t", "d", "proj", CaseOrigin::manual(), now())
            .with_id(CaseId::from_parts("github", "acme/widgets#1"));
        let second = WorkCase::new("t", "d", "proj", CaseOrigin::manual(), now())
            .with_id(CaseId::from_parts("github", "acme/widgets#1"));
        let (stored, replayed) = cases.create_unique(first).await.unwrap();
        assert!(!replayed);
        let (again, replayed) = cases.create_unique(second).await.unwrap();
        assert!(replayed);
        assert_eq!(stored.id, again.id);
        assert_eq!(cases.list(10).await.unwrap().len(), 1);
    }

    #[tokio::test]
    async fn open_and_finish_run_drives_the_state_machine() {
        let (cases, mandates) = stores();
        let case = case_with_mandate(&cases, &mandates).await;
        let updated = cases
            .open_run(case.id.as_str(), "task-1", Some("sub-1".to_string()), now())
            .await
            .unwrap();
        assert_eq!(updated.status, CaseStatus::Active);
        assert_eq!(updated.runs.len(), 1);
        let run_id = updated.runs[0].run_id.0.clone();
        let verifying = cases
            .finish_run(
                case.id.as_str(),
                &run_id,
                CaseRunOutcome::Succeeded,
                None,
                now(),
            )
            .await
            .unwrap();
        assert_eq!(verifying.status, CaseStatus::Verifying);
        let completed = cases
            .transition(case.id.as_str(), CaseStatus::Completed, now())
            .await
            .unwrap();
        assert!(completed.status.is_terminal());
    }

    #[tokio::test]
    async fn illegal_transition_is_rejected_without_writing() {
        let (cases, mandates) = stores();
        let case = case_with_mandate(&cases, &mandates).await;
        let error = cases
            .transition(case.id.as_str(), CaseStatus::Completed, now())
            .await
            .expect_err("open -> completed is illegal");
        assert!(matches!(error, CaseStoreError::Transition(_)));
        let (stored, version) = cases.get(case.id.as_str()).await.unwrap().unwrap();
        assert_eq!(stored.status, CaseStatus::Open);
        assert_eq!(version, 2, "failed transition must not bump the version");
    }

    #[tokio::test]
    async fn case_without_mandate_cannot_open_a_run() {
        let (cases, _mandates) = stores();
        let case = WorkCase::new("t", "d", "proj", CaseOrigin::manual(), now());
        let (case, _) = cases.create_unique(case).await.unwrap();
        let error = cases
            .open_run(case.id.as_str(), "task-1", None, now())
            .await
            .expect_err("no mandate");
        assert!(matches!(error, CaseStoreError::Transition(_)));
    }

    #[tokio::test]
    async fn mandate_revocation_is_idempotent_and_sticky() {
        let (_cases, mandates) = stores();
        let mandate = MandateStore::human_mandate(
            "case-1",
            Principal::user("alice"),
            vec![MandateScope::Action {
                class: uc_types::governance::ActionClass::Read,
            }],
            PolicyVersion(1),
            now(),
        );
        mandates.put_new(&mandate).await.unwrap();
        let revoked = mandates
            .revoke(
                mandate.id.as_str(),
                Principal::user("alice"),
                "changed mind",
                now(),
            )
            .await
            .unwrap();
        assert!(revoked.is_revoked());
        let again = mandates
            .revoke(
                mandate.id.as_str(),
                Principal::user("bob"),
                "second reason",
                now(),
            )
            .await
            .unwrap();
        assert_eq!(
            again.revoked.as_ref().map(|r| r.reason.as_str()),
            Some("changed mind"),
            "the first revocation is authoritative"
        );
    }

    #[tokio::test]
    async fn latest_mandate_for_case_is_the_newest_one() {
        let (_cases, mandates) = stores();
        let older = MandateStore::human_mandate(
            "case-1",
            Principal::user("alice"),
            vec![],
            PolicyVersion(1),
            now(),
        );
        mandates.put_new(&older).await.unwrap();
        let newer = MandateStore::human_mandate(
            "case-1",
            Principal::user("alice"),
            vec![],
            PolicyVersion(2),
            now() + chrono::Duration::hours(1),
        );
        mandates.put_new(&newer).await.unwrap();
        let latest = mandates
            .latest_for_case("case-1", 10)
            .await
            .unwrap()
            .unwrap();
        assert_eq!(latest.id, newer.id);
    }

    #[tokio::test]
    async fn status_counts_group_cases() {
        let (cases, _mandates) = stores();
        for index in 0..3 {
            let mut case = WorkCase::new("t", "d", "proj", CaseOrigin::manual(), now());
            case.id = CaseId(format!("case-{index}"));
            if index == 0 {
                case.status = CaseStatus::NeedsHuman;
            }
            cases.create_unique(case).await.unwrap();
        }
        let counts = cases.status_counts(10).await.unwrap();
        let open = counts
            .iter()
            .find(|(status, _)| *status == CaseStatus::Open)
            .map(|(_, count)| *count)
            .unwrap_or_default();
        assert_eq!(open, 2);
    }
}
