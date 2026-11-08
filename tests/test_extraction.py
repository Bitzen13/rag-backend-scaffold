"""The filesystem extractor: which files it visits, what it can say about them without opening
them, and how it survives the ones it cannot read."""

import hashlib
from pathlib import Path

import pytest

from app.extraction.base import ElementKind, ExtractedDocument, ExtractionError, read_all
from app.extraction.factory import build_extractor, build_pdf_options
from app.extraction.filesystem import FilesystemExtractor, hash_file
from app.extraction.readers.markdown import MarkdownReader
from app.settings import ExtractionConfig

TEXT_EXTENSIONS = (".md", ".txt")


def documents(root: Path, extensions: tuple[str, ...] = TEXT_EXTENSIONS) -> list[ExtractedDocument]:
    return list(read_all(FilesystemExtractor(root, extensions)))


def test_extracts_matching_files_recursively(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("alpha", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.TXT").write_text("beta", encoding="utf-8")
    (tmp_path / "ignored.pdf").write_bytes(b"%PDF")

    docs = documents(tmp_path)

    assert [(d.source_type, d.source_uri, d.text) for d in docs] == [
        ("file", "a.md", "alpha"),
        ("file", "sub/b.TXT", "beta"),
    ]


def test_metadata_records_the_file_and_the_reader_that_read_it(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("# Title\n\nBody.\n", encoding="utf-8")
    (document,) = documents(tmp_path)
    assert document.metadata["filename"] == "a.md"
    assert document.metadata["extension"] == ".md"
    assert document.metadata["reader"] == "markdown"
    assert document.metadata["reader_version"] == MarkdownReader.version


def test_each_file_goes_to_the_reader_for_its_format(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("# Heading\n\nProse.\n", encoding="utf-8")
    (tmp_path / "b.csv").write_text("Name,Value\nx,1\n", encoding="utf-8")
    (tmp_path / "c.txt").write_text("Just prose.\n", encoding="utf-8")

    docs = {d.source_uri: d for d in documents(tmp_path, (".md", ".csv", ".txt"))}

    assert docs["a.md"].elements[0].kind is ElementKind.HEADING
    assert docs["b.csv"].elements[0].kind is ElementKind.TABLE_ROW
    assert docs["c.txt"].elements[0].kind is ElementKind.PARAGRAPH


def test_an_extension_with_no_reader_is_read_as_plain_text(tmp_path: Path) -> None:
    (tmp_path / "notes.unheardof").write_text("Some prose here.\n", encoding="utf-8")
    (document,) = documents(tmp_path, (".unheardof",))
    assert document.metadata["reader"] == "text"
    assert document.text == "Some prose here."


def test_an_unreadable_file_is_skipped_and_the_run_continues(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / "broken.csv").write_bytes(b'"unclosed quote\x00')
    (tmp_path / "good.md").write_text("fine", encoding="utf-8")
    (tmp_path / "broken.docx").write_text("not really a docx", encoding="utf-8")

    docs = documents(tmp_path, (".csv", ".md", ".docx"))

    assert "good.md" in [d.source_uri for d in docs]
    assert "broken.docx" not in [d.source_uri for d in docs]
    assert any("skipping" in record.getMessage() for record in caplog.records)


def test_a_file_that_yields_no_text_is_reported_but_still_returned(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / "empty.md").write_text("   \n\n", encoding="utf-8")
    (document,) = documents(tmp_path, (".md",))
    assert document.text == ""
    assert any("found no text" in record.getMessage() for record in caplog.records)


def test_missing_directory_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        documents(tmp_path / "nope")


def test_factory_builds_filesystem_extractor(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("alpha", encoding="utf-8")
    config = ExtractionConfig(provider="filesystem", path=tmp_path)
    assert [d.source_uri for d in read_all(build_extractor(config))] == ["a.md"]


def test_pdf_options_come_from_config() -> None:
    config = ExtractionConfig.model_validate(
        {
            "provider": "filesystem",
            "path": "documents",
            "pdf": {
                "strip_patterns": [r"\[\d+\]"],
                "skip_sections": ["References"],
                "borderless_tables": False,
                "ocr": {"enabled": True, "language": "deu", "resolution": 150},
            },
        }
    )
    options = build_pdf_options(config)
    assert options.strip_patterns == (r"\[\d+\]",)
    assert options.skip_sections == ("References",)
    assert options.borderless_tables is False
    assert (options.ocr.enabled, options.ocr.language, options.ocr.resolution) == (True, "deu", 150)


def test_invalid_characters_are_stripped_from_text_files(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("nul" + chr(0) + "byte\nsecond line", encoding="utf-8")
    (document,) = documents(tmp_path, (".txt",))
    assert "nulbyte" in document.text
    assert chr(0) not in document.text


# --- candidates: what a run knows before it opens anything -------------------------------------


def test_candidates_identify_every_file_without_parsing_it(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("# Title\n\nBody.\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("prose", encoding="utf-8")

    candidates = list(FilesystemExtractor(tmp_path, TEXT_EXTENSIONS).candidates())

    assert [c.source_uri for c in candidates] == ["a.md", "b.txt"]
    assert [(c.reader, c.reader_version) for c in candidates] == [
        ("markdown", MarkdownReader.version),
        ("text", 1),
    ]
    assert all(c.source_type == "file" for c in candidates)


def test_a_candidates_hash_is_of_the_raw_bytes(tmp_path: Path) -> None:
    raw = b"# Title\n\nBody.\n"
    (tmp_path / "a.md").write_bytes(raw)

    (candidate,) = FilesystemExtractor(tmp_path, TEXT_EXTENSIONS).candidates()

    assert candidate.source_hash == hashlib.sha256(raw).hexdigest()
    # Not the hash of the extracted text: that one costs a parse, which is the point.
    assert candidate.source_hash != hashlib.sha256(b"# Title\n\nBody.").hexdigest()


def test_the_raw_hash_changes_with_the_file_and_not_with_its_name(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("one", encoding="utf-8")
    (tmp_path / "b.md").write_text("one", encoding="utf-8")
    first, second = FilesystemExtractor(tmp_path, TEXT_EXTENSIONS).candidates()
    assert first.source_hash == second.source_hash

    (tmp_path / "a.md").write_text("two", encoding="utf-8")
    changed, _ = FilesystemExtractor(tmp_path, TEXT_EXTENSIONS).candidates()
    assert changed.source_hash != first.source_hash


def test_hashing_reads_a_file_in_blocks(tmp_path: Path) -> None:
    payload = b"x" * (3 * (1 << 20) + 17)  # larger than one read block
    path = tmp_path / "big.bin"
    path.write_bytes(payload)
    assert hash_file(path) == hashlib.sha256(payload).hexdigest()


def test_candidate_read_parses_only_when_called(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("# Title\n\nBody.\n", encoding="utf-8")
    (candidate,) = FilesystemExtractor(tmp_path, TEXT_EXTENSIONS).candidates()

    (tmp_path / "a.md").unlink()  # nothing has been parsed yet, so this is what read() will hit

    with pytest.raises(ExtractionError, match=r"a\.md"):
        candidate.read()


def test_candidate_read_raises_extraction_error_for_the_wrong_format(tmp_path: Path) -> None:
    (tmp_path / "broken.docx").write_text("not really a docx", encoding="utf-8")
    (candidate,) = FilesystemExtractor(tmp_path, (".docx",)).candidates()

    with pytest.raises(ExtractionError, match=r"broken\.docx"):
        candidate.read()
