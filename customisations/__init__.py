"""The boundary where one client's needs live, so N deployments stay one codebase.

Every client runs the same image against its own database and its own documents (see
*Considered and not adopted* in context/design-decisions.md). Almost everything a client
needs differently is already configuration: which documents, which chunking, which models,
which limits. What is left over — a file format only they have, a data source only they
use — needs code, and code is where per-client work turns into per-client forks.

The failure this exists to prevent is five copies of the pipeline in which one bug has to be
found and fixed five times, differently. So client code does not go in the pipeline. It goes in a
module under `customisations/` that the shared codebase does not own, reached only through the
Protocols the pipeline already defines, and named in one config value:

    customisation: acme          # loads customisations/acme

That module exposes a single `customisation` object saying what it provides:

    # customisations/acme/__init__.py
    from customisations import Customisation
    from customisations.acme.readers import InvoiceReader

    customisation = Customisation(readers=(InvoiceReader(),))

What it may provide is deliberately narrow, and each hook is an existing Protocol:

* **readers** — extra `Reader`s, merged into the registry ahead of the built-in ones, so a client
  can claim an extension the shared code also handles. Their elements go through the same
  chunking rules as every other format, so a reader is never a fork of the chunker
  (`context/adding-a-reader.md`).
* **extractors** — extra `Extractor`s by `extraction.provider` name, for a data source the shared
  code has no way to enumerate (`context/adding-an-extractor.md`).

Anything a client asks for that does not fit one of those is a signal, not an obstacle: either it
belongs in config, or the element model is missing a field, or it is something every client would
want and belongs in the shared codebase. Reaching for a fork is the one answer that is wrong,
because a fix written for one client can no longer reach the others.
"""

import importlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from extraction.base import Extractor
from extraction.readers.base import Reader

if TYPE_CHECKING:  # only for the annotation; importing settings here would be a cycle
    from settings import ExtractionConfig

PACKAGE = "customisations"

# An extractor factory is handed the extraction config and returns the extractor to run with.
ExtractorFactory = Callable[["ExtractionConfig"], Extractor]


class CustomisationError(RuntimeError):
    """The named customisation could not be loaded, or does not expose what it must."""


@dataclass(frozen=True)
class Customisation:
    """What one client's module contributes. Every field is optional."""

    readers: Sequence[Reader] = ()
    extractors: Mapping[str, ExtractorFactory] = field(default_factory=dict)

    def describe(self) -> str:
        """A line for the start-up log, so a deployment says out loud what it is running with."""
        readers = ", ".join(sorted(r.name for r in self.readers)) or "none"
        extractors = ", ".join(sorted(self.extractors)) or "none"
        return f"readers: {readers}; extractors: {extractors}"


NONE = Customisation()  # what a deployment with no customisation runs with


def load(name: str | None) -> Customisation:
    """The customisation named in config, or `NONE`.

    Raises `CustomisationError` rather than falling back to `NONE` for a name that does not
    resolve: a client-specific reader that silently did not load would mean their documents were
    quietly ingested by the wrong one, which is worse than not starting.
    """
    if name is None or not name.strip():
        return NONE
    module_name = f"{PACKAGE}.{name.strip()}"
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        raise CustomisationError(
            f"customisation {name!r} does not exist: expected a module at {module_name}"
        ) from exc
    found = getattr(module, "customisation", None)
    if not isinstance(found, Customisation):
        raise CustomisationError(
            f"{module_name} must define `customisation = Customisation(...)`; "
            f"found {type(found).__name__}"
        )
    return found
