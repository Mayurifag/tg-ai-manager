from src.ai.ports import AIProvider


class AdClassifier:
    def __init__(self, provider: AIProvider):
        self.provider = provider

    async def is_ad(self, text: str) -> bool:
        if not text or len(text.split()) < 5:
            # Too short to be a meaningful ad usually
            return False

        # Prompt engineering
        result = await self.provider.classify(
            text, categories=["ADVERTISEMENT", "NORMAL_MESSAGE"]
        )
        return result == "ADVERTISEMENT"
