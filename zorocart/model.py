"""HTTP client for the Trida Stack Zoro-1 decide server."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import math
import re
from time import monotonic
from typing import Any, cast
from urllib.parse import urlsplit

import httpx


class ModelError(RuntimeError):
    """The model could not complete a request."""


class ModelResponseError(ModelError):
    """The server answered with unusable data."""


@dataclass(frozen=True)
class ChoiceResult:
    choice: str
    probabilities: dict[str, float]

    @property
    def probability(self) -> float:
        return self.probabilities[self.choice]


@dataclass(frozen=True)
class ScoreResult:
    score: float
    probabilities: dict[str, float]


DecisionResult = ChoiceResult | ScoreResult
Event = Callable[..., None]


class ZoroClient:
    def __init__(
        self,
        base_url: str,
        *,
        model: str = "zoro-1",
        api_key: str | None = None,
        timeout: float = 30.0,
        event: Event | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        endpoint = urlsplit(base_url)
        if (
            endpoint.scheme not in {"http", "https"}
            or not endpoint.hostname
            or endpoint.username is not None
            or endpoint.query
            or endpoint.fragment
        ):
            raise ValueError("Zoro-1 서버 주소는 인증 정보가 없는 HTTP(S) 주소여야 합니다.")
        if not model.strip():
            raise ValueError("모델 이름이 필요합니다.")
        root = base_url.rstrip("/")
        root = root if endpoint.path.rstrip("/").endswith("/v1") else f"{root}/v1"
        self.model = model
        self._pinned_model = model if re.fullmatch(r"[0-9a-f]{40}", model) else None
        self.event = event
        self._http = httpx.AsyncClient(
            base_url=f"{root}/",
            headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
            timeout=timeout,
            transport=transport,
            trust_env=False,
        )

    async def __aenter__(self) -> "ZoroClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        started = monotonic()
        status: int | None = None
        result = "error"
        try:
            response = await self._http.request(method, path, json=body)
            status = response.status_code
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ModelResponseError("Zoro-1 응답이 JSON 객체 형식이 아닙니다.")
            if method == "POST" and self._pinned_model is not None and data.get("model") != self._pinned_model:
                raise ModelError("Zoro-1 응답의 모델이 고정한 체크포인트와 일치하지 않습니다.")
            result = "received"
            return data
        except httpx.HTTPStatusError as exc:
            raise ModelError(f"Zoro-1 {path} 요청이 HTTP {exc.response.status_code}로 실패했습니다.") from None
        except httpx.RequestError:
            raise ModelError(f"Zoro-1 {path} 서버에 연결하거나 응답을 받지 못했습니다.") from None
        except ValueError:
            raise ModelResponseError("Zoro-1 서버 응답을 JSON으로 읽을 수 없습니다.") from None
        finally:
            if self.event:
                self.event(
                    "model_call",
                    endpoint=f"/v1/{path}",
                    status=status,
                    result=result,
                    elapsed_ms=round((monotonic() - started) * 1000),
                )

    async def chat(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        max_tokens: int = 3072,
        temperature: float = 0.0,
        thinking: bool = False,
    ) -> str:
        data = await self._request("POST", "chat/completions", {
            "model": self.model,
            "messages": list(messages),
            "max_tokens": max_tokens,
            "temperature": temperature,
            "chat_template_kwargs": {"enable_thinking": thinking},
            "stream": False,
        })
        try:
            choice = data["choices"][0]
            text = choice["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise ModelResponseError("Zoro-1 생성 응답에 메시지 본문이 없습니다.") from None
        if choice.get("finish_reason") == "length":
            raise ModelResponseError("Zoro-1 생성 응답이 토큰 한도에서 잘렸습니다.")
        if not isinstance(text, str) or not text.strip():
            raise ModelResponseError("Zoro-1 생성 응답이 비어 있습니다.")
        return text.strip()

    async def decisions(
        self,
        state: Any,
        questions: Mapping[str, Mapping[str, Any]],
        *,
        reasoning: bool = False,
    ) -> dict[str, DecisionResult]:
        data = await self._request("POST", "systemone", {
            "model": self.model,
            "state": state,
            "questions": questions,
            "reasoning": reasoning,
            **({"max_reasoning_tokens": 768} if reasoning else {}),
        })
        raw = data.get("answers")
        if not isinstance(raw, dict) or set(raw) != set(questions):
            raise ModelResponseError("Zoro-1 판단 응답에 요청한 질문의 결과가 없습니다.")
        results: dict[str, DecisionResult] = {}
        for qid, question in questions.items():
            answer = raw[qid]
            kind = question["type"]
            if not isinstance(answer, dict) or answer.get("type") != kind:
                raise ModelResponseError("Zoro-1 판단의 질문 유형이 다릅니다.")
            criteria = question["criteria"]
            if kind == "choice":
                probabilities = _probabilities(answer, list(criteria))
                selected = answer.get("choice")
                if (
                    not isinstance(selected, str)
                    or selected not in probabilities
                    or probabilities[selected] < max(probabilities.values())
                ):
                    raise ModelResponseError("Zoro-1 판단 결과가 허용된 최상위 선택지와 다릅니다.")
                results[qid] = ChoiceResult(selected, probabilities)
            elif kind == "score":
                keys = [str(index) for index in range(len(criteria))]
                probabilities = _probabilities(answer, keys)
                score = sum(index * probabilities[key] for index, key in enumerate(keys))
                results[qid] = ScoreResult(score, probabilities)
            else:
                raise ValueError(f"지원하지 않는 판단 유형: {kind}")
        return results

    async def choice(
        self,
        state: Any,
        instructions: str,
        criteria: Mapping[str, str],
        *,
        reasoning: bool = False,
    ) -> ChoiceResult:
        result = await self.decisions(state, {
            "choice": {"type": "choice", "instructions": instructions, "criteria": dict(criteria)},
        }, reasoning=reasoning)
        return cast(ChoiceResult, result["choice"])

    async def score(
        self,
        state: Any,
        instructions: str,
        criteria: Sequence[str],
        *,
        reasoning: bool = False,
    ) -> ScoreResult:
        result = await self.decisions(state, {
            "score": {"type": "score", "instructions": instructions, "criteria": list(criteria)},
        }, reasoning=reasoning)
        return cast(ScoreResult, result["score"])

    async def probe(self) -> dict:
        models = await self._request("GET", "models")
        chat = await self.chat([
            {"role": "user", "content": "Reply with exactly the single token READY. No punctuation."},
        ], max_tokens=16)
        if chat != "READY":
            raise ModelResponseError("Zoro-1 연결 점검에서 예상한 생성 응답을 받지 못했습니다.")
        answers = await self.decisions({"status": "ready"}, {
            "choice": {
                "type": "choice", "instructions": "What status is displayed?",
                "criteria": {"ready": "ready", "offline": "offline"},
            },
            "score": {
                "type": "score", "instructions": "How completely does the status match the word 'ready'?",
                "criteria": ["mismatch", "exact match"],
            },
        })
        choice = cast(ChoiceResult, answers["choice"])
        score = cast(ScoreResult, answers["score"])
        if choice.choice != "ready":
            raise ModelResponseError("Zoro-1 연결 점검에서 예상한 판단 응답을 받지 못했습니다.")
        return {"models": models.get("data", []), "chat": chat, "choice": choice.choice, "score": score.score}


def _probabilities(answer: dict, keys: list[str]) -> dict[str, float]:
    raw = answer.get("probabilities")
    if not keys or not isinstance(raw, dict) or set(raw) != set(keys):
        raise ModelResponseError("Zoro-1 판단 확률에 허용된 선택지가 모두 포함되어야 합니다.")
    if any(type(value) not in {int, float} or not 0 <= value <= 1 for value in raw.values()):
        raise ModelResponseError("Zoro-1 판단 확률은 0에서 1 사이 숫자여야 합니다.")
    probabilities = {key: float(raw[key]) for key in keys}
    # Trida rounds the probability of each option to four decimal places.
    if not math.isclose(sum(probabilities.values()), 1.0, abs_tol=len(keys) * 0.00005 + 0.000001):
        raise ModelResponseError("Zoro-1 판단 확률의 합계가 유효하지 않습니다.")
    return probabilities
