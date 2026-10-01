"""Builds the speech-to-text, LLM and text-to-speech plugins named in the config.

Each stage is configured as `provider/model`, e.g. `openai/gpt-4o-mini-tts` or
`anthropic/claude-haiku-4-5`. Plugin objects are always built here and handed
to the session: a bare model string given to `AgentSession` would be routed
through LiveKit Cloud's inference gateway instead of the provider.
"""

import importlib
import os
from collections.abc import Mapping
from typing import Any

# environment variable holding each provider's API key
KEY_ENV = {
    "openai": "OPENAI_API_KEY",
    "deepgram": "DEEPGRAM_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}

SUPPORTED = {
    "stt": ("openai", "deepgram"),
    "llm": ("openai", "anthropic"),
    "tts": ("openai", "deepgram"),
}


class ConfigError(ValueError):
    pass


def parse(stage: str, spec: str) -> tuple[str, str]:
    """`provider/model` as (provider, model), checked against what the stage supports."""
    provider, _, model = spec.strip().partition("/")
    if not provider or not model:
        raise ConfigError(f"{stage}: expected provider/model, got {spec!r}")
    if provider not in SUPPORTED[stage]:
        raise ConfigError(
            f"{stage}: unknown provider {provider!r}; supported: {', '.join(SUPPORTED[stage])}"
        )
    return provider, model


def missing_keys(specs: Mapping[str, str], env: Mapping[str, str] | None = None) -> list[str]:
    """API key variables the configured providers need and the environment lacks."""
    env = os.environ if env is None else env
    needed = {KEY_ENV[parse(stage, spec)[0]] for stage, spec in specs.items()}
    return sorted(name for name in needed if not env.get(name))


def _plugin(provider: str) -> Any:
    return importlib.import_module(f"livekit.plugins.{provider}")


def load(specs: Mapping[str, str]) -> None:
    """Import the plugins the config names, and only those.

    Called when the worker process starts: a plugin first imported inside a
    job blocks that job's event loop for a few hundred milliseconds.
    """
    for stage, spec in specs.items():
        _plugin(parse(stage, spec)[0])


def build_stt(spec: str) -> Any:
    provider, model = parse("stt", spec)
    return _plugin(provider).STT(model=model)


def build_llm(spec: str) -> Any:
    provider, model = parse("llm", spec)
    return _plugin(provider).LLM(model=model)


def build_tts(spec: str, voice: str = "") -> Any:
    """`voice` applies to OpenAI only; a Deepgram voice is part of its model name."""
    provider, model = parse("tts", spec)
    if provider == "openai" and voice:
        return _plugin(provider).TTS(model=model, voice=voice)
    return _plugin(provider).TTS(model=model)
