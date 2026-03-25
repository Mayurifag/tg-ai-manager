import google.genai
from google.genai import errors as genai_errors  # noqa: F401 – re-exported for callers

from src.ai.ports import AIClassifier

_DEFAULT_PROMPT_SUFFIX = (
    "\n\nIs this message an advertisement or spam? "
    "Reply with exactly one word: true or false."
)


class GeminiClassifier(AIClassifier):
    """AIClassifier implementation backed by Google Gemini via google-genai SDK.

    The client is created fresh inside classify_is_ad to keep the instance
    stateless (safe for concurrent use; no persistent auth state on self).
    APIError propagates to the caller — resilience is handled in S03.
    """

    def __init__(self, api_key: str, model: str) -> None:
        self._api_key = api_key
        self._model = model

    async def classify_is_ad(self, text: str) -> bool:
        client = google.genai.Client(api_key=self._api_key)
        prompt = text + _DEFAULT_PROMPT_SUFFIX
        response = await client.aio.models.generate_content(
            model=self._model,
            contents=prompt,
        )
        return response.text.strip().lower() == "true"
