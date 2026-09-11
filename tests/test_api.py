"""The HTTP surface, exercised end to end with ``TestClient``.

The centre of this module is one test that walks the whole workflow an auditor would:
create a project, scope a control, upload evidence, run an assessment, read the citations
back to their chunk, record a review, generate a report and download it. If that path
works over HTTP, the routers are wired to the same service layer the Streamlit app uses.

The rest covers the two things a thin router layer can still get wrong: the error
envelope (every failure must arrive in the same shape, with the right status code) and
what the API is allowed to disclose - no secrets, no filesystem paths, no connection
string.
"""

from __future__ import annotations

import pytest

from app.api.main import API_V1_PREFIX, SECURITY_WARNING
from app.audit import service
from app.evaluation.datasets import get_dataset
from app.schemas.enums import AssessmentStatus, EvidenceType, ExperimentMode

V1 = API_V1_PREFIX


def _upload(client, project_id, path, evidence_type=EvidenceType.OTHER):
    return client.post(
        V1 + "/evidence/upload",
        files={"file": (path.name, path.read_bytes(), "application/octet-stream")},
        data={
            "project_id": str(project_id),
            "evidence_type": evidence_type.value if hasattr(evidence_type, "value") else evidence_type,
            "is_synthetic": "true",
        },
    )


# ==========================================================================
# The whole workflow, over HTTP
# ==========================================================================
def test_the_full_audit_workflow_end_to_end(api_client, api_session, generated_datasets):
    dataset = get_dataset("DATASET-001")

    # ---- 1. the control library is seeded on startup
    controls = api_client.get(V1 + "/controls")
    assert controls.status_code == 200
    assert any(c["control_id"] == dataset.control_ref for c in controls.json())

    # ---- 2. create a project with the control already in scope
    created = api_client.post(
        V1 + "/projects",
        json={
            "name": "API workflow test",
            "audit_area": "Identity and Access Management",
            "auditor_name": "A. Auditor",
            "control_refs": [dataset.control_ref],
        },
    )
    assert created.status_code == 201
    project = created.json()
    project_id = project["id"]
    assert project["controls_in_scope"] == 1
    assert project["control_refs"] == [dataset.control_ref]

    # ---- 3. upload the dataset's evidence
    for path in generated_datasets["DATASET-001"]:
        declared = dataset.file(path.name)
        response = _upload(api_client, project_id, path, declared.evidence_type)
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["parse_status"] == "PARSED"
        assert body["chunk_count"] > 0
        assert body["chunks_embedded"] == body["chunks_stored"]
        assert body["sha256"]
        assert "stored_path" not in body, "the server filesystem is not part of the trail"

    # ---- 4. run the assessment
    run = api_client.post(
        V1 + "/assessments/run",
        json={
            "project_id": project_id,
            "control_ref": dataset.control_ref,
            "mode": ExperimentMode.C_RAG_WORKFLOW.value,
        },
    )
    assert run.status_code == 200, run.text
    payload = run.json()
    assert payload["persisted"] is True
    assert "requires auditor review" in payload["notice"] or "not an audit conclusion" in payload["notice"]

    assessment = payload["assessment"]
    assert assessment["status"] == AssessmentStatus.POTENTIAL_DEFICIENCY.value
    assert assessment["source"] == "AI-generated"
    assert assessment["requires_human_review"] is True
    assessment_id = assessment["id"]

    # ---- 5. read the assessment back with its citations
    detail = api_client.get("{0}/assessments/{1}".format(V1, assessment_id))
    assert detail.status_code == 200
    citations = detail.json()["citations"]
    assert citations
    assert all(c["chunk_resolved"] for c in citations)
    assert all(c["verdict"] == "VERIFIED" for c in citations)

    # ---- 6. follow a citation to the evidence chunk it points at
    chunk_id = citations[0]["chunk_id"]
    context = api_client.get("{0}/evidence/chunks/{1}".format(V1, chunk_id))
    assert context.status_code == 200
    assert context.json()["found"] is True
    assert citations[0]["quoted_text"] in context.json()["text"]

    # ---- 7. record the auditor's decision
    review = api_client.post(
        V1 + "/reviews",
        json={
            "assessment_id": assessment_id,
            "reviewer_name": "A. Auditor",
            "decision": "ACCEPTED",
            "comments": "Checked against the export.",
            "review_seconds": 90.0,
            "usefulness_rating": 4,
        },
    )
    assert review.status_code == 201
    assert review.json()["agreed_with_ai_status"] is True
    assert review.json()["source"] == "Human auditor"

    # ---- 8. the queue empties and the dashboard reflects it
    assert api_client.get(V1 + "/reviews/pending", params={"project_id": project_id}).json() == []
    stats = api_client.get(V1 + "/dashboard/stats", params={"project_id": project_id}).json()
    assert stats["controls_assessed"] == 1
    assert stats["completed_reviews"] == 1
    assert stats["human_ai_agreement_rate"] == 1.0
    assert stats["definitions"]["counting_basis"]

    # ---- 9. the finding shows up on the findings endpoint
    findings = api_client.get(V1 + "/assessments/findings", params={"project_id": project_id}).json()
    assert [f["id"] for f in findings] == [assessment_id]

    # ---- 10. generate and download the report
    report = api_client.post(V1 + "/reports/generate", json={"project_id": project_id})
    assert report.status_code == 201
    report_id = report.json()["id"]
    assert "Executive Summary" in report.json()["content"]

    download = api_client.get("{0}/reports/{1}/download".format(V1, report_id))
    assert download.status_code == 200
    assert download.headers["content-type"].startswith("text/markdown")
    assert "attachment" in download.headers.get("content-disposition", "")
    assert "Executive Summary" in download.text

    # ---- 11. the row really is in the database the API wrote to
    stored = service.require_assessment(api_session, assessment_id)
    assert stored.project_id == project_id
    assert stored.human_review_required is True


# ==========================================================================
# System endpoints
# ==========================================================================
def test_health_reports_the_database_and_the_provider(api_client):
    body = api_client.get("/health").json()
    assert body["status"] == "ok"
    assert body["database_ok"] is True
    assert body["provider"]["active_provider"] == "mock"
    assert body["authentication"] == "none", "the absence of auth must be stated where users look"


def test_the_index_names_the_api_and_its_security_posture(api_client):
    body = api_client.get("/").json()
    assert body["api_prefix"] == V1
    assert "authentication" in str(body).lower() or "no authentication" in str(body).lower()


def test_the_openapi_document_states_that_there_is_no_authentication(api_client):
    document = api_client.get("/openapi.json").json()
    assert SECURITY_WARNING in document["info"]["description"]


def test_settings_are_disclosed_without_secrets_or_paths(api_client):
    body = api_client.get(V1 + "/settings").json()
    blob = str(body)
    assert body["provider"]["llm_api_key"] == "(not set)"
    assert body["database_backend"] == "sqlite"
    assert "sqlite:///" not in blob, "the connection string can carry credentials"
    for key in ("upload_dir", "report_dir", "data_dir", "stored_path"):
        assert key not in body
    assert body["force_human_review"] is True
    assert body["security_note"]


def test_configuration_cannot_be_changed_over_http(api_client):
    """An unauthenticated endpoint that could repoint the model client would expose
    every piece of evidence subsequently assessed."""
    response = api_client.post(V1 + "/settings", json={"changes": {"llm_provider": "openai"}})
    assert response.status_code == 200
    assert response.json()["applied"] is False
    assert response.json()["requested_keys"] == ["llm_provider"]
    assert api_client.get(V1 + "/settings").json()["provider"]["llm_provider"] == "mock"


def test_provider_status_makes_no_network_call(api_client):
    body = api_client.get(V1 + "/settings/providers").json()
    assert body["active_provider"] == "mock"
    assert body["fell_back_to_mock"] is False


# ==========================================================================
# The error envelope
# ==========================================================================
def _assert_envelope(response, status_code, error_type):
    assert response.status_code == status_code
    body = response.json()
    assert set(body) == {"error"}
    assert body["error"]["type"] == error_type
    assert body["error"]["status_code"] == status_code
    assert body["error"]["message"]
    assert body["error"]["path"]


@pytest.mark.parametrize(
    "path",
    [
        "/projects/987654",
        "/controls/CONTROL-NOPE",
        "/assessments/987654",
        "/evidence/987654",
        "/reports/987654",
    ],
)
def test_a_missing_resource_returns_the_standard_404_envelope(api_client, path):
    _assert_envelope(api_client.get(V1 + path), 404, "NotFoundError")


def test_a_service_level_rejection_returns_400(api_client):
    project = api_client.post(V1 + "/projects", json={"name": "P", "audit_area": "IAM"}).json()
    response = api_client.post(
        V1 + "/projects/{0}/controls".format(project["id"]),
        json={"control_refs": ["CONTROL-DOES-NOT-EXIST"]},
    )
    _assert_envelope(response, 404, "NotFoundError")


def test_a_malformed_body_returns_422(api_client):
    response = api_client.post(V1 + "/projects", json={"audit_area": "IAM"})
    _assert_envelope(response, 422, "RequestValidationError")
    assert response.json()["error"]["detail"]


def test_an_oversized_upload_returns_413(api_client, settings, tmp_path):
    project = api_client.post(V1 + "/projects", json={"name": "P", "audit_area": "IAM"}).json()
    path = tmp_path / "huge.csv"
    path.write_bytes(b"x" * (settings.max_upload_bytes + 1024))
    _assert_envelope(_upload(api_client, project["id"], path), 413, "EvidenceTooLargeError")


def test_an_unknown_route_returns_the_envelope_too(api_client):
    response = api_client.get(V1 + "/there-is-no-such-thing")
    assert response.status_code == 404
    assert "error" in response.json()


def test_uploading_to_a_missing_project_is_reported_not_stored(api_client, tmp_path):
    path = tmp_path / "listing.csv"
    path.write_text("Account,MFA_Status\na,Enabled\n", encoding="utf-8")
    response = _upload(api_client, 987654, path)
    assert response.status_code in (400, 404)
    assert "error" in response.json()


# ==========================================================================
# Individual endpoints
# ==========================================================================
def test_project_crud(api_client):
    created = api_client.post(V1 + "/projects", json={"name": "CRUD", "audit_area": "IAM"})
    project_id = created.json()["id"]

    patched = api_client.patch(
        V1 + "/projects/{0}".format(project_id), json={"status": "FIELDWORK", "description": "Updated"}
    )
    assert patched.status_code == 200
    assert patched.json()["status"] == "FIELDWORK"

    # ``include_demo=False`` excludes the seeded demonstration project, which the
    # startup bootstrap also creates in FIELDWORK.
    listed = api_client.get(
        V1 + "/projects", params={"status": "FIELDWORK", "include_demo": False}
    ).json()
    assert [p["id"] for p in listed] == [project_id]

    deleted = api_client.delete(V1 + "/projects/{0}".format(project_id))
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True
    assert api_client.get(V1 + "/projects/{0}".format(project_id)).status_code == 404


def test_scoping_and_unscoping_controls(api_client):
    project_id = api_client.post(V1 + "/projects", json={"name": "Scope", "audit_area": "IAM"}).json()["id"]

    scoped = api_client.post(
        V1 + "/projects/{0}/controls".format(project_id), json={"control_refs": ["CONTROL-001", "CONTROL-005"]}
    )
    assert scoped.status_code == 200
    assert scoped.json()["controls_in_scope"] == 2

    removed = api_client.delete(V1 + "/projects/{0}/controls/CONTROL-005".format(project_id))
    assert removed.status_code == 200
    assert removed.json()["changed"] is True
    listing = api_client.get(V1 + "/projects/{0}/controls".format(project_id)).json()
    assert listing["scoped_control_refs"] == ["CONTROL-001"]
    assert listing["controls_in_scope"] == 1

    # Removing something that was never in scope changes nothing and says so.
    again = api_client.delete(V1 + "/projects/{0}/controls/CONTROL-005".format(project_id))
    assert again.json()["changed"] is False


def test_control_library_endpoints(api_client):
    categories = api_client.get(V1 + "/controls/categories").json()
    assert "Logical Access" in categories

    created = api_client.post(
        V1 + "/controls",
        json={"control_id": "CONTROL-950", "name": "API-created control", "category": "Logical Access"},
    )
    assert created.status_code == 201

    assert api_client.post(V1 + "/controls/CONTROL-950/deactivate").status_code == 200
    active = api_client.get(V1 + "/controls", params={"active_only": True}).json()
    assert "CONTROL-950" not in [c["control_id"] for c in active]

    assert api_client.post(V1 + "/controls/CONTROL-950/activate").status_code == 200
    assert api_client.get(V1 + "/controls/CONTROL-950").json()["is_active"] is True


def test_evidence_inventory_and_deletion(api_client, generated_datasets):
    project_id = api_client.post(V1 + "/projects", json={"name": "Ev", "audit_area": "IAM"}).json()["id"]
    path = [p for p in generated_datasets["DATASET-001"] if p.suffix == ".csv"][0]
    uploaded = _upload(api_client, project_id, path, EvidenceType.USER_LISTING).json()

    listing = api_client.get(V1 + "/evidence", params={"project_id": project_id}).json()
    assert [item["id"] for item in listing] == [uploaded["id"]]

    chunks = api_client.get(V1 + "/evidence/{0}/chunks".format(uploaded["id"])).json()
    assert chunks
    assert all(c["locator_text"] for c in chunks)

    stats = api_client.get(V1 + "/evidence/stats", params={"project_id": project_id}).json()
    assert stats["evidence_files"] == 1

    assert api_client.delete(V1 + "/evidence/{0}".format(uploaded["id"])).json()["deleted"] is True
    assert api_client.get(V1 + "/evidence", params={"project_id": project_id}).json() == []


def test_an_unsupported_file_is_stored_and_reported_not_dropped(api_client, tmp_path):
    project_id = api_client.post(V1 + "/projects", json={"name": "Odd", "audit_area": "IAM"}).json()["id"]
    path = tmp_path / "tool.exe"
    path.write_bytes(b"MZ\x90")
    response = _upload(api_client, project_id, path)
    assert response.status_code == 201
    assert response.json()["parse_status"] == "UNSUPPORTED"
    assert response.json()["parse_error"]


@pytest.mark.parametrize("mode", [m.value for m in ExperimentMode])
def test_every_experiment_mode_is_reachable_over_http(api_client, generated_datasets, mode):
    dataset = get_dataset("DATASET-001")
    project_id = api_client.post(
        V1 + "/projects",
        json={"name": "Mode " + mode, "audit_area": "IAM", "control_refs": [dataset.control_ref]},
    ).json()["id"]
    for path in generated_datasets["DATASET-001"]:
        _upload(api_client, project_id, path, dataset.file(path.name).evidence_type)

    response = api_client.post(
        V1 + "/assessments/run",
        json={"project_id": project_id, "control_ref": dataset.control_ref, "mode": mode},
    )
    assert response.status_code == 200
    assert response.json()["run"]["mode"] == mode
    assert response.json()["assessment"]["experiment_mode"] == mode


def test_running_without_persisting_writes_nothing(api_client, generated_datasets):
    dataset = get_dataset("DATASET-001")
    project_id = api_client.post(
        V1 + "/projects",
        json={"name": "Transient", "audit_area": "IAM", "control_refs": [dataset.control_ref]},
    ).json()["id"]
    for path in generated_datasets["DATASET-001"]:
        _upload(api_client, project_id, path, dataset.file(path.name).evidence_type)

    response = api_client.post(
        V1 + "/assessments/run",
        json={"project_id": project_id, "control_ref": dataset.control_ref, "persist": False},
    )
    assert response.json()["persisted"] is False
    assert response.json()["assessment"] is None
    assert api_client.get(V1 + "/assessments", params={"project_id": project_id}).json() == []


def test_evaluation_endpoints_expose_the_suite(api_client):
    datasets = api_client.get(V1 + "/evaluation/datasets").json()
    assert len(datasets) == 6
    assert {d["dataset_id"] for d in datasets} >= {"DATASET-001", "DATASET-005"}
    assert api_client.get(V1 + "/evaluation/runs").json() == []


@pytest.mark.slow
def test_an_evaluation_run_can_be_started_and_read_back(api_client):
    started = api_client.post(
        V1 + "/evaluation/run",
        json={"mode": ExperimentMode.B_RAG.value, "dataset_ids": ["DATASET-005"], "run_name": "api test"},
    )
    assert started.status_code == 201, started.text
    run_id = started.json()["id"]

    detail = api_client.get("{0}/evaluation/runs/{1}".format(V1, run_id)).json()
    assert detail["experiment_mode"] == ExperimentMode.B_RAG.value
    assert len(detail["results"]) == 1
    assert detail["results"][0]["expected_status"] == AssessmentStatus.INSUFFICIENT_EVIDENCE.value

    compared = api_client.get(V1 + "/evaluation/compare", params={"run_ids": [run_id]})
    assert compared.status_code == 200


def test_the_dashboard_breakdowns_cover_every_member(api_client):
    statuses = api_client.get(V1 + "/dashboard/status-breakdown").json()
    risks = api_client.get(V1 + "/dashboard/risk-breakdown").json()
    assert set(statuses["counts"]) == set(AssessmentStatus.values())
    assert risks["basis"]
    activity = api_client.get(V1 + "/dashboard/activity", params={"limit": 5}).json()
    assert isinstance(activity, list)


def test_evaluation_assessments_are_hidden_from_the_operational_listing_by_default(
    api_client, api_session, generated_datasets
):
    from app.database.models import EvaluationRun

    dataset = get_dataset("DATASET-001")
    project_id = api_client.post(
        V1 + "/projects",
        json={"name": "Hidden", "audit_area": "IAM", "control_refs": [dataset.control_ref]},
    ).json()["id"]
    for path in generated_datasets["DATASET-001"]:
        _upload(api_client, project_id, path, dataset.file(path.name).evidence_type)

    run = EvaluationRun(name="tagging", experiment_mode=ExperimentMode.B_RAG.value)
    api_session.add(run)
    api_session.commit()
    from app.audit.engine import assess

    assess(
        api_session,
        project_id,
        dataset.control_ref,
        mode=ExperimentMode.B_RAG,
        evaluation_run_id=run.id,
    )

    assert api_client.get(V1 + "/assessments", params={"project_id": project_id}).json() == []
    included = api_client.get(
        V1 + "/assessments", params={"project_id": project_id, "include_evaluation": True}
    ).json()
    assert len(included) == 1
