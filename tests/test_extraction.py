"""The filesystem extractor: which files it visits, and how it survives the ones it cannot read."""

from pathlib import Path

import pytest

from extraction.base import ElementKind
from extraction.factory import build_extractor, build_pdf_options
from extraction.filesystem import FilesystemExtractor
from settings import ExtractionConfig

TEXT_EXTENSIONS = (".md", ".txt")


def test_extracts_matching_files_recursively(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("alpha", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.TXT").write_text("beta", encoding="utf-8")
    (tmp_path / "ignored.pdf").write_bytes(b"%PDF")

    docs = list(FilesystemExtractor(tmp_path, TEXT_EXTENSIONS).extract())

    assert [(d.source_type, d.source_uri, d.text) for d in docs] == [
        ("file", "a.md", "alpha"),
        ("file", "sub/b.TXT", "beta"),
    ]


def test_metadata_records_the_file_and_the_reader_that_read_it(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("# Title\n\nBody.\n", encoding="utf-8")
    (document,) = FilesystemExtractor(tmp_path, TEXT_EXTENSIONS).extract()
    assert document.metadata["filename"] == "a.md"
    assert document.metadata["extension"] == ".md"
    assert document.metadata["reader"] == "markdown"


def test_each_file_goes_to_the_reader_for_its_format(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("# Heading\n\nProse.\n", encoding="utf-8")
    (tmp_path / "b.csv").write_text("Name,Value\nx,1\n", encoding="utf-8")
    (tmp_path / "c.txt").write_text("Just prose.\n", encoding="utf-8")

    docs = {
        d.source_uri: d for d in FilesystemExtractor(tmp_path, (".md", ".csv", ".txt")).extract()
    }

    assert docs["a.md"].elements[0].kind is ElementKind.HEADING
    assert docs["b.csv"].elements[0].kind is ElementKind.TABLE_ROW
    assert docs["c.txt"].elements[0].kind is ElementKind.PARAGRAPH


def test_an_extension_with_no_reader_is_read_as_plain_text(tmp_path: Path) -> None:
    (tmp_path / "notes.unheardof").write_text("Some prose here.\n", encoding="utf-8")
    (document,) = FilesystemExtractor(tmp_path, (".unheardof",)).extract()
    assert document.metadata["reader"] == "text"
    assert document.text == "Some prose here."


def test_an_unreadable_file_is_skipped_and_the_run_continues(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / "broken.csv").write_bytes(b'"unclosed quote\x00')
    (tmp_path / "good.md").write_text("fine", encoding="utf-8")
    (tmp_path / "broken.docx").write_text("not really a docx", encoding="utf-8")

    docs = list(FilesystemExtractor(tmp_path, (".csv", ".md", ".docx")).extract())

    assert "good.md" in [d.source_uri for d in docs]
    assert "broken.docx" not in [d.source_uri for d in docs]
    assert any("skipping" in record.getMessage() for record in caplog.records)


def test_a_file_that_yields_no_text_is_reported_but_still_returned(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / "empty.md").write_text("   \n\n", encoding="utf-8")
    (document,) = FilesystemExtractor(tmp_path, (".md",)).extract()
    assert document.text == ""
    assert any("found no text" in record.getMessage() for record in caplog.records)


def test_missing_directory_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        list(FilesystemExtractor(tmp_path / "nope", (".md",)).extract())


def test_factory_builds_filesystem_extractor(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("alpha", encoding="utf-8")
    config = ExtractionConfig(provider="filesystem", path=tmp_path)
    assert [d.source_uri for d in build_extractor(config).extract()] == ["a.md"]


def test_pdf_options_come_from_config() -> None:
    config = ExtractionConfig.model_validate(
        {
            "provider": "filesystem",
            "path": "data/sample",
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
    (document,) = FilesystemExtractor(tmp_path, (".txt",)).extract()
    assert "nulbyte" in document.text
    assert chr(0) not in document.text
