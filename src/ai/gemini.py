import google.genai
from google.genai import errors as genai_errors  # noqa: F401 – re-exported for callers

from src.ai.ports import AIClassifier

_DEFAULT_PROMPT_SUFFIX = (
    "\n\nIs this message an advertisement or spam? "
    "Reply with exactly one word: true or false."
)


class GeminiClassifier(AIClassifier):
    """AIClassifier implementation backed by Google Gemini via google-genai SDK."""

    def __init__(self, api_key: str, model: str, prompt: str | None = None) -> None:
        self._model = model
        self._prompt = prompt
        self._client = google.genai.Client(api_key=api_key)

    async def classify_is_ad(self, text: str) -> bool:
        prompt = f"{self._prompt}\n\n{text}" if self._prompt else text
        prompt += _DEFAULT_PROMPT_SUFFIX
        response = await self._client.aio.models.generate_content(
            model=self._model,
            contents=prompt,
        )
        return response.text.strip().lower() == "true"
