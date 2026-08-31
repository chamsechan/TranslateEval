from __future__ import annotations

import httpx
import pytest

from app.evaluators.base import RetriableEvaluatorError, ScoreInput
from app.evaluators.bleu import SacreBleuZhEvaluator
from app.evaluators.llm import OpenAICompatibleEvaluator, parse_json_content, render_template


@pytest.mark.asyncio
async def test_bleu_exact_match_is_100() -> None:
    evaluator = SacreBleuZhEvaluator({"tokenize": "zh", "smooth_method": "exp"})
    result = await evaluator.evaluate_one(ScoreInput("de", "Hallo", "早上好", "早上好"))
    assert result.score == pytest.approx(100.0)
    assert result.score_max == 100.0


def test_llm_json_parser_and_template() -> None:
    assert parse_json_content('```json\n{"score": 8.5, "reason": "好"}\n```')["score"] == 8.5
    item = ScoreInput("de", "Hallo", "你好", "您好")
    assert render_template("{source_language}:{translation_zh}", item) == "de:您好"
    with pytest.raises(RetriableEvaluatorError):
        parse_json_content("not-json")


@pytest.mark.asyncio
async def test_openai_compatible_structured_score() -> None:
    evaluator = OpenAICompatibleEvaluator(
        {"base_url": "https://judge.invalid/v1", "model": "judge-a", "api_key": "secret"}
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"score": 9.25, "reason": "准确"}'}}]},
        )

    await evaluator.client.aclose()
    evaluator.client = httpx.AsyncClient(
        base_url="https://judge.invalid/v1/",
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer secret"},
    )
    result = await evaluator.evaluate_one(ScoreInput("de", "Hallo", "你好", "你好"))
    assert result.score == 9.25
    assert result.reason == "准确"
    await evaluator.close()

