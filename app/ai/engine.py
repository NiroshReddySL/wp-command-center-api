import json
import re
from typing import Any, TypeVar

from openai import AsyncOpenAI

from app.config import settings

T = TypeVar("T")

MODEL = "gpt-4o"
# Cheaper/faster tier for high-volume, well-structured tasks — a per-post
# recommendation ("3 fixes for these specific issues") doesn't need the
# flagship model, and an enterprise site can trigger thousands of these.
FAST_MODEL = "gpt-4o-mini"


class AIEngine:
    def __init__(self) -> None:
        self._client: AsyncOpenAI | None = None

    @property
    def client(self) -> AsyncOpenAI:
        if self._client is None:
            # SDK default timeout is ~10 min — far too long for agent runs.
            self._client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY, timeout=30.0, max_retries=2)
        return self._client

    async def analyze(
        self,
        prompt: str,
        system: str | None = None,
        max_tokens: int = 2048,
        json_mode: bool = False,
        model: str = MODEL,
    ) -> str:
        kwargs: dict[str, Any] = {}
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        response = await self.client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            messages=[
                {
                    "role": "system",
                    "content": system or "You are an AI assistant helping analyze WordPress site performance and content.",
                },
                {"role": "user", "content": prompt},
            ],
            **kwargs,
        )
        return response.choices[0].message.content or ""

    async def generate_json(
        self, prompt: str, schema_hint: str = "", max_tokens: int = 2048, model: str = MODEL,
    ) -> dict[str, Any]:
        system = (
            "You are an AI assistant. Always respond with valid JSON only. "
            "Do not include markdown code blocks, explanations, or any text outside the JSON."
        )
        if schema_hint:
            system += f"\n\nExpected schema: {schema_hint}"

        result = await self.analyze(prompt, system=system, max_tokens=max_tokens, json_mode=True, model=model)
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", result, re.DOTALL)
            if match:
                return json.loads(match.group())
            return {}


ai_engine = AIEngine()
