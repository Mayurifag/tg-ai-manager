import aiohttp
from src.ai.ports import AIProvider
from src.infrastructure.logging import get_logger

logger = get_logger(__name__)


class OpenAICompatibleProvider(AIProvider):
    def __init__(self, api_key: str, base_url: str, model: str):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model

    async def complete(self, prompt: str, system: str = None) -> str:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload = {"model": self.model, "messages": messages, "temperature": 0.7}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}/chat/completions", headers=headers, json=payload
                ) as resp:
                    if resp.status != 200:
                        err = await resp.text()
                        logger.error("openai_api_error", status=resp.status, error=err)
                        return ""

                    data = await resp.json()
                    return data["choices"][0]["message"]["content"]
        except Exception as e:
            logger.error("openai_request_failed", error=str(e))
            return ""

    async def classify(self, text: str, categories: list[str]) -> str:
        cats_str = ", ".join(categories)
        system = f"You are a classifier. Classify the user text into one of: {cats_str}. Return ONLY the category name."
        result = await self.complete(text, system=system)

        result = result.strip()
        for c in categories:
            if c.lower() == result.lower():
                return c
        # Fuzzy match
        for c in categories:
            if c.lower() in result.lower():
                return c
        return categories[0]
