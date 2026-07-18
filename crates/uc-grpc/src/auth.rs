//! gRPC auth interceptor for protecting business services with a shared bearer token.
//!
//! When `UC_DASHBOARD_TOKEN` is set (non-empty), all four business services
//! (EngineService / TaskService / DashboardService / WorkerService) require an
//! `Authorization: Bearer <token>` header that matches the configured token.
//! The standard tonic_health service is intentionally NOT protected so kube /
//! docker health probes keep working.
//!
//! When the configured token is empty, the interceptor is a no-op (allows all
//! requests) — preserving backwards compatibility for dev / local deployments
//! that do not set `UC_DASHBOARD_TOKEN`.

use std::sync::Arc;

use tonic::service::Interceptor;
use tonic::{Request, Status};

/// The expected `Authorization` header scheme prefix.
const BEARER_PREFIX: &str = "Bearer ";

/// An auth interceptor validating `Authorization: Bearer <token>` against a
/// shared secret.
///
/// Clone-cheap (inner is `Arc<str>`), so a single instance can be cloned per
/// service when wiring up `with_interceptor`. When `expected` is empty, every
/// request is allowed (no gate — backwards compat).
#[derive(Clone)]
pub struct AuthInterceptor {
    /// The shared bearer token. Empty = no auth gate (allow all).
    expected: Arc<str>,
}

impl AuthInterceptor {
    /// Create a new interceptor expecting the given bearer token.
    ///
    /// An empty `expected` disables the gate (all requests pass).
    pub fn new(expected: Arc<str>) -> Self {
        Self { expected }
    }

    /// Returns `true` when auth is disabled (no token configured).
    pub fn is_disabled(&self) -> bool {
        self.expected.is_empty()
    }
}

impl Interceptor for AuthInterceptor {
    fn call(&mut self, request: Request<()>) -> Result<Request<()>, Status> {
        // No token configured → open access (backwards compat).
        if self.expected.is_empty() {
            return Ok(request);
        }

        let header = request
            .metadata()
            .get("authorization")
            .and_then(|v| v.to_str().ok());

        let provided = match header {
            Some(h) => h,
            None => {
                return Err(Status::unauthenticated(
                    "missing Authorization header (expected Bearer token)",
                ));
            }
        };

        if constant_time_eq_bearer(provided, &self.expected) {
            Ok(request)
        } else {
            Err(Status::unauthenticated("invalid bearer token"))
        }
    }
}

/// Constant-time comparison of a `Bearer <provided>` header against the
/// expected token.
///
/// Compares the full `Bearer <token>` form so a missing/extra scheme prefix
/// also fails. We do not short-circuit on the first mismatched byte — we
/// accumulate a difference flag across the entire comparison and check length
/// equality separately, so timing does not leak how much of the token matched.
fn constant_time_eq_bearer(provided_header: &str, expected_token: &str) -> bool {
    // Build the expected full header value: "Bearer <token>".
    // We compare byte-by-byte without early return.
    let expected_header: String = format!("{BEARER_PREFIX}{expected_token}");

    let p = provided_header.as_bytes();
    let e = expected_header.as_bytes();

    if p.len() != e.len() {
        return false;
    }

    let mut diff: u8 = 0;
    for (pb, eb) in p.iter().zip(e.iter()) {
        diff |= pb ^ eb;
    }
    diff == 0
}

#[cfg(test)]
mod tests {
    use super::*;
    use tonic::metadata::MetadataValue;

    fn make_request(auth_header: Option<&str>) -> Request<()> {
        let mut req = Request::new(());
        if let Some(val) = auth_header {
            req.metadata_mut()
                .insert("authorization", MetadataValue::try_from(val).unwrap());
        }
        req
    }

    #[test]
    fn token_configured_correct_bearer_passes() {
        let mut interceptor = AuthInterceptor::new(Arc::from("s3cret"));
        let req = make_request(Some("Bearer s3cret"));
        assert!(interceptor.call(req).is_ok());
    }

    #[test]
    fn token_configured_wrong_bearer_rejected() {
        let mut interceptor = AuthInterceptor::new(Arc::from("s3cret"));
        let req = make_request(Some("Bearer wrong"));
        let err = interceptor.call(req).unwrap_err();
        assert_eq!(err.code(), tonic::Code::Unauthenticated);
    }

    #[test]
    fn token_configured_missing_header_rejected() {
        let mut interceptor = AuthInterceptor::new(Arc::from("s3cret"));
        let req = make_request(None);
        let err = interceptor.call(req).unwrap_err();
        assert_eq!(err.code(), tonic::Code::Unauthenticated);
    }

    #[test]
    fn token_configured_missing_bearer_scheme_rejected() {
        let mut interceptor = AuthInterceptor::new(Arc::from("s3cret"));
        // Header present but no "Bearer " prefix.
        let req = make_request(Some("s3cret"));
        let err = interceptor.call(req).unwrap_err();
        assert_eq!(err.code(), tonic::Code::Unauthenticated);
    }

    #[test]
    fn empty_token_allows_all() {
        let mut interceptor = AuthInterceptor::new(Arc::from(""));
        // No header.
        assert!(interceptor.call(make_request(None)).is_ok());
        // Wrong header still passes (gate disabled).
        assert!(interceptor.call(make_request(Some("Bearer anything"))).is_ok());
    }

    #[test]
    fn empty_token_is_disabled() {
        let interceptor = AuthInterceptor::new(Arc::from(""));
        assert!(interceptor.is_disabled());
    }

    #[test]
    fn nonempty_token_is_enabled() {
        let interceptor = AuthInterceptor::new(Arc::from("tok"));
        assert!(!interceptor.is_disabled());
    }

    #[test]
    fn constant_time_eq_matches_full_bearer_form() {
        assert!(constant_time_eq_bearer("Bearer abc", "abc"));
        assert!(!constant_time_eq_bearer("Bearer abc", "abd"));
        assert!(!constant_time_eq_bearer("Bearer abc", "abc "));
        assert!(!constant_time_eq_bearer("abc", "abc"));
        assert!(!constant_time_eq_bearer("Bearer  abc", "abc"));
    }
}
