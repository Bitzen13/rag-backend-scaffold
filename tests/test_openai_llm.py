"""The second LLM provider, behind the same contract as the first.

A second implementation is what makes `LLMProvider` an interface rather than the shape one class
happens to have, so what is tested here is that the guarantees belong to the contract and not to
the Anthropic SDK: bounds from config, a failure raised as `LLMError` rather than returned as an
empty answer, and the same provider selected by one config value.
"""

from types import SimpleNamespace
from typing import Any, cast

import httpx
import openai
import pytest
from openai import AsyncOpenAI

from generation.anthropic_provider import AnthropicProvider
from generation.base import LLMError
from generation.factory import build_llm_provider
from generation.openai_provider import OpenAIProvider
from settings import Settings, load_settings


class FakeCompletions:
    def __init__(
        self,
        content: str | None = "Rayleigh scattering.",
        *,
        choices: bool = True,
        fail: bool = False,
        timeout: bool = False,
    ) -> None:
        self.content = content
        self.choices = choices
        self.fail = fail
        self.timeout = timeout
        self.kwargs: dict[str, Any] = {}

    async def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        if self.timeout:
            raise openai.APITimeoutError(request=httpx.Request("POST", "https://x"))
        if self.fail:
            raise openai.APIConnectionError(request=httpx.Request("POST", "https://x"))
        if not self.choices:
            return SimpleNamespace(choices=[])
        message = SimpleNamespace(content=self.content)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def provider(completions: FakeCompletions) -> OpenAIProvider:
    chat = SimpleNamespace(completions=completions)
    return OpenAIProvider(
        "sk-test", "gpt-test", 256, client=cast(AsyncOpenAI, SimpleNamespace(chat=chat))
    )


def configured(provider_name: str, **models: Any) -> Settings:
    settings = load_settings(env_file=None)
    return settings.model_copy(
        update={
            "models": settings.models.model_copy(update={"llm_provider": provider_name, **models})
        }
    )


# --- generating ----------------------------------------------------------------------------------


async def test_the_system_prompt_and_question_are_sent_as_two_messages() -> None:
    completions = FakeCompletions()

    reply = await provider(completions).generate(system="sys", user="hi")

    assert reply == "Rayleigh scattering."
    assert completions.kwargs["model"] == "gpt-test"
    assert completions.kwargs["max_completion_tokens"] == 256
    assert completions.kwargs["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
    ]


async def test_the_reply_is_stripped() -> None:
    assert await provider(FakeCompletions("  spaced  ")).generate(system="s", user="u") == "spaced"


@pytest.mark.parametrize(
    "broken",
    [
        FakeCompletions(fail=True),
        FakeCompletions(timeout=True),
        FakeCompletions(""),
        FakeCompletions(None),
        FakeCompletions(choices=False),
    ],
)
async def test_every_failure_mode_is_an_llm_error_not_an_empty_answer(
    broken: FakeCompletions,
) -> None:
    with pytest.raises(LLMError):
        await provider(broken).generate(system="s", user="u")


def test_the_configured_bounds_reach_the_sdk_client() -> None:
    built = OpenAIProvider("sk-test", "gpt-test", 256, timeout_seconds=8.0, max_retries=1)

    assert built._client.timeout == 8.0
    assert built._client.max_retries == 1


# --- selection -----------------------------------------------------------------------------------


def test_one_config_value_chooses_between_the_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-oai-test")

    assert isinstance(build_llm_provider(configured("anthropic")), AnthropicProvider)
    assert isinstance(build_llm_provider(configured("openai")), OpenAIProvider)


def test_the_factory_passes_the_bounds_through(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-oai-test")

    built = cast(
        OpenAIProvider,
        build_llm_provider(configured("openai", llm_timeout_seconds=4.0, llm_max_retries=0)),
    )

    assert built._client.timeout == 4.0
    assert built._client.max_retries == 0


@pytest.mark.parametrize(
    ("provider_name", "variable"),
    [("anthropic", "ANTHROPIC_API_KEY"), ("openai", "OPENAI_API_KEY")],
)
def test_a_missing_secret_names_the_variable_to_set_at_start_up(
    provider_name: str, variable: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At build time, not on the first question a consumer asks."""
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(RuntimeError, match=variable):
        build_llm_provider(configured(provider_name))
