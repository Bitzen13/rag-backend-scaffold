"""The customisation boundary: what a client module may contribute, and how loudly it fails.

The failures matter more than the successes here. A customisation that silently did not load
would mean a client's documents were quietly ingested by the wrong reader — which looks like
working software and produces wrong answers — so every way of getting the name wrong must stop
start-up instead.

The modules under test are written into a temporary package on `sys.path`, so this file also
doubles as the worked example of what a client module looks like.
"""

import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest

import customisations
from customisations import Customisation, CustomisationError
from extraction.base import ElementKind, ExtractionError
from extraction.factory import build_extractor
from extraction.readers.registry import build_registry
from settings import ExtractionConfig

CLIENT_READER = '''
    from pathlib import Path

    from extraction.base import DocumentElement, ElementKind
    from extraction.elements import ElementList
    from extraction.readers.base import ReadResult


    class LedgerReader:
        """A format only this client has."""

        version = 1

        @property
        def name(self) -> str:
            return "ledger"

        @property
        def extensions(self) -> tuple[str, ...]:
            return (".ledger", ".md")

        def read(self, path: Path) -> ReadResult:
            elements = ElementList()
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    elements.add(
                        DocumentElement(kind=ElementKind.TABLE_ROW, text=line, table="Ledger")
                    )
            text, placed = elements.build()
            return ReadResult(text, placed, {"encoding": "utf-8"})
'''

CLIENT_EXTRACTOR = '''
    from collections.abc import Iterator

    from extraction.base import ExtractedDocument, SourceCandidate


    class LedgerCandidate(SourceCandidate):
        def read(self) -> ExtractedDocument:
            return ExtractedDocument("ledger-api", self.source_uri, "row one", {}, ())


    class LedgerApiExtractor:
        """A data source the shared code has no way to enumerate."""

        def __init__(self, config: object) -> None:
            self.config = config

        @property
        def source_type(self) -> str:
            return "ledger-api"

        def candidates(self) -> Iterator[SourceCandidate]:
            yield LedgerCandidate(
                source_type="ledger-api", source_uri="2026-Q3", source_hash="abc", reader="ledger"
            )
'''


def write_module(root: Path, name: str, body: str, init: str) -> None:
    package = root / "customisations" / name
    package.mkdir(parents=True)
    if body:
        (package / "parts.py").write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    (package / "__init__.py").write_text(textwrap.dedent(init).lstrip(), encoding="utf-8")


@pytest.fixture
def client_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A temporary `customisations.<name>` tree, importable for the duration of one test.

    `customisations` is a real package in this repo, so the temporary directory is appended to
    its `__path__` rather than shadowing it — which is exactly how a client module is added in a
    deployment: a directory alongside the ones already there.
    """
    root = tmp_path / "site"
    (root / "customisations").mkdir(parents=True)
    monkeypatch.syspath_prepend(str(root))
    monkeypatch.setattr(
        customisations, "__path__", [*customisations.__path__, str(root / "customisations")]
    )
    yield root
    for module in [m for m in sys.modules if m.startswith("customisations.")]:
        del sys.modules[module]


# --- nothing configured --------------------------------------------------------------------------


@pytest.mark.parametrize("name", [None, "", "   "])
def test_no_customisation_loads_nothing(name: str | None) -> None:
    loaded = customisations.load(name)

    assert loaded.readers == ()
    assert loaded.extractors == {}
    assert loaded.describe() == "readers: none; extractors: none"


# --- failing loudly ------------------------------------------------------------------------------


def test_a_name_that_does_not_resolve_stops_start_up() -> None:
    """Not a fallback to none: the client's reader silently missing is a wrong-answer bug."""
    with pytest.raises(CustomisationError, match="does not exist"):
        customisations.load("no_such_client")


def test_a_module_without_the_expected_object_stops_start_up(client_package: Path) -> None:
    write_module(client_package, "halfdone", "", init="x = 1\n")

    with pytest.raises(CustomisationError, match="must define"):
        customisations.load("halfdone")


def test_a_module_whose_object_is_the_wrong_type_stops_start_up(client_package: Path) -> None:
    write_module(client_package, "confused", "", init='customisation = "acme"\n')

    with pytest.raises(CustomisationError, match="must define"):
        customisations.load("confused")


def test_an_error_inside_the_client_module_is_not_swallowed(client_package: Path) -> None:
    write_module(client_package, "broken", "", init="raise ValueError('bad client code')\n")

    with pytest.raises(ValueError, match="bad client code"):
        customisations.load("broken")


# --- contributing a reader -----------------------------------------------------------------------


def acme(client_package: Path, init: str) -> Customisation:
    write_module(client_package, "acme", CLIENT_READER, init)
    return customisations.load("acme")


def test_a_client_reader_is_loaded_and_described(client_package: Path) -> None:
    loaded = acme(
        client_package,
        """
        from customisations import Customisation
        from customisations.acme.parts import LedgerReader

        customisation = Customisation(readers=(LedgerReader(),))
        """,
    )

    assert [r.name for r in loaded.readers] == ["ledger"]
    assert loaded.describe() == "readers: ledger; extractors: none"


def test_a_client_reader_handles_its_own_extension(client_package: Path) -> None:
    loaded = acme(
        client_package,
        """
        from customisations import Customisation
        from customisations.acme.parts import LedgerReader

        customisation = Customisation(readers=(LedgerReader(),))
        """,
    )

    registry = build_registry(extra=loaded.readers)

    assert registry.for_path(Path("q3.ledger")).name == "ledger"


def test_a_client_reader_wins_an_extension_the_shared_code_also_handles(
    client_package: Path,
) -> None:
    """Which is the point: a client whose .md files are not Markdown needs to say so."""
    loaded = acme(
        client_package,
        """
        from customisations import Customisation
        from customisations.acme.parts import LedgerReader

        customisation = Customisation(readers=(LedgerReader(),))
        """,
    )

    assert build_registry().for_path(Path("notes.md")).name == "markdown"
    assert build_registry(extra=loaded.readers).for_path(Path("notes.md")).name == "ledger"


def test_a_client_reader_reaches_the_extractor_and_its_elements_chunk_normally(
    client_package: Path, tmp_path: Path
) -> None:
    """A reader is never a fork of the chunker: its elements go through the same rules."""
    loaded = acme(
        client_package,
        """
        from customisations import Customisation
        from customisations.acme.parts import LedgerReader

        customisation = Customisation(readers=(LedgerReader(),))
        """,
    )
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "q3.ledger").write_text("1000 rent\n250 coffee\n", encoding="utf-8")
    config = ExtractionConfig(provider="filesystem", path=corpus, extensions=(".ledger",))

    documents = [c.read() for c in build_extractor(config, loaded).candidates()]

    assert [d.source_uri for d in documents] == ["q3.ledger"]
    assert [e.kind for e in documents[0].elements] == [ElementKind.TABLE_ROW] * 2
    assert documents[0].metadata["reader"] == "ledger"


# --- contributing an extractor -------------------------------------------------------------------


def test_a_client_extractor_is_selectable_by_provider_name(client_package: Path) -> None:
    write_module(
        client_package,
        "ledgerco",
        CLIENT_EXTRACTOR,
        """
        from customisations import Customisation
        from customisations.ledgerco.parts import LedgerApiExtractor

        customisation = Customisation(extractors={"ledger_api": LedgerApiExtractor})
        """,
    )
    loaded = customisations.load("ledgerco")
    config = ExtractionConfig(provider="ledger_api", path=Path("."), extensions=(".md",))

    extractor = build_extractor(config, loaded)

    assert extractor.source_type == "ledger-api"
    assert [c.source_uri for c in extractor.candidates()] == ["2026-Q3"]
    assert loaded.describe() == "readers: none; extractors: ledger_api"


def test_an_unknown_provider_names_the_ones_that_exist() -> None:
    """Checked while the extractor is built — the start of a run, not part way through one."""
    config = ExtractionConfig(provider="carrier_pigeon", path=Path("."), extensions=(".md",))

    with pytest.raises(ValueError, match="available: filesystem"):
        build_extractor(config)


def test_the_built_in_provider_still_works_with_no_customisation(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("# Title\n\nBody.\n", encoding="utf-8")
    config = ExtractionConfig(provider="filesystem", path=tmp_path, extensions=(".md",))

    assert [c.source_uri for c in build_extractor(config).candidates()] == ["a.md"]


def test_a_client_extractor_that_cannot_read_a_source_still_reports_it(
    client_package: Path,
) -> None:
    """The shared contract holds for client code too: ExtractionError, logged, run continues."""
    write_module(
        client_package,
        "failco",
        """
        from collections.abc import Iterator

        from extraction.base import ExtractedDocument, ExtractionError, SourceCandidate


        class Unreadable(SourceCandidate):
            def read(self) -> ExtractedDocument:
                raise ExtractionError("2026-Q3: the ledger API is down")


        class FailingExtractor:
            def __init__(self, config: object) -> None:
                pass

            @property
            def source_type(self) -> str:
                return "ledger-api"

            def candidates(self) -> Iterator[SourceCandidate]:
                yield Unreadable(
                    source_type="ledger-api", source_uri="2026-Q3", source_hash="abc"
                )
        """,
        """
        from customisations import Customisation
        from customisations.failco.parts import FailingExtractor

        customisation = Customisation(extractors={"ledger_api": FailingExtractor})
        """,
    )
    loaded = customisations.load("failco")
    config = ExtractionConfig(provider="ledger_api", path=Path("."), extensions=(".md",))

    candidate = next(iter(build_extractor(config, loaded).candidates()))

    with pytest.raises(ExtractionError, match="ledger API is down"):
        candidate.read()
