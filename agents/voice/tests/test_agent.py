import json

import pytest

import agent
import providers


def test_parse_splits_provider_and_model():
    assert providers.parse("llm", "openai/gpt-4.1-mini") == ("openai", "gpt-4.1-mini")
    assert providers.parse("llm", " anthropic/claude-haiku-4-5 ") == (
        "anthropic",
        "claude-haiku-4-5",
    )


@pytest.mark.parametrize("spec", ["", "openai", "openai/", "/gpt-4.1-mini"])
def test_parse_rejects_malformed_spec(spec):
    with pytest.raises(providers.ConfigError, match="expected provider/model"):
        providers.parse("llm", spec)


@pytest.mark.parametrize(
    ("stage", "spec"),
    [("stt", "anthropic/claude-haiku-4-5"), ("llm", "deepgram/nova-3"), ("tts", "cartesia/sonic")],
)
def test_parse_rejects_provider_the_stage_does_not_support(stage, spec):
    with pytest.raises(providers.ConfigError, match="unknown provider"):
        providers.parse(stage, spec)


def test_missing_keys_names_each_provider_once():
    specs = {
        "stt": "deepgram/nova-3",
        "llm": "anthropic/claude-haiku-4-5",
        "tts": "deepgram/aura-2-andromeda-en",
    }
    assert providers.missing_keys(specs, env={}) == ["ANTHROPIC_API_KEY", "DEEPGRAM_API_KEY"]
    assert providers.missing_keys(specs, env={"DEEPGRAM_API_KEY": "x"}) == ["ANTHROPIC_API_KEY"]
    assert providers.missing_keys(specs, env={"DEEPGRAM_API_KEY": "x", "ANTHROPIC_API_KEY": "y"}) == []


def test_missing_keys_treats_empty_value_as_missing():
    assert providers.missing_keys(agent.SPECS, env={"OPENAI_API_KEY": ""}) == ["OPENAI_API_KEY"]


def test_default_specs_are_valid_and_need_one_key():
    assert providers.missing_keys(agent.SPECS, env={}) == ["OPENAI_API_KEY"]


@pytest.mark.parametrize(
    ("build", "spec", "provider"),
    [
        (providers.build_stt, "openai/gpt-4o-mini-transcribe", "openai"),
        (providers.build_stt, "deepgram/nova-3", "deepgram"),
        (providers.build_llm, "openai/gpt-4.1-mini", "openai"),
        (providers.build_llm, "anthropic/claude-haiku-4-5", "anthropic"),
        (providers.build_tts, "openai/gpt-4o-mini-tts", "openai"),
        (providers.build_tts, "deepgram/aura-2-andromeda-en", "deepgram"),
    ],
)
def test_build_returns_the_configured_plugin(monkeypatch, build, spec, provider):
    for name in providers.KEY_ENV.values():
        monkeypatch.setenv(name, "test-key")
    plugin = build(spec)
    assert provider in type(plugin).__module__
    assert plugin.model == spec.split("/", 1)[1]


def test_job_options_default_without_metadata():
    assert agent.job_options("") == (agent.INSTRUCTIONS, agent.GREETING)
    assert agent.job_options("{}") == (agent.INSTRUCTIONS, agent.GREETING)


def test_job_options_take_overrides_from_metadata():
    metadata = json.dumps({"instructions": "Talk like a pirate.", "greeting": "Ahoy!"})
    assert agent.job_options(metadata) == ("Talk like a pirate.", "Ahoy!")


def test_job_options_empty_greeting_means_no_greeting():
    assert agent.job_options(json.dumps({"greeting": ""})) == (agent.INSTRUCTIONS, "")


@pytest.mark.parametrize("metadata", ["not json", "[1, 2]", '{"instructions": 5, "greeting": null}'])
def test_job_options_ignore_unusable_metadata(metadata):
    assert agent.job_options(metadata) == (agent.INSTRUCTIONS, agent.GREETING)
