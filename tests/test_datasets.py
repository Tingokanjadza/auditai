"""The synthetic evaluation suite: ground truth that is re-derivable from the bytes.

Every accuracy figure this project reports rests on these six records, so the important
test here is not that the declarations exist but that the *files on disk still satisfy
them*: the planted counts, the exception rows and every key evidence marker are checked
against what the production parser actually reads back. Ground truth asserted only in a
docstring is not ground truth.

The suite also has to stay honest about what it is - fabricated, unambiguous, and n = 6 -
so the disclaimers are asserted too.
"""

from __future__ import annotations

import pytest

from app.evaluation.datasets import (
    DATASET_DISCLAIMER,
    GENERATOR_SEED,
    SYNTHETIC_DATASETS,
    SYNTHETIC_NOTICE,
    DatasetFile,
    SyntheticDataset,
    UnknownDatasetError,
    dataset_ids,
    dataset_manifest,
    expected_status_map,
    get_dataset,
    markers_for,
)
from app.evaluation.generator import verify_dataset
from app.evidence.parsers import parse_file
from app.schemas.enums import AssessmentStatus, EvidenceType, RiskLevel

ALL_IDS = dataset_ids()


# --------------------------------------------------------------------- the suite
def test_the_five_mandated_datasets_are_present_plus_the_declared_addition():
    assert len(SYNTHETIC_DATASETS) == 6
    mandated = [d for d in SYNTHETIC_DATASETS if d.mandated]
    additions = [d for d in SYNTHETIC_DATASETS if not d.mandated]
    assert [d.dataset_id for d in mandated] == [
        "DATASET-001",
        "DATASET-002",
        "DATASET-003",
        "DATASET-004",
        "DATASET-005",
    ]
    assert [d.dataset_id for d in additions] == ["DATASET-006"]
    assert dataset_ids(mandated_only=True) == [d.dataset_id for d in mandated]


@pytest.mark.parametrize(
    "dataset_id,expected_status",
    [
        ("DATASET-001", AssessmentStatus.POTENTIAL_DEFICIENCY),
        ("DATASET-002", AssessmentStatus.POTENTIAL_DEFICIENCY),
        ("DATASET-003", AssessmentStatus.NOT_EFFECTIVE),
        ("DATASET-004", AssessmentStatus.POTENTIAL_DEFICIENCY),
        ("DATASET-005", AssessmentStatus.INSUFFICIENT_EVIDENCE),
        ("DATASET-006", AssessmentStatus.EFFECTIVE),
    ],
)
def test_the_declared_ground_truth_matches_the_research_design(dataset_id, expected_status):
    assert get_dataset(dataset_id).expected_status is expected_status


def test_the_confusion_matrix_has_a_true_negative_class():
    """Without an EFFECTIVE case, "answer POTENTIAL_DEFICIENCY to everything" scores 100%."""
    statuses = {d.expected_status for d in SYNTHETIC_DATASETS}
    assert AssessmentStatus.EFFECTIVE in statuses
    assert AssessmentStatus.INSUFFICIENT_EVIDENCE in statuses
    assert len(statuses) >= 3


def test_three_datasets_hold_the_requirement_fixed_and_vary_only_the_evidence():
    """One unchanged control, three different correct answers - the cleanest available
    demonstration that the system reads the evidence rather than the control text."""
    family = [get_dataset(i) for i in ("DATASET-001", "DATASET-005", "DATASET-006")]
    assert {d.control_ref for d in family} == {"CONTROL-001"}
    policy_names = {d.files[0].filename for d in family}
    assert len(policy_names) == 1, "the policy document must be identical across the family"
    assert len({d.expected_status for d in family}) == 3


def test_dataset_005_is_the_case_the_study_turns_on():
    dataset = get_dataset("DATASET-005")
    assert dataset.expected_status is AssessmentStatus.INSUFFICIENT_EVIDENCE
    assert dataset.expected_missing_evidence is True
    # The attribute under test is declared absent, and no exception row is claimed.
    # (``exception_ratio`` is nonetheless 0.0 here rather than None, because the
    # declaration does carry a population total for the listing. Read it with
    # ``population["absent_attribute"]``: nothing was tested, so nothing failed.)
    assert dataset.population["absent_attribute"] == "MFA enrolment status"
    assert dataset.exception_count == 0
    assert dataset.exception_rows == []


def test_dataset_005_contains_no_mfa_token_anywhere(generated_datasets):
    """The declaration lists the substrings that must not appear; if one leaked in, the
    case would stop being "the attribute is absent" and the result would be
    uninterpretable."""
    dataset = get_dataset("DATASET-005")
    listing = [p for p in generated_datasets["DATASET-005"] if p.suffix == ".csv"][0]
    text = listing.read_text(encoding="utf-8", errors="replace").lower()
    for forbidden in dataset.population["forbidden_substrings"]:
        assert forbidden not in text, "{0!r} leaked into the listing".format(forbidden)


def test_dataset_003_plants_a_configured_value_below_its_policy_threshold():
    """The condition is a threshold comparison, not a population rate."""
    population = get_dataset("DATASET-003").population
    assert population["required_value"] == 14
    assert population["configured_value"] == 8
    assert population["attribute"] == "Minimum_Password_Length"


@pytest.mark.parametrize(
    "dataset_id,total,exceptions",
    [("DATASET-001", 100, 10), ("DATASET-002", 100, 5), ("DATASET-004", 100, 5), ("DATASET-006", 100, 0)],
)
def test_the_planted_populations_are_declared_in_numbers(dataset_id, total, exceptions):
    dataset = get_dataset(dataset_id)
    assert dataset.population_total == total
    assert dataset.exception_count == exceptions
    assert dataset.exception_ratio == pytest.approx(exceptions / float(total))


# ----------------------------------------------------------------------- lookup
@pytest.mark.parametrize("raw", ["DATASET-001", "dataset-001", "dataset_001", "001", 1, "1"])
def test_dataset_lookup_is_tolerant_of_how_the_id_was_typed(raw):
    assert get_dataset(raw).dataset_id == "DATASET-001"


def test_an_unknown_dataset_raises_rather_than_returning_nothing():
    """Silently evaluating nothing would look like a passing run."""
    with pytest.raises(UnknownDatasetError):
        get_dataset("DATASET-999")


def test_get_dataset_passes_a_dataset_object_through():
    dataset = SYNTHETIC_DATASETS[0]
    assert get_dataset(dataset) is dataset


def test_expected_status_map_is_the_ground_truth_the_runner_scores_against():
    mapping = expected_status_map()
    assert set(mapping) == set(ALL_IDS)
    assert mapping["DATASET-005"] == AssessmentStatus.INSUFFICIENT_EVIDENCE.value
    assert expected_status_map(["DATASET-001"]) == {
        "DATASET-001": AssessmentStatus.POTENTIAL_DEFICIENCY.value
    }


# ----------------------------------------------------------------- declarations
@pytest.mark.parametrize("dataset", SYNTHETIC_DATASETS, ids=lambda d: d.dataset_id)
def test_every_dataset_declares_what_a_reader_needs_to_judge_it(dataset):
    assert isinstance(dataset, SyntheticDataset)
    assert dataset.control_ref.startswith("CONTROL-")
    assert dataset.expected_risk in set(RiskLevel)
    assert dataset.files and all(isinstance(f, DatasetFile) for f in dataset.files)
    assert dataset.key_evidence_markers
    assert dataset.notes and dataset.rationale
    assert dataset.seed == GENERATOR_SEED


@pytest.mark.parametrize("dataset", SYNTHETIC_DATASETS, ids=lambda d: d.dataset_id)
def test_every_declared_file_names_its_evidence_type(dataset):
    """The retriever copies this onto every chunk, and the provider uses it to tell a
    document that *states a requirement* from an export that *records what happened*."""
    for declared in dataset.files:
        assert declared.evidence_type in EvidenceType.values()
        assert declared.role
        assert declared.description
        assert declared.extension


def test_the_suite_covers_every_supported_document_format():
    extensions = {ext for d in SYNTHETIC_DATASETS for ext in d.extensions}
    assert {".csv", ".xlsx", ".docx", ".pdf", ".txt", ".json"} <= extensions


def test_the_manifest_is_serialisable_for_the_api_and_the_write_up():
    manifest = dataset_manifest()
    assert len(manifest) == 6
    entry = manifest[0]
    assert entry["dataset_id"] == "DATASET-001"
    assert entry["expected_status"] == AssessmentStatus.POTENTIAL_DEFICIENCY.value
    assert entry["key_evidence_markers"]
    assert entry["exception_rows"]


def test_the_suite_states_its_own_limits():
    assert "synthetic" in DATASET_DISCLAIMER.lower()
    assert "six datasets" in DATASET_DISCLAIMER or "statistically" in DATASET_DISCLAIMER
    assert "SYNTHETIC RESEARCH DATA" in SYNTHETIC_NOTICE
    assert len(SYNTHETIC_NOTICE) <= 255, "the notice must fit an OOXML core property"


# -------------------------------------------------------------- the actual bytes
@pytest.mark.parametrize("dataset_id", ALL_IDS)
def test_the_generated_files_are_exactly_the_declared_ones(generated_datasets, dataset_id):
    dataset = get_dataset(dataset_id)
    written = generated_datasets[dataset_id]
    assert [p.name for p in written] == dataset.file_names
    assert all(p.is_file() and p.stat().st_size > 0 for p in written)


@pytest.mark.parametrize("dataset_id", ALL_IDS)
def test_every_key_evidence_marker_is_literally_in_its_own_document(generated_datasets, dataset_id):
    """A marker that is not a substring of the evidence it claims to come from would
    make the retrieval metric a measurement of nothing."""
    dataset = get_dataset(dataset_id)
    corpus = []
    for path in generated_datasets[dataset_id]:
        result = parse_file(str(path), path.name)
        corpus.extend(c.text for c in result.chunks)
    blob = " ".join(corpus)
    normalised = " ".join(blob.split())
    for marker in dataset.key_evidence_markers:
        assert " ".join(marker.split()) in normalised, "marker not found: {0!r}".format(marker)


@pytest.mark.parametrize("dataset_id", ALL_IDS)
def test_the_declaration_is_re_derivable_from_what_was_written(generated_datasets, dataset_id):
    """The generator's own verifier, run against the bytes this session produced."""
    report = verify_dataset(dataset_id, regenerate=False)
    assert report["missing_files"] == []
    assert all(f["ok"] for f in report["files"]), report["files"]
    assert all(m["ok"] for m in report["markers"]), [m["marker"] for m in report["markers"] if not m["ok"]]
    assert all(c["ok"] for c in report["population_checks"]), report["population_checks"]
    assert report["ok"] is True


def test_the_exception_rows_are_where_the_dataset_says_they_are(generated_datasets):
    """DATASET-001 declares the 1-based spreadsheet rows carrying disabled accounts."""
    dataset = get_dataset("DATASET-001")
    csv_path = [p for p in generated_datasets["DATASET-001"] if p.suffix == ".csv"][0]
    result = parse_file(str(csv_path), csv_path.name)
    found = []
    for chunk in result.chunks:
        for line in chunk.text.splitlines():
            if line.startswith("row ") and "Disabled" in line:
                found.append(int(line.split()[1].rstrip(":")))
    assert sorted(found) == sorted(dataset.exception_rows)
    assert len(found) == dataset.exception_count


def test_the_generator_is_deterministic(tmp_path):
    """A figure recorded in the write-up has to be reproducible from the fixed seed."""
    from app.evaluation.generator import generate_dataset

    first = generate_dataset("DATASET-001", output_dir=tmp_path / "a")
    second = generate_dataset("DATASET-001", output_dir=tmp_path / "b")
    for left, right in zip(first, second):
        assert left.name == right.name
        assert left.read_bytes() == right.read_bytes()


def test_every_generated_file_declares_itself_as_synthetic(generated_datasets):
    """No file may describe anything that could be read as a real organisation."""
    for dataset_id in ALL_IDS:
        for path in generated_datasets[dataset_id]:
            text = " ".join(
                c.text for c in parse_file(str(path), path.name).chunks
            ).upper()
            assert "SYNTHETIC" in text, "{0} does not say it is synthetic".format(path.name)


def test_markers_for_returns_a_copy(generated_datasets):
    markers = markers_for("DATASET-001")
    markers.append("mutation")
    assert "mutation" not in markers_for("DATASET-001")
