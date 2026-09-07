from __future__ import annotations

import json
import math
import re
from time import perf_counter
from typing import Any

import httpx
from ..validation import validate_prompt_template

from .base import (
    BaseEvaluator,
    PermanentEvaluatorError,
    RetriableEvaluatorError,
    ScoreInput,
    ScoreOutput,
    finite_number,
)


DEFAULT_SYSTEM_TEMPLATE = """你是一名严格的外文到中文翻译质量评审员。请比较源文、中文参考译文和候选译文，从准确性、完整性、流畅性综合评分。只返回 JSON。"""
DEFAULT_USER_TEMPLATE = """源语种：{source_language}
源文：{source_text}
中文参考译文：{reference_zh}
候选译文：{translation_zh}

返回格式：{{"score": 0到10之间的浮点数, "reason": "简短中文理由"}}"""


async def check_openai_compatible_connection(
    config: dict[str, Any], *, transport: httpx.AsyncBaseTransport | None = None
) -> dict[str, Any]:
    """通过 OpenAI 兼容的 /models 接口检查服务、凭证和模型名。"""
    normalized = OpenAICompatibleEvaluator.validate_config(config)
    started = perf_counter()
    try:
        async with httpx.AsyncClient(
            base_url=normalized["base_url"].rstrip("/") + "/",
            timeout=min(float(normalized["timeout_seconds"]), 10.0),
            headers={"Authorization": f"Bearer {normalized['api_key']}"},
            transport=transport,
        ) as client:
            response = await client.get("models")
    except httpx.HTTPError as exc:
        return {
            "status": "disconnected",
            "detail": f"无法连接模型服务：{exc.__class__.__name__}",
            "latency_ms": None,
            "model_available": None,
        }

    latency_ms = round((perf_counter() - started) * 1000)
    if response.status_code >= 400:
        if response.status_code in {401, 403}:
            detail = f"模型服务已响应，但身份验证失败（HTTP {response.status_code}）"
        else:
            detail = f"模型服务返回 HTTP {response.status_code}"
        return {
            "status": "disconnected",
            "detail": detail,
            "latency_ms": latency_ms,
            "model_available": None,
        }

    model_ids: set[str] = set()
    try:
        payload = response.json()
        if isinstance(payload, dict) and isinstance(payload.get("data"), list):
            model_ids = {
                str(item["id"])
                for item in payload["data"]
                if isinstance(item, dict) and item.get("id") is not None
            }
    except (TypeError, ValueError):
        # 某些兼容服务的 /models 不返回标准结构，但 2xx 仍说明服务可达。
        pass

    configured_model = str(normalized["model"])
    model_available = configured_model in model_ids if model_ids else None
    if model_available is False:
        return {
            "status": "disconnected",
            "detail": f"服务可达，但未找到模型 {configured_model}",
            "latency_ms": latency_ms,
            "model_available": False,
        }
    return {
        "status": "connected",
        "detail": f"模型列表包含 {configured_model}，实际评分尚未试跑" if model_available else "服务可达，响应未提供可核验的模型列表",
        "latency_ms": latency_ms,
        "model_available": model_available,
    }


class _SafeValues(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def render_template(template: str, item: ScoreInput) -> str:
    values = _SafeValues(
        source_language=item.source_language,
        source_text=item.source_text,
        reference_zh=item.reference_zh,
        translation_zh=item.translation_zh,
    )
    return template.format_map(values)


def parse_json_content(content: str) -> dict[str, Any]:
    if not isinstance(content, str) or not content.strip():
        raise RetriableEvaluatorError("评分响应 content 必须是非空文本")
    stripped = content.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", stripped, re.DOTALL | re.IGNORECASE)
    if fenced:
        stripped = fenced.group(1)
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as exc:
        match = re.search(r"\{.*\}", stripped, re.DOTALL)
        if not match:
            raise RetriableEvaluatorError("评分模型未返回 JSON 对象") from exc
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError as nested:
            raise RetriableEvaluatorError("评分模型返回的 JSON 无法解析") from nested
    if not isinstance(data, dict):
        raise RetriableEvaluatorError("评分响应必须是 JSON 对象")
    return data


class OpenAICompatibleEvaluator(BaseEvaluator):
    evaluator_type = "openai_compatible_llm"

    def __init__(
        self,
        config: dict[str, Any],
        *,
        system_template: str = DEFAULT_SYSTEM_TEMPLATE,
        user_template: str = DEFAULT_USER_TEMPLATE,
    ) -> None:
        normalized = self.validate_config(config)
        super().__init__(normalized)
        self.system_template = validate_prompt_template(system_template)
        self.user_template = validate_prompt_template(user_template)
        self.client = httpx.AsyncClient(
            base_url=normalized["base_url"].rstrip("/") + "/",
            timeout=normalized["timeout_seconds"],
            headers={"Authorization": f"Bearer {normalized['api_key']}"},
        )

    @classmethod
    def validate_config(cls, config: dict[str, Any]) -> dict[str, Any]:
        for name in ("base_url", "model", "api_key"):
            if not isinstance(config.get(name, ""), str):
                raise ValueError(f"{name} 必须是文本")
        base_url = config.get("base_url", "").strip()
        model = config.get("model", "").strip()
        api_key = config.get("api_key", "").strip()
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("base_url 必须以 http:// 或 https:// 开头")
        if not model:
            raise ValueError("必须填写评分模型名")
        if not api_key:
            raise ValueError("必须填写 API Key")
        concurrency = finite_number(config.get("concurrency", 8), "concurrency", 1, 64, integer=True)
        timeout = finite_number(config.get("timeout_seconds", 60), "timeout_seconds", 0.01, 3600)
        retries = finite_number(config.get("max_retries", 3), "max_retries", 0, 10, integer=True)
        cache_policy = config.get("cache_policy", "content_model")
        if not isinstance(cache_policy, str) or cache_policy not in {"content_model", "strict_revision"}:
            raise ValueError("cache_policy 必须为 content_model 或 strict_revision")
        return {
            "base_url": base_url,
            "model": model,
            "api_key": api_key,
            "concurrency": concurrency,
            "timeout_seconds": timeout,
            "max_retries": retries,
            "temperature": finite_number(config.get("temperature", 0), "temperature", 0, 2),
            "max_tokens": finite_number(config.get("max_tokens", 256), "max_tokens", 1, 131072, integer=True),
            "cache_policy": cache_policy,
        }

    @property
    def model_name(self) -> str:
        return str(self.config["model"])

    @property
    def base_url(self) -> str:
        return str(self.config["base_url"])

    async def evaluate_one(self, item: ScoreInput) -> ScoreOutput:
        payload = {
            "model": self.model_name,
            "messages": [
                {"role": role, "content": render_template(template, item)}
                for role, template in (
                    ("system", self.system_template),
                    ("user", self.user_template),
                )
                if template.strip()
            ],
            "temperature": self.config["temperature"],
            "max_tokens": self.config["max_tokens"],
            "response_format": {"type": "json_object"},
        }
        try:
            response = await self.client.post("chat/completions", json=payload)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise RetriableEvaluatorError(f"评分服务网络错误: {exc}") from exc
        if response.status_code == 429 or response.status_code >= 500:
            raise RetriableEvaluatorError(
                f"评分服务暂时不可用 ({response.status_code}): {response.text[:300]}"
            )
        if response.status_code >= 400:
            raise PermanentEvaluatorError(
                f"评分服务拒绝请求 ({response.status_code}): {response.text[:300]}"
            )
        try:
            raw = response.json()
            if not isinstance(raw, dict) or not isinstance(raw.get("choices"), list) or not raw["choices"]:
                raise RetriableEvaluatorError("评分响应缺少有效 choices")
            choice = raw["choices"][0]
            if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
                raise RetriableEvaluatorError("评分响应缺少有效 message")
            message = choice["message"]
            if message.get("refusal") or choice.get("finish_reason") == "content_filter":
                raise PermanentEvaluatorError("评分模型拒绝了本次评分请求")
            content = message.get("content")
            data = parse_json_content(content)
            value = data["score"]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise RetriableEvaluatorError("评分响应 score 必须是 JSON 数值")
            score = float(value)
            reason = str(data.get("reason", ""))
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise RetriableEvaluatorError("评分响应缺少有效 score") from exc
        if not math.isfinite(score) or not 0.0 <= score <= 10.0:
            raise RetriableEvaluatorError(f"评分 {score} 超出 0–10 范围")
        return ScoreOutput(
            score=score,
            score_min=0.0,
            score_max=10.0,
            unit="point",
            reason=reason,
            raw_response=raw,
        )

    async def close(self) -> None:
        await self.client.aclose()
