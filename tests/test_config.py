"""Configuration, and the proof that this suite cannot touch real data.

The first three tests are the ones that matter. Everything the suite does afterwards -
ingesting files, writing reports, seeding controls - is only safe if the redirection in
``conftest`` actually took effect, and "it should have" is not a test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import BASE_DIR, SUPPORTED_UPLOAD_EXTENSIONS, Settings, get_settings, reload_settings
from app.database.base import engine


def _is_inside(child, parent) -> bool:
    child_path = Path(str(child)).resolve()
    parent_path = Path(str(parent)).resolve()
    return parent_path == child_path or parent_path in child_path.parents


# ---- isolation
def test_engine_url_points_inside_the_temporary_tree(tmp_root):
    """The live SQLAlchemy engine - not merely the settings object - is redirected."""
    url = str(engine.url)
    assert url.startswith("sqlite:")
    database_path = Path(engine.url.database)
    assert _is_inside(database_path, tmp_root), "engine is bound to {0}".format(url)


def test_real_project_database_is_never_the_target():
    """The developer's ``data/audit.db`` is not what the suite writes to."""
    real_db = (BASE_DIR / "data" / "audit.db").resolve()
    assert Path(engine.url.database).resolve() != real_db


@pytest.mark.parametrize(
    "attribute",
    ["data_dir", "upload_dir", "report_dir", "synthetic_dir", "evaluation_output_dir"],
)
def test_writable_directories_are_inside_the_temporary_tree(settings, tmp_root, attribute):
    assert _is_inside(getattr(settings, attribute), tmp_root)


def test_real_upload_directory_is_not_written_to(settings):
    real_uploads = (BASE_DIR / "data" / "uploads").resolve()
    assert Path(settings.upload_dir).resolve() != real_uploads


def test_control_library_directory_is_deliberately_not_redirected(settings):
    """``controls_dir`` is read-only study data, so it stays where the app ships it."""
    assert Path(settings.controls_dir).resolve() == (BASE_DIR / "data" / "controls").resolve()
    assert (Path(settings.controls_dir) / "control_library.json").is_file()


def test_no_llm_credentials_are_configured(settings):
    """A real key in the developer's environment must not reach a provider."""
    assert not settings.llm_api_key
    assert not settings.llm_base_url
    assert settings.llm_provider == "mock"
    assert settings.embedding_provider == "local"


# ---- caching
def test_get_settings_is_cached():
    assert get_settings() is get_settings()


def test_reload_settings_returns_a_fresh_equivalent_instance(settings):
    reloaded = reload_settings()
    try:
        assert reloaded is not settings
        assert reloaded.database_url == settings.database_url
        assert reloaded.llm_provider == settings.llm_provider
    finally:
        # Restore the identity the rest of the suite (and the imported modules) hold.
        get_settings.cache_clear()
        assert get_settings().database_url == settings.database_url


# ---- validators and derived values
@pytest.mark.parametrize(
    "given,expected",
    [(-1.0, 0.0), (0.0, 0.0), (0.25, 0.25), (1.0, 1.0), (5.0, 1.0)],
)
def test_mock_hallucination_rate_is_clamped_to_a_probability(given, expected):
    assert Settings(mock_hallucination_rate=given).mock_hallucination_rate == expected


@pytest.mark.parametrize("given,expected", [("  MoCk ", "mock"), ("OpenAI", "openai")])
def test_provider_names_are_normalised_to_lower_case(given, expected):
    assert Settings(llm_provider=given).llm_provider == expected


@pytest.mark.parametrize("given", ["hybrid", "Hybrid", " HYBRID "])
def test_retrieval_strategy_is_normalised_to_upper_case(given):
    assert Settings(retrieval_strategy=given).retrieval_strategy == "HYBRID"


def test_path_fields_accept_strings_and_expand_the_user_directory():
    settings = Settings(upload_dir="~/itaudit-uploads-that-do-not-exist")
    assert isinstance(settings.upload_dir, Path)
    assert "~" not in str(settings.upload_dir)


def test_max_upload_bytes_derives_from_megabytes():
    assert Settings(max_upload_mb=7).max_upload_bytes == 7 * 1024 * 1024


def test_llm_configured_is_true_for_the_mock_and_false_for_an_unconfigured_remote():
    assert Settings(llm_provider="mock").llm_configured is True
    assert Settings(llm_provider="openai", llm_api_key=None, llm_base_url=None).llm_configured is False
    assert Settings(llm_provider="openai", llm_base_url="http://localhost:11434/v1").llm_configured is True


def test_is_sqlite_reflects_the_url(settings):
    assert settings.is_sqlite is True
    assert Settings(database_url="postgresql+psycopg://u:p@h/db").is_sqlite is False


# ---- secret handling
def test_redacted_dict_masks_secrets_and_stringifies_paths():
    settings = Settings(llm_api_key="sk-abcdefghijklmnop", embedding_api_key=None)
    redacted = settings.redacted_dict()
    assert "sk-abcdefghijklmnop" not in str(redacted)
    assert redacted["llm_api_key"].startswith("sk-")
    assert redacted["llm_api_key"].endswith("(len 19)")
    assert redacted["embedding_api_key"] == "(not set)"
    assert isinstance(redacted["upload_dir"], str)


def test_short_secrets_are_fully_starred():
    assert Settings(llm_api_key="abc123").redacted_dict()["llm_api_key"] == "******"


def test_provider_summary_never_echoes_the_key():
    summary = Settings(llm_provider="openai", llm_api_key="sk-secret-value-1234").provider_summary()
    assert "sk-secret-value-1234" not in str(summary)
    assert summary["llm_configured"] is True
    assert summary["embedding_model"] == "hashing-vectorizer"


# ---- directories and extensions
def test_ensure_directories_creates_every_configured_directory(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "d",
        upload_dir=tmp_path / "u",
        report_dir=tmp_path / "r",
        synthetic_dir=tmp_path / "s",
        controls_dir=tmp_path / "c",
        sample_evidence_dir=tmp_path / "e",
        evaluation_output_dir=tmp_path / "v",
    )
    settings.ensure_directories()
    for name in "durscev":
        assert (tmp_path / name).is_dir()


def test_supported_upload_extensions_cover_every_parser():
    assert set(SUPPORTED_UPLOAD_EXTENSIONS) == {
        ".pdf",
        ".docx",
        ".csv",
        ".xlsx",
        ".xls",
        ".txt",
        ".md",
        ".json",
    }
