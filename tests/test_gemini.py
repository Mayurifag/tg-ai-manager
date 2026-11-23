import json
import unittest
from unittest.mock import patch

import httpx2
from google import genai

from src.ai.gemini import GeminiClassifier


class GeminiSDKCompatibility(unittest.IsolatedAsyncioTestCase):
    async def test_beta_model_request_keeps_store_disabled(self):
        requests = []

        async def respond(request):
            requests.append(request)
            return httpx2.Response(
                200,
                json={
                    "candidates": [
                        {
                            "content": {
                                "role": "model",
                                "parts": [{"text": '{"is_ad":true}'}],
                            },
                            "finishReason": "STOP",
                        }
                    ]
                },
            )

        original_client = genai.Client
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:

            def client_with_mock_transport(*args, **kwargs):
                kwargs["http_options"].httpx_async_client = http
                return original_client(*args, **kwargs)

            with patch("src.ai.gemini.google.genai.Client", client_with_mock_transport):
                classifier = GeminiClassifier("test-key", "gemini-flash-latest")
            try:
                self.assertTrue(await classifier.classify_is_ad("fixture text"))
            finally:
                await classifier.close()

        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(
            request.url.path, "/v1beta/models/gemini-flash-latest:generateContent"
        )
        body = json.loads(request.content)
        self.assertIs(body["store"], False)
        self.assertEqual(
            body["generationConfig"]["responseMimeType"], "application/json"
        )
        self.assertEqual(body["contents"][0]["parts"][0]["text"], "fixture text")
