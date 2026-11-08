"""The output checks: no image survives, and no link unless its host is allowed."""

import pytest
from pydantic import ValidationError

from safety.output import OutputPolicy, OutputSanitizer
from settings import OutputSafetyConfig

pytestmark = pytest.mark.safety_output

STRICT = OutputSanitizer()
DOCS = OutputSanitizer(OutputPolicy.of(["docs.example.com"]))


def clean(text: str, sanitizer: OutputSanitizer = STRICT) -> str:
    return sanitizer.clean(text).text


# --- images: always removed ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "![x](https://evil.example/p.png?q=secret)",
        '![x](<https://evil.example/a b.png> "title")',
        "![x][ref]\n\n[ref]: https://evil.example/p.png",
        "![ref]\n\n[ref]: https://evil.example/p.png",
        '<img src="https://evil.example/p.png">',
        "<IMG SRC=https://evil.example/p.png>",
        '<picture><source srcset="https://evil.example/p.png"></picture>',
        '<svg><image href="https://evil.example/p.png"/></svg>',
        '<video poster="https://evil.example/p.png"></video>',
    ],
)
def test_every_image_is_removed(text: str) -> None:
    result = STRICT.clean(f"Before {text} after")
    assert "evil.example" not in result.text
    assert result.removed >= 1


def test_an_image_is_removed_even_from_an_allowed_host() -> None:
    assert clean("![d](https://docs.example.com/d.png)", DOCS) == ""


# --- links: removed unless allowed, keeping their label -----------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Read [the docs](https://evil.example/a).", "Read the docs."),
        ("Read [the docs](https://evil.example/a_(b)).", "Read the docs."),
        ("Read [the docs][1].\n\n[1]: https://evil.example/a 'Title'", "Read the docs.\n\n"),
        ("Read [the docs][].\n\n[the docs]: https://evil.example/a", "Read the docs.\n\n"),
        ("Read [The Docs].\n\n[the docs]: <https://evil.example/a>", "Read The Docs.\n\n"),
        ('A <a href="https://evil.example/">label</a>.', "A label."),
        ("Go to <https://evil.example/x>.", "Go to ."),
        ("Go to https://evil.example/x?q=1.", "Go to ."),
        ("Or www.evil.example/x, today", "Or , today"),
        ("[x](//evil.example/a)", "x"),
        ("[x](/relative/path)", "x"),
        ("[x](mailto:someone@evil.example)", "x"),
    ],
)
def test_a_disallowed_link_is_removed_and_keeps_its_label(text: str, expected: str) -> None:
    assert clean(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "[x](javascript:alert(1))",
        "[x](JavaScript:alert(1))",
        "[x](java&#115;cript:alert(1))",
        '<a href="java&#9;script:alert(1)">x</a>',  # browsers strip the tab before following
        '<a href="java\tscript:alert(1)">x</a>',
        "[x](data:text/html;base64,PHNjcmlwdD4=)",
        "[x](vbscript:msgbox)",
        '<a href="javascript:alert(1)">x</a>',
        "javascript:alert(document.cookie)",
        "data:text/html;base64,PHNjcmlwdD4=",
    ],
)
def test_script_and_data_urls_are_never_allowed(text: str) -> None:
    result = clean(text, DOCS)
    assert "script:" not in result.lower()
    assert "data:text" not in result


def test_an_allowed_host_keeps_its_links() -> None:
    text = "See [the docs](https://docs.example.com/a) or https://docs.example.com/b."
    assert clean(text, DOCS) == text


def test_an_allowed_host_does_not_allow_its_lookalikes() -> None:
    for url in (
        "https://docs.example.com.evil.example/",
        "https://docs.example.com@evil.example/",
        "https://evil.example/docs.example.com",
        "http://sub.docs.example.com/",
        "ftp://docs.example.com/",
    ):
        assert "evil" not in clean(f"[x]({url})", DOCS)
        assert clean(f"[x]({url})", DOCS) == "x"


def test_an_image_inside_a_link_goes_with_it() -> None:
    assert clean("[![a](https://evil.example/i)](https://evil.example/l)") == ""


def test_ordinary_text_is_left_alone() -> None:
    text = (
        "JavaScript: a language. data: 5 rows. Footnote [1] and [brackets]. a<b and c>d. "
        "Email me at someone@example.com. Code: `if (x) { y[0] = z; }`"
    )
    result = STRICT.clean(text)
    assert result.text == text
    assert result.removed == 0


def test_removals_are_logged_as_counts_never_content(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("INFO", logger="safety.output")
    STRICT.clean_text("![x](https://evil.example/secret-question)", field="answer")
    assert "removed 1" in caplog.text
    assert "secret-question" not in caplog.text


# --- configuration ------------------------------------------------------------------------------


def test_allowed_hosts_are_normalised() -> None:
    assert OutputSafetyConfig(allowed_link_hosts=("Docs.Example.com",)).allowed_link_hosts == (
        "docs.example.com",
    )


@pytest.mark.parametrize(
    "host", ["https://docs.example.com", "docs.example.com/path", "*.example.com", "a.com:8080"]
)
def test_an_allowed_host_must_be_a_bare_host_name(host: str) -> None:
    with pytest.raises(ValidationError, match="bare host name"):
        OutputSafetyConfig(allowed_link_hosts=(host,))
