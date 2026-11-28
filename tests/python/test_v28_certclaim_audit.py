from __future__ import annotations

from ultimate_coders.acceptance.certclaim_audit import (
    CertClaimAuditor,
    ChannelMeasurement,
    CriteriaExposureGuard,
    CrossAgentFormalAuditor,
    EvaluationCriteria,
    FormalAuditRecord,
    certclaim_audit_enabled,
)


def test_flag_is_opt_in(monkeypatch):
    monkeypatch.delenv("UC_CERTCLAIM_AUDIT", raising=False)
    assert certclaim_audit_enabled() is False
    monkeypatch.setenv("UC_CERTCLAIM_AUDIT", "1")
    assert certclaim_audit_enabled() is True


def test_authorized_deployment_with_calibrated_channels_is_accepted():
    certificate = CertClaimAuditor().audit(
        provider="acme",
        model="acme-large",
        deployment="prod-a",
        authorized_deployments=("prod-a", "prod-b"),
        channels=(
            ChannelMeasurement("math", 0.8, 0.79, 0.02, 100),
            ChannelMeasurement("code", 0.7, 0.71, 0.02, 100),
        ),
        error_bound_limit=0.1,
    )
    assert certificate.accepted is True
    assert certificate.reasons == ()
    assert abs(certificate.max_drift - 0.01) < 1e-9


def test_unauthorized_deployment_is_rejected():
    certificate = CertClaimAuditor().audit(
        provider="acme",
        model="acme-large",
        deployment="prod-x",
        authorized_deployments=("prod-a",),
        channels=(ChannelMeasurement("math", 0.8, 0.8, 0.02, 100),),
        required_channels=1,
    )
    assert certificate.accepted is False
    assert "deployment_not_authorized" in certificate.reasons


def test_channel_drift_and_insufficient_channels_are_rejected():
    certificate = CertClaimAuditor().audit(
        provider="acme",
        model="acme-large",
        deployment="prod-a",
        authorized_deployments=("prod-a",),
        channels=(
            ChannelMeasurement("math", 0.8, 0.9, 0.02, 10),
            ChannelMeasurement("code", 0.7, 0.71, 0.02, 10),
        ),
        required_channels=3,
        error_bound_limit=1.0,
    )
    assert certificate.accepted is False
    assert "insufficient_channels" in certificate.reasons
    assert "channel_drift:math" in certificate.reasons


def test_multi_channel_error_bound_is_enforced():
    certificate = CertClaimAuditor().audit(
        provider="acme",
        model="acme-large",
        deployment="prod-a",
        authorized_deployments=("prod-a",),
        channels=(ChannelMeasurement("math", 0.8, 0.85, 0.1, 1),),
        required_channels=1,
        error_bound_limit=0.1,
    )
    assert certificate.accepted is False
    assert "error_bound_exceeded" in certificate.reasons


def test_duplicate_channels_and_invalid_inputs_are_rejected():
    auditor = CertClaimAuditor()
    try:
        auditor.audit(
            provider="acme",
            model="m",
            deployment="d",
            authorized_deployments=("d",),
            channels=(
                ChannelMeasurement("math", 0.8, 0.8, 0.02, 1),
                ChannelMeasurement("math", 0.8, 0.8, 0.02, 1),
            ),
        )
    except ValueError as error:
        assert "duplicate channel" in str(error)
    else:
        raise AssertionError("expected duplicate channel error")

    for kwargs in (
        dict(provider="", model="m", deployment="d"),
        dict(provider="p", model="", deployment="d"),
        dict(provider="p", model="m", deployment=""),
    ):
        try:
            auditor.audit(
                authorized_deployments=("d",),
                channels=(ChannelMeasurement("math", 0.8, 0.8, 0.02, 1),),
                **kwargs,
            )
        except ValueError:
            pass
        else:
            raise AssertionError("expected empty-field error")


def test_formal_auditor_accepts_only_fully_audited_proofs():
    report = CrossAgentFormalAuditor().audit(
        (
            FormalAuditRecord("impl", "verify", "s1", True, 0, True, True),
            FormalAuditRecord("impl", "verify", "s2", True, 0, True, False),
        )
    )
    assert [r.spec_id for r in report.trustworthy] == ["s1"]
    assert [r.spec_id for r in report.rejected] == ["s2"]
    assert report.consistency == 0.5


def test_direct_spec_predicate_call_invalidates_a_green_proof():
    report = CrossAgentFormalAuditor().audit(
        (FormalAuditRecord("impl", "verify", "s1", True, 1, True, True),)
    )
    assert report.all_trustworthy is False
    assert report.rejected[0].spec_id == "s1"


def test_duplicate_specs_and_empty_formal_input_are_rejected():
    auditor = CrossAgentFormalAuditor()
    try:
        auditor.audit(())
    except ValueError:
        pass
    else:
        raise AssertionError("expected empty input error")

    try:
        auditor.audit(
            (
                FormalAuditRecord("impl", "verify", "s1", True, 0, True, True),
                FormalAuditRecord("impl2", "verify2", "s1", True, 0, True, True),
            )
        )
    except ValueError as error:
        assert "duplicate" in str(error)
    else:
        raise AssertionError("expected duplicate spec error")


def test_hidden_criteria_are_not_exposed_to_the_agent():
    guard = CriteriaExposureGuard()
    criteria = (
        EvaluationCriteria("hidden-rule", hidden=True, exposed_to_agent=False),
        EvaluationCriteria("public-rule", hidden=False, exposed_to_agent=True),
    )
    assert guard.safe(criteria) is True
    exposed = (EvaluationCriteria("hidden-rule", hidden=True, exposed_to_agent=True),)
    assert guard.safe(exposed) is False
    assert guard.exposed(exposed) == ("hidden-rule",)


def test_certificate_serialisation_contains_reasons():
    certificate = CertClaimAuditor().audit(
        provider="acme",
        model="m",
        deployment="d",
        authorized_deployments=(),
        channels=(ChannelMeasurement("math", 0.8, 0.8, 0.02, 100),),
        required_channels=1,
    )
    payload = certificate.to_dict()
    assert payload["accepted"] is False
    assert "deployment_not_authorized" in payload["reasons"]
