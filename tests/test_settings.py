import copy
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from settings import load_settings

VALID_CONFIG: dict[str, Any] = {
    "extraction": {"provider": "filesystem", "path": "data/sample", "extensions": [".md", ".txt"]},
    "chunking": {
        "chunk_size": 500,
        "chunk_overlap": 50,
        "documents": {"a.md": "fixed_size", "b.pdf": "document_aware"},
    },
    "retrieval": {"top_k": 5, "similarity_threshold": 0.75},
    "models": {
        "embedding_provider": "openai",
        "embedding_model": "text-embedding-3-small",
        "llm_provider": "anthropic",
        "llm_model": "claude-sonnet-4-6",
    },
    "vector_db": {"provider": "pgvector"},
    "auth": {"api_key_header": "X-API-Key"},
}

SECRET_ENV_VARS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DATABASE_URL", "VECTOR_DB_URL")


@pytest.fixture(autouse=True)
def _clean_secret_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in SECRET_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def write_config(tmp_path: Path, overrides: dict[str, dict[str, Any]] | None = None) -> Path:
    config = copy.deepcopy(VALID_CONFIG)
    for section, values in (overrides or {}).items():
        config.setdefault(section, {}).update(values)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return path


def test_real_config_yaml_is_valid() -> None:
    settings = load_settings(Path("config.yaml"), env_file=None)
    assert settings.chunking.chunk_size == 500
    assert settings.vector_db.provider == "pgvector"
    assert settings.max_chunk_tokens <= 8191  # the model's input limit


def test_every_document_in_the_real_config_has_a_usable_plan() -> None:
    from chunking.factory import build_chunkers

    settings = load_settings(Path("config.yaml"), env_file=None)
    chunkers = build_chunkers(settings)
    for uri in settings.chunking.documents:
        plan = chunkers.plan_for(uri)
        assert plan.chunk_size <= plan.max_chunk_tokens
        assert plan.chunk_overlap < plan.chunk_size


def test_a_document_may_be_a_bare_strategy_or_a_mapping(tmp_path: Path) -> None:
    settings = load_settings(
        write_config(
            tmp_path,
            {
                "chunking": {
                    "documents": {
                        "short.md": "fixed_size",
                        "long.pdf": {
                            "strategy": "document_aware",
                            "chunk_size": 400,
                            "prose_splitting": "sentence",
                        },
                    }
                }
            },
        ),
        env_file=None,
    )
    documents = settings.chunking.documents
    assert documents["short.md"].strategy == "fixed_size"
    assert documents["short.md"].chunk_size is None  # falls back to the global value
    assert documents["long.pdf"].chunk_size == 400
    assert documents["long.pdf"].prose_splitting == "sentence"


def test_prose_splitting_is_rejected_where_it_would_do_nothing(tmp_path: Path) -> None:
    entry = {"strategy": "fixed_size", "prose_splitting": "sentence"}
    overrides: dict[str, dict[str, Any]] = {"chunking": {"documents": {"a.md": entry}}}
    with pytest.raises(ValidationError, match="document_aware"):
        load_settings(write_config(tmp_path, overrides), env_file=None)


def test_a_per_document_overlap_larger_than_its_size_is_rejected(tmp_path: Path) -> None:
    overrides = {
        "chunking": {"documents": {"a.md": {"strategy": "fixed_size", "chunk_overlap": 900}}}
    }
    with pytest.raises(ValidationError, match="chunk_overlap"):
        load_settings(write_config(tmp_path, overrides), env_file=None)


def test_the_hard_cap_is_derived_from_the_embedding_model(tmp_path: Path) -> None:
    settings = load_settings(write_config(tmp_path), env_file=None)
    assert settings.chunking.max_chunk_tokens is None
    assert settings.max_chunk_tokens == int(8191 * 0.95)


def test_an_unknown_embedding_model_requires_an_explicit_cap(tmp_path: Path) -> None:
    overrides: dict[str, dict[str, Any]] = {"models": {"embedding_model": "some-new-model"}}
    with pytest.raises(ValidationError, match="max_chunk_tokens"):
        load_settings(write_config(tmp_path, overrides), env_file=None)

    overrides["chunking"] = {"max_chunk_tokens": 1024}
    settings = load_settings(write_config(tmp_path, overrides), env_file=None)
    assert settings.max_chunk_tokens == 1024


def test_pdf_options_have_defaults(tmp_path: Path) -> None:
    pdf = load_settings(write_config(tmp_path), env_file=None).extraction.pdf
    assert pdf.borderless_tables is True
    assert pdf.ocr.enabled is False
    assert pdf.ocr.language == "eng"


def test_valid_config_loads(tmp_path: Path) -> None:
    settings = load_settings(write_config(tmp_path), env_file=None)
    assert settings.retrieval.top_k == 5
    assert settings.auth.api_key_header == "X-API-Key"


@pytest.mark.parametrize(
    ("overrides", "bad_field"),
    [
        ({"extraction": {"provider": "carrier_pigeon"}}, "provider"),
        ({"extraction": {"extensions": ["md"]}}, "extensions"),
        ({"chunking": {"chunk_size": -1}}, "chunk_size"),
        ({"chunking": {"chunk_size": 0}}, "chunk_size"),
        ({"chunking": {"chunk_overlap": 500}}, "chunk_overlap"),
        ({"chunking": {"documents": {"a.md": "sliding"}}}, "documents"),
        ({"chunking": {"strategy": "fixed_size"}}, "strategy"),
        ({"chunking": {"tokenizer": "guesswork"}}, "tokenizer"),
        ({"chunking": {"prose_splitting": "semantic"}}, "prose_splitting"),
        ({"chunking": {"max_chunk_tokens": 0}}, "max_chunk_tokens"),
        ({"chunking": {"max_chunk_tokens": 99999}}, "max_chunk_tokens"),
        ({"chunking": {"chunk_size": 20000}}, "chunk_size"),
        ({"extraction": {"pdf": {"strip_patterns": ["[unclosed"]}}}, "regular expression"),
        ({"extraction": {"pdf": {"ocr": {"resolution": 10}}}}, "resolution"),
        ({"retrieval": {"top_k": 0}}, "top_k"),
        ({"retrieval": {"similarity_threshold": 1.5}}, "similarity_threshold"),
        ({"models": {"llm_provider": "mystery"}}, "llm_provider"),
        ({"models": {"llm_model": ""}}, "llm_model"),
        ({"vector_db": {"provider": "sqlite"}}, "provider"),
        ({"auth": {"unexpected": "x"}}, "unexpected"),
    ],
)
def test_bad_values_fail_validation(
    tmp_path: Path, overrides: dict[str, dict[str, Any]], bad_field: str
) -> None:
    with pytest.raises(ValidationError) as exc_info:
        load_settings(write_config(tmp_path, overrides), env_file=None)
    assert bad_field in str(exc_info.value)


def test_missing_section_fails_validation(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"chunking": VALID_CONFIG["chunking"]}), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_settings(path, env_file=None)


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_settings(tmp_path / "nope.yaml", env_file=None)


def test_non_mapping_yaml_raises(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("- just\n- a list\n", encoding="utf-8")
    with pytest.raises(ValueError, match="YAML mapping"):
        load_settings(path, env_file=None)


def test_secrets_absent_by_default(tmp_path: Path) -> None:
    secrets = load_settings(write_config(tmp_path), env_file=None).secrets
    assert secrets.anthropic_api_key is None
    assert secrets.database_url is None


def test_secrets_come_from_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    secrets = load_settings(write_config(tmp_path), env_file=None).secrets
    assert secrets.anthropic_api_key is not None
    assert secrets.anthropic_api_key.get_secret_value() == "sk-test"
    assert "sk-test" not in repr(secrets)


def test_config_path_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONFIG_PATH", str(write_config(tmp_path, {"retrieval": {"top_k": 9}})))
    assert load_settings(env_file=None).retrieval.top_k == 9


def test_fuzzy_retrieval_settings_have_defaults_and_are_validated(tmp_path: Path) -> None:
    retrieval = load_settings(write_config(tmp_path), env_file=None).retrieval
    assert (retrieval.fuzzy_top_k, retrieval.fuzzy_min_similarity) == (5, 0.6)

    with pytest.raises(ValidationError, match="fuzzy_min_similarity"):
        load_settings(
            write_config(tmp_path, {"retrieval": {"fuzzy_min_similarity": 1.5}}), env_file=None
        )
