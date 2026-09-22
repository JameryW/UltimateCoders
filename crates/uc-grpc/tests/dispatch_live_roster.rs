//! Gateway dispatch decisions on a live RPC-built roster.
//!
//! Scope A (S1): prove `dispatch_gate` / `placement_target` decide on a roster
//! built purely through real `RegisterWorker` / `WorkerHeartbeat` RPCs against
//! a real `GrpcServer` — not a hand-constructed in-process `WorkerRegistry`.
//!
//! The cloned registry handle is READ-ONLY in the assertions: every roster row
//! must arrive via RPC. No NATS, no sleeps beyond the harness 100ms pattern.
//!
//! Wire-fidelity tripwire: worker `affine-twin` carries the same capabilities,
//! load, and affinity signal as `live-producer` but declares
//! `per_worker_topic=false`. Its id sorts before the producer's, so it would
//! win any affinity tie — the `placement_target` assertion only stays green
//! while the server honors the per-worker-topic filter the heartbeat RPC
//! populates. (Verified by temporary mutation: flipping the twin's heartbeat
//! to `per_worker_topic=true` turns the test red.)

use std::collections::HashSet;
use std::sync::Arc;

use tokio::sync::RwLock;
use tonic::transport::Server;
use uc_engine::LocalEngine;
use uc_grpc::client::GrpcEngineClient;
use uc_grpc::server::GrpcServer;
use uc_grpc::worker_service::{ReviewIndependence, WorkerDispatchGate, WorkerRegistry};
use uc_types::CONTRACT_VERSION;

const PRODUCER: &str = "live-producer";
const REVIEWER: &str = "live-reviewer";
/// Sorts before [`PRODUCER`]: wins any affinity tie when NOT filtered out by
/// the per-worker-topic gate — which is exactly what makes it a tripwire.
const TWIN: &str = "affine-twin";
const LEGACY: &str = "live-legacy";

fn s(v: &str) -> String {
    v.to_string()
}

/// Start a real GrpcServer on a random port (all 4 services, fallback engine)
/// and return its endpoint plus a clone of the live worker-registry handle.
///
/// Must be cloned BEFORE `into_services()` consumes the server value.
async fn start_server_with_registry() -> (String, Arc<RwLock<WorkerRegistry>>) {
    let engine = LocalEngine::new_fallback();
    let grpc_server = GrpcServer::new(engine);
    let registry = grpc_server.worker_registry().clone();
    let (engine_service, task_service, dashboard_service, worker_service) =
        grpc_server.into_services();

    // Use port 0 to let OS pick a free port
    let addr: std::net::SocketAddr = "127.0.0.1:0".parse().unwrap();
    let listener = tokio::net::TcpListener::bind(addr).await.unwrap();
    let actual_addr = listener.local_addr().unwrap();

    tokio::spawn(async move {
        Server::builder()
            .add_service(engine_service)
            .add_service(task_service)
            .add_service(dashboard_service)
            .add_service(worker_service)
            .serve_with_incoming(tokio_stream::wrappers::TcpListenerStream::new(listener))
            .await
            .unwrap();
    });

    // Give the server a moment to start
    tokio::time::sleep(std::time::Duration::from_millis(100)).await;

    (format!("http://{}", actual_addr), registry)
}

/// Build the canonical roster purely via RPCs: producer (`code`), reviewer
/// (`code` + `review`), an affine-but-topic-less twin (`code`), and a legacy
/// worker (empty contract version). Placement signals arrive via heartbeats.
async fn build_live_roster(client: &GrpcEngineClient) {
    let version = Some(CONTRACT_VERSION);
    let open: &[String] = &[];

    assert!(
        client
            .register_worker(
                PRODUCER,
                &[s("code")],
                4,
                Some(r#"{"hostname":"h1","pid":123}"#),
                version,
                open,
            )
            .await
            .unwrap(),
        "producer registration via RPC must succeed"
    );
    assert!(
        client
            .register_worker(REVIEWER, &[s("code"), s("review")], 4, None, version, open)
            .await
            .unwrap(),
        "reviewer registration via RPC must succeed"
    );
    assert!(
        client
            .register_worker(TWIN, &[s("code")], 4, None, version, open)
            .await
            .unwrap(),
        "twin registration via RPC must succeed"
    );
    // Legacy worker: empty contract version is accepted (observability) but
    // must never become dispatchable.
    assert!(
        client
            .register_worker(LEGACY, &[s("code")], 4, None, None, open)
            .await
            .unwrap(),
        "legacy registration via RPC must be accepted"
    );

    assert!(
        client
            .worker_heartbeat(PRODUCER, 1, version, &[s("src/foo.rs")], true)
            .await
            .unwrap(),
        "producer heartbeat via RPC must be accepted"
    );
    assert!(
        client
            .worker_heartbeat(REVIEWER, 1, version, &[s("docs/notes.md")], true)
            .await
            .unwrap(),
        "reviewer heartbeat via RPC must be accepted"
    );
    // Tripwire heartbeat: same affinity signal and load as the producer, but
    // NO per-worker topic — the affinity gate must filter it out.
    assert!(
        client
            .worker_heartbeat(TWIN, 1, version, &[s("src/foo.rs")], false)
            .await
            .unwrap(),
        "twin heartbeat via RPC must be accepted"
    );
    assert!(
        client
            .worker_heartbeat(LEGACY, 0, None, &[], false)
            .await
            .unwrap(),
        "legacy heartbeat via RPC must be accepted"
    );
}

#[tokio::test]
async fn live_roster_dispatch_gate() {
    let (endpoint, registry) = start_server_with_registry().await;
    let client = GrpcEngineClient::connect(&endpoint).await.unwrap();
    build_live_roster(&client).await;

    let reg = registry.read().await;

    // Nobody holds this capability → keep Pending.
    assert_eq!(
        reg.dispatch_gate(&[s("nonexistent-cap")], "", &ReviewIndependence::default()),
        WorkerDispatchGate::NoCapableWorker,
    );

    // T19 through the wire: every review-capable worker produced a dependency
    // of this review node (producer + reviewer both excluded) → no
    // independent reviewer. Every roster row arrived via RPC.
    let mut excluded_both = HashSet::new();
    excluded_both.insert(PRODUCER.to_string());
    excluded_both.insert(REVIEWER.to_string());
    let both_producers = ReviewIndependence {
        excluded_producers: excluded_both,
        unknown_producers: Vec::new(),
    };
    assert_eq!(
        reg.dispatch_gate(&[s("review")], "", &both_producers),
        WorkerDispatchGate::NoIndependentReviewer {
            producers: vec![PRODUCER.to_string(), REVIEWER.to_string()],
        },
    );

    // Precision: excluding only the producer still leaves the reviewer
    // eligible — the gate removes the worst shape, nothing more.
    let mut excluded_producer = HashSet::new();
    excluded_producer.insert(PRODUCER.to_string());
    let producer_only = ReviewIndependence {
        excluded_producers: excluded_producer,
        unknown_producers: Vec::new(),
    };
    assert_eq!(
        reg.dispatch_gate(&[s("review")], "", &producer_only),
        WorkerDispatchGate::Dispatch,
    );
}

#[tokio::test]
async fn live_roster_affinity_placement() {
    let (endpoint, registry) = start_server_with_registry().await;
    let client = GrpcEngineClient::connect(&endpoint).await.unwrap();
    build_live_roster(&client).await;

    let reg = registry.read().await;
    let empty: HashSet<String> = HashSet::new();

    // Constrained on producer-touched files → the affine worker. The twin
    // carries the identical signal but no per-worker topic, so it must never
    // be targeted (wire-fidelity tripwire: it would win the tie on worker id
    // if the topic filter were dropped).
    let target = reg.placement_target(&[s("code")], "", &[s("src/foo.rs")], &empty, &empty);
    assert!(target.is_some(), "affine node must have a placement target");
    assert_eq!(target.unwrap().worker_id, PRODUCER);

    // Zero overlap in affinity mode → None (shared fallback), never a guess.
    assert_eq!(
        reg.placement_target(
            &[s("code")],
            "",
            &[s("elsewhere/unrelated.rs")],
            &empty,
            &empty
        ),
        None,
    );
}

#[tokio::test]
async fn live_roster_contract_version_gates() {
    let (endpoint, registry) = start_server_with_registry().await;
    let client = GrpcEngineClient::connect(&endpoint).await.unwrap();
    build_live_roster(&client).await;

    // Non-empty mismatch is refused loudly at the handshake, never enters
    // the roster.
    assert!(
        !client
            .register_worker(
                "live-mismatch",
                &[s("code")],
                4,
                None,
                Some("v0-bogus"),
                &[]
            )
            .await
            .unwrap(),
        "mismatched contract version via RPC must be refused"
    );
    assert!(
        !client
            .worker_heartbeat(PRODUCER, 1, Some("v0-bogus"), &[], true)
            .await
            .unwrap(),
        "mismatched heartbeat version via RPC must be refused"
    );

    let reg = registry.read().await;
    let empty: HashSet<String> = HashSet::new();

    // Legacy worker (empty version) is registered but never dispatchable;
    // the refused worker never entered the roster at all.
    let mut candidate_ids: Vec<String> = reg
        .dispatch_candidates(&[s("code")], "", &empty)
        .iter()
        .map(|w| w.id.clone())
        .collect();
    candidate_ids.sort();
    assert_eq!(
        candidate_ids,
        vec![TWIN.to_string(), PRODUCER.to_string(), REVIEWER.to_string()],
    );

    // The refused heartbeat left the producer's placement signal intact.
    let target = reg.placement_target(&[s("code")], "", &[s("src/foo.rs")], &empty, &empty);
    assert_eq!(target.unwrap().worker_id, PRODUCER);
}
