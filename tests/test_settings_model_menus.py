"""The Advanced Settings cloud-cleanup model menus get their choices.

1.3.0 imported a GROQ_CLEANUP_MODELS that cloud_keys never defined, so every
call raised ImportError and the OpenAI / Anthropic provider panes stopped
building halfway.
"""

import pytest

import wayfinder_main
from wayfinder.core import cloud_keys


@pytest.mark.parametrize(
    "provider, expected",
    [
        ("openai", cloud_keys.OPENAI_CLEANUP_MODELS),
        ("anthropic", cloud_keys.ANTHROPIC_CLEANUP_MODELS),
        ("groq", cloud_keys.GROQ_CLEANUP_MODELS),
    ],
)
def test_each_provider_menu_lists_its_models(provider, expected):
    models = wayfinder_main._cloud_cleanup_models(provider)
    assert models == list(expected) and models


def test_groq_menu_offers_the_configured_default():
    from wayfinder.config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["groq_llm_model"] in cloud_keys.GROQ_CLEANUP_MODELS
