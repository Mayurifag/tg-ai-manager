"""Tests for AIClassifier ABC and GeminiClassifier.

All tests mock google.genai.Client so no live API calls are made.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.ai.ports import AIClassifier
from src.ai.gemini import GeminiClassifier


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_client(response_text: str) -> MagicMock:
    """Return a fully-mocked google.genai.Client whose aio.models.generate_content
    returns a response with the given text."""
    mock_response = MagicMock()
    mock_response.text = response_text

    mock_aio_models = MagicMock()
    mock_aio_models.generate_content = AsyncMock(return_value=mock_response)

    mock_aio = MagicMock()
    mock_aio.models = mock_aio_models

    mock_client = MagicMock()
    mock_client.aio = mock_aio
    return mock_client


# ---------------------------------------------------------------------------
# Interface contract
# ---------------------------------------------------------------------------


def test_gemini_classifier_is_ai_classifier():
    """GeminiClassifier must be a concrete implementation of AIClassifier."""
    classifier = GeminiClassifier(api_key="test-key", model="gemini-2.0-flash")
    assert isinstance(classifier, AIClassifier)


# ---------------------------------------------------------------------------
# Happy-path: various "true" / "false" response forms
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response_text, expected",
    [
        ("true", True),
        ("True", True),
        ("TRUE", True),
        ("  true  ", True),
        ("false", False),
        ("False", False),
        ("FALSE", False),
        ("  false\n", False),
    ],
)
async def test_classify_is_ad_parses_response(response_text: str, expected: bool):
    classifier = GeminiClassifier(api_key="test-key", model="gemini-2.0-flash")
    with patch(
        "src.ai.gemini.google.genai.Client", return_value=_mock_client(response_text)
    ):
        result = await classifier.classify_is_ad("Buy cheap stuff now!")
    assert result is expected


# ---------------------------------------------------------------------------
# Client is created per call (stateless)
# ---------------------------------------------------------------------------


async def test_classify_creates_client_per_call():
    """A new google.genai.Client is instantiated on every classify_is_ad call."""
    classifier = GeminiClassifier(api_key="my-key", model="gemini-2.0-flash")
    with patch(
        "src.ai.gemini.google.genai.Client", return_value=_mock_client("true")
    ) as mock_cls:
        await classifier.classify_is_ad("some text")
        await classifier.classify_is_ad("some text")
    assert mock_cls.call_count == 2


# ---------------------------------------------------------------------------
# API error propagation
# ---------------------------------------------------------------------------


async def test_api_error_propagates():
    """google.genai.errors.APIError must bubble up unchanged (no suppression)."""
    import google.genai.errors as genai_errors

    classifier = GeminiClassifier(api_key="bad-key", model="gemini-2.0-flash")

    # Build a minimal mock client whose generate_content raises APIError.
    mock_aio_models = MagicMock()
    mock_aio_models.generate_content = AsyncMock(
        side_effect=genai_errors.APIError(500, "quota exceeded")
    )
    mock_aio = MagicMock()
    mock_aio.models = mock_aio_models
    mock_client = MagicMock()
    mock_client.aio = mock_aio

    with patch("src.ai.gemini.google.genai.Client", return_value=mock_client):
        with pytest.raises(genai_errors.APIError):
            await classifier.classify_is_ad("some ad text")
