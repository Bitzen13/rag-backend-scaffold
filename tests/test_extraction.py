from pathlib import Path

import pytest

from extraction.factory import build_extractor
from extraction.filesystem import FilesystemExtractor
from settings import ExtractionConfig


def test_extracts_matching_files_recursively(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("alpha", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.TXT").write_text("beta", encoding="utf-8")
    (tmp_path / "ignored.pdf").write_bytes(b"%PDF")

    docs = list(FilesystemExtractor(tmp_path, [".md", ".txt"]).extract())

    assert [(d.source_type, d.source_uri, d.text) for d in docs] == [
        ("file", "a.md", "alpha"),
        ("file", "sub/b.TXT", "beta"),
    ]
    assert docs[0].metadata == {"filename": "a.md", "extension": ".md"}


def test_skips_undecodable_files(tmp_path: Path) -> None:
    (tmp_path / "bad.txt").write_bytes(b"\xff\xfe\x00bad")
    (tmp_path / "good.txt").write_text("ok", encoding="utf-8")
    docs = list(FilesystemExtractor(tmp_path, [".txt"]).extract())
    assert [d.source_uri for d in docs] == ["good.txt"]


def test_missing_directory_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        list(FilesystemExtractor(tmp_path / "nope", [".md"]).extract())


def test_factory_builds_filesystem_extractor(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("alpha", encoding="utf-8")
    config = ExtractionConfig(provider="filesystem", path=tmp_path)
    assert [d.source_uri for d in build_extractor(config).extract()] == ["a.md"]
