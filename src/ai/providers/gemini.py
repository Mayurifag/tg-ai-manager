import aiohttp
from src.ai.ports import AIProvider
from src.infrastructure.logging import get_logger

logger = get_logger(__name__)


class GeminiProvider(AIProvider):
    def __init__(self, api_key: str, model: str = "gemini-pro"):
        self.api_key = api_key
        self.model = model
        self.url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    async def complete(self, prompt: str, system: str = None) -> str:
        if not self.api_key:
            return ""

        headers = {"Content-Type": "application/json"}

        # Gemini doesn't support 'system' role in v1beta consistently across models via simple API,
        # so we prepend it to the prompt for robustness.
        full_prompt = prompt
        if system:
            full_prompt = f"{system}\n\nUser: {prompt}"

        payload = {"contents": [{"parts": [{"text": full_prompt}]}]}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.url}?key={self.api_key}", headers=headers, json=payload
                ) as resp:
                    if resp.status != 200:
                        err = await resp.text()
                        logger.error("gemini_api_error", status=resp.status, error=err)
                        return ""

                    data = await resp.json()
                    try:
                        return data["candidates"][0]["content"]["parts"][0]["text"]
                    except (KeyError, IndexError):
                        return ""
        except Exception as e:
            logger.error("gemini_request_failed", error=str(e))
            return ""

    async def classify(self, text: str, categories: list[str]) -> str:
        # Gemini is a chat model, so we prompt it to classify
        cats_str = ", ".join(categories)
        prompt = (
            f"Classify the following text into exactly one of these categories: [{cats_str}].\n"
            f"Reply ONLY with the category name.\n\n"
            f"Text: {text}"
        )
        result = await self.complete(prompt)
        result = result.strip()

        # Basic cleanup
        for c in categories:
            if c.lower() in result.lower():
                return c
        return categories[0]  # Fallback
