"""File limits: each oversized or hostile file is rejected with a named reason (safety-plan §4).

The pipeline-level caps (extracted characters, chunks, metadata) and the mass-deletion guard
are tested in `test_ingestion_pipeline.py`, against a real database.
"""

import json
import os
import uuid
import zipfile
from pathlib import Path

import pytest

from db.repositories.sources import SourceState
from extraction.filesystem import FilesystemExtractor
from extraction.limits import FileLimits, LimitExceededError, outside_root
from extraction.readers.markdown import MarkdownReader
from extraction.readers.structured import StructuredReader
from extraction.readers.tabular import ExcelReader
from ingestion.pipeline import MassDeletionError, check_mass_deletion

# About 300 bytes on disk; expanded, a list of 9**8 = 43 million strings.
YAML_BOMB = """\
a: &a ["lol","lol","lol","lol","lol","lol","lol","lol","lol"]
b: &b [*a,*a,*a,*a,*a,*a,*a,*a,*a]
c: &c [*b,*b,*b,*b,*b,*b,*b,*b,*b]
d: &d [*c,*c,*c,*c,*c,*c,*c,*c,*c]
e: &e [*d,*d,*d,*d,*d,*d,*d,*d,*d]
f: &f [*e,*e,*e,*e,*e,*e,*e,*e,*e]
g: &g [*f,*f,*f,*f,*f,*f,*f,*f,*f]
h: &h [*g,*g,*g,*g,*g,*g,*g,*g,*g]
"""


def candidates(root: Path, limits: FileLimits, extensions: tuple[str, ...] = (".md",)) -> dict:  # type: ignore[type-arg]
    extractor = FilesystemExtractor(root, extensions, limits=limits)
    return {c.source_uri: c for c in extractor.candidates()}


def rejection(candidate: object) -> str:
    with pytest.raises(LimitExceededError) as caught:
        candidate.read()  # type: ignore[attr-defined]
    return caught.value.reason


# --- the filesystem extractor ------------------------------------------------------------------


@pytest.mark.safety_limits
def test_a_file_over_the_size_cap_is_rejected_without_being_read(tmp_path: Path) -> None:
    (tmp_path / "small.md").write_text("ok", encoding="utf-8")
    (tmp_path / "big.md").write_text("x" * 1000, encoding="utf-8")

    found = candidates(tmp_path, FileLimits(max_file_bytes=100))

    assert found["big.md"].source_hash == ""  # not even hashed
    assert rejection(found["big.md"]) == "file_too_large"
    assert found["small.md"].read().text == "ok"  # the run carries on


@pytest.mark.safety_limits
def test_a_symlink_out_of_the_corpus_is_rejected(tmp_path: Path) -> None:
    corpus, outside = tmp_path / "corpus", tmp_path / "secret.md"
    corpus.mkdir()
    outside.write_text("private", encoding="utf-8")
    try:
        os.symlink(outside, corpus / "link.md")
    except (OSError, NotImplementedError):
        pytest.skip("this platform or user cannot create symlinks")

    found = candidates(corpus, FileLimits())

    assert rejection(found["link.md"]) == "outside_corpus"


@pytest.mark.safety_limits
def test_a_path_resolving_outside_the_root_is_detected_on_any_platform(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    assert outside_root(corpus / ".." / "secret.md", corpus)
    assert not outside_root(corpus / "sub" / ".." / "doc.md", corpus)


@pytest.mark.safety_limits
def test_a_symlink_inside_the_corpus_is_followed(tmp_path: Path) -> None:
    (tmp_path / "real.md").write_text("real", encoding="utf-8")
    try:
        os.symlink(tmp_path / "real.md", tmp_path / "alias.md")
    except (OSError, NotImplementedError):
        pytest.skip("this platform or user cannot create symlinks")

    assert candidates(tmp_path, FileLimits())["alias.md"].read().text == "real"


@pytest.mark.safety_limits
def test_an_archive_declaring_too_much_uncompressed_content_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bomb.docx"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", b"\0" * 2_000_000)  # a few KB compressed
    assert path.stat().st_size < 50_000

    found = candidates(tmp_path, FileLimits(max_archive_uncompressed_bytes=1_000_000), (".docx",))

    assert rejection(found["bomb.docx"]) == "archive_too_large"


@pytest.mark.safety_limits
def test_the_rejection_names_the_source_and_the_setting(tmp_path: Path) -> None:
    (tmp_path / "big.md").write_text("x" * 1000, encoding="utf-8")
    with pytest.raises(LimitExceededError, match=r"big\.md.*max_file_bytes"):
        candidates(tmp_path, FileLimits(max_file_bytes=100))["big.md"].read()


# --- spreadsheets ------------------------------------------------------------------------------


def _workbook(path: Path, *cells: str) -> Path:
    openpyxl = pytest.importorskip("openpyxl")
    workbook = openpyxl.Workbook()
    for cell in cells:
        workbook.active[cell] = 1
    workbook.save(path)
    return path


@pytest.mark.safety_limits
def test_a_sheet_declaring_a_huge_grid_is_rejected_before_it_is_iterated(tmp_path: Path) -> None:
    # Two cells, a few KB, declaring A1:FXX4000 - 18.8 million cells once padded.
    path = _workbook(tmp_path / "grid.xlsx", "A1", "FXX4000")
    assert path.stat().st_size < 10_000

    with pytest.raises(LimitExceededError) as caught:
        ExcelReader().read(path)

    assert caught.value.reason == "spreadsheet_too_large"
    assert "4,000 x 4,704" in str(caught.value)


@pytest.mark.safety_limits
def test_a_sheet_within_the_cell_limit_is_read(tmp_path: Path) -> None:
    path = _workbook(tmp_path / "ok.xlsx", "A1", "C3")
    assert ExcelReader(max_cells=9).read(path).text


@pytest.mark.safety_limits
def test_the_extractor_passes_its_cell_limit_to_the_excel_reader(tmp_path: Path) -> None:
    _workbook(tmp_path / "grid.xlsx", "A1", "J10")
    found = candidates(tmp_path, FileLimits(max_spreadsheet_cells=50), (".xlsx",))
    assert rejection(found["grid.xlsx"]) == "spreadsheet_too_large"


# --- YAML aliases --------------------------------------------------------------------------------


@pytest.mark.safety_limits
def test_yaml_aliases_are_not_expanded(tmp_path: Path) -> None:
    path = tmp_path / "bomb.yaml"
    path.write_text(YAML_BOMB, encoding="utf-8")
    assert path.stat().st_size < 400

    result = StructuredReader().read(path)

    assert len(result.text) < 2_000
    assert "*g" in result.text  # the alias is kept as what it says, not what it points at


@pytest.mark.safety_limits
def test_front_matter_aliases_are_not_expanded(tmp_path: Path) -> None:
    path = tmp_path / "doc.md"
    path.write_text(f"---\n{YAML_BOMB}---\n# Title\n\nBody.\n", encoding="utf-8")

    result = MarkdownReader().read(path)

    assert len(json.dumps(result.metadata)) < 2_000
    assert "Body." in result.text


@pytest.mark.safety_limits
def test_ordinary_yaml_still_reads(tmp_path: Path) -> None:
    path = tmp_path / "plain.yaml"
    path.write_text("name: Merlin\nthrust: 845\n", encoding="utf-8")
    assert StructuredReader().read(path).text == "name: Merlin | thrust: 845"


# --- the mass-deletion guard ---------------------------------------------------------------------


def _stored(*uris: str, deleted: tuple[str, ...] = ()) -> dict[str, SourceState]:
    return {
        uri: SourceState(
            id=uuid.uuid4(),
            source_uri=uri,
            source_hash="h",
            content_hash="c",
            chunking={},
            status="deleted" if uri in deleted else "active",
        )
        for uri in (*uris, *deleted)
    }


@pytest.mark.safety_deletion
def test_an_empty_mount_would_delete_everything_and_is_stopped() -> None:
    with pytest.raises(MassDeletionError, match="--allow-mass-delete"):
        check_mass_deletion(_stored("a", "b", "c"), set(), 0.5)


@pytest.mark.safety_deletion
def test_deleting_up_to_the_fraction_is_allowed() -> None:
    assert check_mass_deletion(_stored("a", "b", "c", "d"), {"a", "b"}, 0.5) == ["c", "d"]


@pytest.mark.safety_deletion
def test_sources_already_deleted_do_not_count() -> None:
    stored = _stored("a", "b", deleted=("x", "y", "z"))
    assert check_mass_deletion(stored, {"a"}, 0.5) == ["b"]


@pytest.mark.safety_deletion
def test_a_fraction_of_one_allows_deleting_everything() -> None:
    assert check_mass_deletion(_stored("a", "b"), set(), 1.0) == ["a", "b"]


@pytest.mark.safety_deletion
def test_a_first_run_deletes_nothing() -> None:
    assert check_mass_deletion({}, {"a"}, 0.0) == []
