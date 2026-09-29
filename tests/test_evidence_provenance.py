"""Provenance travels with an upload, on every transport, and lands on the row at creation.

``EvidenceFile.provenance`` says where an artefact came from - generated test data, a
reconstruction of a publicly documented failure pattern, or real organisational evidence -
and therefore how far anything concluded from it may be trusted. These tests pin the
plumbing that carries an auditor's declaration from the Evidence page to the database:

* ``ingest_file`` writes it on the row it creates, with SYNTHETIC as the default for an
  absent or unrecognised value (the under-trusting direction to be wrong in);
* ``is_synthetic`` never contradicts it;
* the REST upload accepts it as a form field, the response model reports it, and the
  listing can filter on it;
* the console's facade and HTTP client pass it through rather than writing it afterwards.
"""

from __future__ import annotations

import inspect

import pytest

from app.api.main import API_V1_PREFIX
from app.evidence.service import ingest_file, resolve_provenance
from app.schemas.enums import DEFAULT_EVIDENCE_PROVENANCE, EvidenceProvenance, EvidenceType

V1 = API_V1_PREFIX


# ==========================================================================
# ingest_file
# ==========================================================================
def test_ingest_file_stores_the_declared_provenance(seeded_session, project, sample_files):
    path = sample_files["csv"]
    record = ingest_file(
        seeded_session,
        project.id,
        path.read_bytes(),
        path.name,
        evidence_type=EvidenceType.USER_LISTING,
        provenance=EvidenceProvenance.HISTORICAL_PUBLIC,
    )
    seeded_session.expire_all()
    stored = seeded_session.get(type(record), record.id)
    assert stored.provenance == EvidenceProvenance.HISTORICAL_PUBLIC.value
    # A reconstruction did not come from an audited entity, so it is synthetic too.
    assert stored.is_synthetic is True


def test_ingest_file_defaults_an_unknown_provenance_to_synthetic(seeded_session, project, sample_files):
    """Nobody said where it came from: under-trust it."""
    path = sample_files["policy_txt"]
    record = ingest_file(seeded_session, project.id, path.read_bytes(), path.name)
    assert record.provenance == DEFAULT_EVIDENCE_PROVENANCE.value == EvidenceProvenance.SYNTHETIC.value
    assert record.is_synthetic is True

    nonsense = ingest_file(
        seeded_session, project.id, path.read_bytes(), "second_" + path.name, provenance="from the moon"
    )
    assert nonsense.provenance == EvidenceProvenance.SYNTHETIC.value
    assert nonsense.is_synthetic is True


def test_ingest_file_accepts_any_spelling_of_a_provenance(seeded_session, project, sample_files):
    path = sample_files["policy_txt"]
    record = ingest_file(
        seeded_session, project.id, path.read_bytes(), path.name, provenance="historical public"
    )
    assert record.provenance == EvidenceProvenance.HISTORICAL_PUBLIC.value


def test_organisational_provenance_is_the_only_one_that_is_not_synthetic(
    seeded_session, project, sample_files
):
    path = sample_files["xlsx"]
    real = ingest_file(
        seeded_session,
        project.id,
        path.read_bytes(),
        path.name,
        evidence_type=EvidenceType.CONFIGURATION_EXPORT,
        provenance=EvidenceProvenance.ORGANISATIONAL,
    )
    assert real.provenance == EvidenceProvenance.ORGANISATIONAL.value
    assert real.is_synthetic is False

    # An explicit synthetic flag is never lowered by the provenance, only ever raised.
    flagged = ingest_file(
        seeded_session,
        project.id,
        path.read_bytes(),
        "flagged_" + path.name,
        provenance=EvidenceProvenance.ORGANISATIONAL,
        is_synthetic=True,
    )
    assert flagged.is_synthetic is True


def test_provenance_is_written_at_creation_not_afterwards(seeded_session, project, sample_files):
    """The unsupported-file path returns before parsing; the row must still carry it."""
    record = ingest_file(
        seeded_session, project.id, b"MZ binary", "tool.exe", provenance=EvidenceProvenance.ORGANISATIONAL
    )
    assert record.provenance == EvidenceProvenance.ORGANISATIONAL.value


def test_the_upload_activity_entry_records_provenance(seeded_session, project, sample_files):
    from sqlalchemy import select

    from app.database.models import ActivityLog
    from app.schemas.enums import ActivityAction

    path = sample_files["csv"]
    record = ingest_file(
        seeded_session, project.id, path.read_bytes(), path.name, provenance=EvidenceProvenance.HISTORICAL_PUBLIC
    )
    entries = list(
        seeded_session.execute(
            select(ActivityLog).where(
                ActivityLog.entity_type == "evidence_file",
                ActivityLog.entity_id == record.id,
                ActivityLog.action == ActivityAction.EVIDENCE_UPLOADED.value,
            )
        ).scalars()
    )
    assert entries and entries[0].details["provenance"] == EvidenceProvenance.HISTORICAL_PUBLIC.value


def test_resolve_provenance_shares_the_enum_default():
    assert resolve_provenance(None) is DEFAULT_EVIDENCE_PROVENANCE
    assert resolve_provenance("") is DEFAULT_EVIDENCE_PROVENANCE
    assert resolve_provenance("organisational") is EvidenceProvenance.ORGANISATIONAL
    assert resolve_provenance(EvidenceProvenance.HISTORICAL_PUBLIC) is EvidenceProvenance.HISTORICAL_PUBLIC


# ==========================================================================
# read side
# ==========================================================================
def test_list_evidence_filters_by_provenance(seeded_session, project, sample_files):
    from app.audit import service

    csv_path, txt_path = sample_files["csv"], sample_files["policy_txt"]
    synthetic = ingest_file(seeded_session, project.id, csv_path.read_bytes(), csv_path.name)
    historical = ingest_file(
        seeded_session,
        project.id,
        txt_path.read_bytes(),
        txt_path.name,
        provenance=EvidenceProvenance.HISTORICAL_PUBLIC,
    )

    everything = service.list_evidence(seeded_session, project_id=project.id)
    assert {row.id for row in everything} == {synthetic.id, historical.id}

    only_historical = service.list_evidence(
        seeded_session, project_id=project.id, provenance=EvidenceProvenance.HISTORICAL_PUBLIC.value
    )
    assert [row.id for row in only_historical] == [historical.id]

    several = service.list_evidence(
        seeded_session,
        project_id=project.id,
        provenance=[EvidenceProvenance.SYNTHETIC.value, EvidenceProvenance.HISTORICAL_PUBLIC.value],
    )
    assert {row.id for row in several} == {synthetic.id, historical.id}

    none_real = service.list_evidence(
        seeded_session, project_id=project.id, provenance=EvidenceProvenance.ORGANISATIONAL.value
    )
    assert none_real == []

    assert service.evidence_to_dict(historical)["provenance"] == EvidenceProvenance.HISTORICAL_PUBLIC.value


# ==========================================================================
# REST
# ==========================================================================
def _upload(client, project_id, path, **form):
    data = {"project_id": str(project_id), "evidence_type": EvidenceType.OTHER.value}
    data.update(form)
    return client.post(
        V1 + "/evidence/upload",
        files={"file": (path.name, path.read_bytes(), "application/octet-stream")},
        data=data,
    )


def test_the_api_upload_round_trips_provenance(api_client, sample_files):
    project_id = api_client.post(V1 + "/projects", json={"name": "Prov", "audit_area": "IAM"}).json()["id"]
    path = sample_files["csv"]

    response = _upload(api_client, project_id, path, provenance=EvidenceProvenance.HISTORICAL_PUBLIC.value)
    assert response.status_code == 201, response.text
    uploaded = response.json()
    assert uploaded["provenance"] == EvidenceProvenance.HISTORICAL_PUBLIC.value
    assert uploaded["is_synthetic"] is True

    fetched = api_client.get(V1 + "/evidence/{0}".format(uploaded["id"])).json()
    assert fetched["provenance"] == EvidenceProvenance.HISTORICAL_PUBLIC.value

    listed = api_client.get(V1 + "/evidence", params={"project_id": project_id}).json()
    assert [row["provenance"] for row in listed] == [EvidenceProvenance.HISTORICAL_PUBLIC.value]


def test_the_api_upload_defaults_provenance_to_synthetic(api_client, sample_files):
    project_id = api_client.post(V1 + "/projects", json={"name": "Prov2", "audit_area": "IAM"}).json()["id"]
    response = _upload(api_client, project_id, sample_files["policy_txt"])
    assert response.status_code == 201, response.text
    assert response.json()["provenance"] == EvidenceProvenance.SYNTHETIC.value
    assert response.json()["is_synthetic"] is True


def test_the_api_rejects_an_unknown_provenance(api_client, sample_files):
    """Over HTTP a typo must fail loudly rather than be quietly coerced."""
    project_id = api_client.post(V1 + "/projects", json={"name": "Prov3", "audit_area": "IAM"}).json()["id"]
    response = _upload(api_client, project_id, sample_files["policy_txt"], provenance="GENUINE")
    assert response.status_code == 422
    assert api_client.get(V1 + "/evidence", params={"project_id": project_id}).json() == []


def test_the_api_listing_filters_by_provenance(api_client, sample_files):
    project_id = api_client.post(V1 + "/projects", json={"name": "Prov4", "audit_area": "IAM"}).json()["id"]
    synthetic = _upload(api_client, project_id, sample_files["csv"]).json()
    real = _upload(
        api_client, project_id, sample_files["xlsx"], provenance=EvidenceProvenance.ORGANISATIONAL.value
    ).json()
    assert real["is_synthetic"] is False

    only_real = api_client.get(
        V1 + "/evidence",
        params={"project_id": project_id, "provenance": EvidenceProvenance.ORGANISATIONAL.value},
    ).json()
    assert [row["id"] for row in only_real] == [real["id"]]

    both = api_client.get(
        V1 + "/evidence",
        params={
            "project_id": project_id,
            "provenance": [EvidenceProvenance.ORGANISATIONAL.value, EvidenceProvenance.SYNTHETIC.value],
        },
    ).json()
    assert {row["id"] for row in both} == {synthetic["id"], real["id"]}

    assert api_client.get(V1 + "/evidence", params={"provenance": "GENUINE"}).status_code == 422


# ==========================================================================
# the console's two transports
# ==========================================================================
def test_the_facade_and_client_carry_provenance_as_a_parameter():
    """The Evidence page passes ``provenance=`` straight through; no follow-up write."""
    from app.frontend import api_client, data_access

    assert "provenance" in inspect.signature(data_access.upload_evidence).parameters
    assert "provenance" in inspect.signature(api_client.ApiClient.upload_evidence).parameters
    assert "provenance" in inspect.signature(ingest_file).parameters


def test_the_http_client_sends_provenance_as_a_form_field(monkeypatch):
    from app.frontend.api_client import ApiClient

    captured = {}

    def fake_request(self, operation, path_params=None, params=None, json_body=None, files=None, **kwargs):
        captured["operation"] = operation
        captured["data"] = dict(kwargs.get("data") or {})
        captured["files"] = files
        return {"id": 1}

    monkeypatch.setattr(ApiClient, "request", fake_request)
    client = ApiClient(base_url="http://testserver")

    client.upload_evidence(
        7, b"A,B\n1,2\n", "x.csv", provenance=EvidenceProvenance.ORGANISATIONAL.value, is_synthetic=False
    )
    assert captured["operation"] == "upload_evidence"
    assert captured["data"]["provenance"] == EvidenceProvenance.ORGANISATIONAL.value
    assert captured["data"]["is_synthetic"] == "false"
    assert captured["files"]["file"][0] == "x.csv"

    # ``None`` is omitted so the server's own default applies, never the string "None".
    client.upload_evidence(7, b"A,B\n1,2\n", "y.csv")
    assert "provenance" not in captured["data"]


def test_the_facade_records_provenance_in_process(clean_database, project, sample_files):  # noqa: ARG001
    from app.frontend import data_access

    data_access.invalidate_cache()
    path = sample_files["csv"]
    record = data_access.upload_evidence(
        project.id,
        path.read_bytes(),
        path.name,
        evidence_type=EvidenceType.USER_LISTING,
        uploaded_by="Test Auditor",
        is_synthetic=True,
        provenance=EvidenceProvenance.HISTORICAL_PUBLIC,
    )
    assert record["provenance"] == EvidenceProvenance.HISTORICAL_PUBLIC.value
    assert record["is_synthetic"] is True

    listing = data_access.list_evidence(project_id=project.id)
    assert [row["provenance"] for row in listing] == [EvidenceProvenance.HISTORICAL_PUBLIC.value]


def test_the_evidence_view_no_longer_writes_provenance_behind_ingestion():
    """The stopgap that patched the row after ingestion is gone; the page relies on the pipeline."""
    import app.frontend.views.evidence as view

    assert not hasattr(view, "_record_provenance")
    assert not hasattr(view, "_facade_takes_provenance")
    # The radio still offers every provenance, default (SYNTHETIC) first.
    assert view._PROVENANCE_CHOICES == [member.value for member in EvidenceProvenance]
    assert view._PROVENANCE_CHOICES[0] == EvidenceProvenance.SYNTHETIC.value
