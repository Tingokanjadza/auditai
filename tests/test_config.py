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
    assert not settings.anthropic_api_key
    assert not settings.anthropic_base_url
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


# ---- product defaults
def test_display_name_defaults_to_auditai():
    assert Settings().app_short_name == "AuditAI"


def test_default_auditor_name_is_blank_so_nothing_is_silently_attributed():
    """The sidebar asks for a name; a blank default must not turn into an author."""
    assert Settings().default_auditor_name == ""


# ---- provider readiness, per provider
def test_llm_configured_is_always_true_for_the_mock():
    assert Settings(llm_provider="mock").llm_configured is True
    assert Settings(llm_provider="mock", anthropic_api_key=None, llm_api_key=None).llm_configured is True


def test_llm_configured_for_openai_needs_a_key_or_a_base_url():
    assert Settings(llm_provider="openai", llm_api_key=None, llm_base_url=None).llm_configured is False
    assert Settings(llm_provider="openai", llm_api_key="sk-x").llm_configured is True
    assert Settings(llm_provider="openai", llm_base_url="http://localhost:11434/v1").llm_configured is True


@pytest.mark.parametrize("provider", ["claude", "anthropic"])
def test_llm_configured_for_claude_reads_the_anthropic_fields_not_the_openai_ones(provider):
    assert Settings(llm_provider=provider, anthropic_api_key=None, anthropic_base_url=None).llm_configured is False
    assert Settings(llm_provider=provider, anthropic_api_key="sk-ant-x").llm_configured is True
    assert Settings(llm_provider=provider, anthropic_base_url="http://gateway.local").llm_configured is True
    # An OpenAI key alone does not make Claude ready. anthropic_base_url is pinned to
    # None because a developer shell often exports ANTHROPIC_BASE_URL for other tooling.
    assert (
        Settings(
            llm_provider=provider, llm_api_key="sk-openai", anthropic_api_key=None, anthropic_base_url=None
        ).llm_configured
        is False
    )


def test_empty_string_key_does_not_count_as_configured():
    """``ANTHROPIC_API_KEY=`` in a copied .env.example is an unset key, not a set one."""
    assert Settings(llm_provider="claude", anthropic_api_key="", anthropic_base_url=None).llm_configured is False
    assert Settings(llm_provider="openai", llm_api_key="").llm_configured is False


def test_is_claude_provider_matches_both_spellings():
    assert Settings(llm_provider="claude").is_claude_provider is True
    assert Settings(llm_provider="Anthropic").is_claude_provider is True
    assert Settings(llm_provider="openai").is_claude_provider is False
    assert Settings(llm_provider="mock").is_claude_provider is False


def test_active_llm_model_follows_the_selected_provider():
    assert Settings(llm_provider="claude", anthropic_model="claude-opus-5").active_llm_model == "claude-opus-5"
    assert Settings(llm_provider="openai", llm_model="gpt-4o-mini").active_llm_model == "gpt-4o-mini"
    assert "mock" in Settings(llm_provider="mock").active_llm_model


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


def test_redacted_dict_masks_the_anthropic_key():
    key = "sk-ant-api03-verysecretvalue"
    redacted = Settings(anthropic_api_key=key).redacted_dict()
    assert "verysecretvalue" not in str(redacted)
    assert redacted["anthropic_api_key"].startswith("sk-")
    assert redacted["anthropic_api_key"].endswith("(len {0})".format(len(key)))
    assert Settings(anthropic_api_key=None).redacted_dict()["anthropic_api_key"] == "(not set)"


def test_every_field_named_key_is_a_declared_secret():
    """A credential field added to Settings must also be added to SECRET_FIELDS."""
    from app.config import SECRET_FIELDS

    key_fields = {name for name in Settings.model_fields if name.endswith("_api_key")}
    assert key_fields <= SECRET_FIELDS, key_fields - SECRET_FIELDS


def test_provider_summary_never_echoes_the_key():
    summary = Settings(llm_provider="openai", llm_api_key="sk-secret-value-1234").provider_summary()
    assert "sk-secret-value-1234" not in str(summary)
    assert summary["llm_configured"] is True
    assert summary["llm_model"] == "gpt-4o-mini"
    assert summary["embedding_model"] == "hashing-vectorizer"


def test_provider_summary_reports_the_claude_model_and_masks_the_claude_key():
    summary = Settings(
        llm_provider="claude",
        anthropic_api_key="sk-ant-secret-value-5678",
        anthropic_model="claude-opus-5",
        llm_model="gpt-4o-mini",
    ).provider_summary()
    assert "sk-ant-secret-value-5678" not in str(summary)
    assert summary["llm_provider"] == "claude"
    assert summary["llm_model"] == "claude-opus-5"
    assert summary["anthropic_model"] == "claude-opus-5"
    assert summary["llm_configured"] is True
    assert summary["llm_api_key"] != "(not set)"
    assert summary["anthropic_api_key"].endswith("(len 24)")


def test_provider_summary_for_the_mock_names_no_real_model():
    summary = Settings(llm_provider="mock").provider_summary()
    assert summary["llm_configured"] is True
    assert "mock" in summary["llm_model"]


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
