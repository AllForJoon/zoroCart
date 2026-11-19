import asyncio
import json

import httpx
import pytest

from zorocart.model import ModelError, ModelResponseError, ZoroClient
from zorocart.planning import _guide_applies, plan_purchase


def test_direct_plan_retries_invalid_json_and_defaults_to_one_unit():
    turns = iter(("DIRECT", '{"kind":"direct","kind":"mixed"}', json.dumps({
        "kind": "direct", "total_budget": 7000,
        "candidates": [{"name": "연필", "source": "direct", "required": ["HB"]}],
    })))

    def serve(request):
        assert request.url.path == "/v1/chat/completions"
        return httpx.Response(200, json={"choices": [{"message": {"content": next(turns)}, "finish_reason": "stop"}]})

    async def run():
        async with ZoroClient("http://localhost", transport=httpx.MockTransport(serve)) as client:
            return await plan_purchase(client, "상품가 합계 7천 원 안에서 HB 연필 담아줘.")

    plan = asyncio.run(run())
    assert plan.total_budget == 7000
    assert not plan.include_shipping
    assert plan.shipping_source == "explicit"
    assert plan.items[0].order_quantity == 1
    assert plan.items[0].quantity_source == "default"


def test_context_reference_rejects_extra_product_requirements():
    request = "3인분 크림 파스타를 만들 거야. 마늘은 집에 있어. 상품가 합계 2만 원 안에서 필요한 재료를 담아줘."
    assert _guide_applies("RECIPE", request)
    assert not _guide_applies("RECIPE", request + " 소스는 저염으로 골라줘.")
    assert not _guide_applies("RECIPE", request.replace("마늘은", "글루텐 프리 면을 원하고 마늘은"))


def test_trida_score_uses_the_ordered_probability_distribution():
    def serve(request):
        assert request.url.path == "/v1/systemone"
        assert json.loads(request.content)["questions"]["score"]["criteria"] == ["bad", "okay", "good"]
        return httpx.Response(200, json={"answers": {
            "score": {"type": "score", "score": 0.0, "probabilities": {"0": 0.1, "1": 0.2, "2": 0.7}},
        }})

    async def run():
        async with ZoroClient("http://localhost/v1/", transport=httpx.MockTransport(serve)) as client:
            return await client.score({}, "How suitable?", ["bad", "okay", "good"])

    assert asyncio.run(run()).score == pytest.approx(1.6)


def test_trida_choice_rejects_nonargmax_and_checkpoint_drift():
    def serve(_request):
        return httpx.Response(200, json={"model": "other", "answers": {
            "choice": {"type": "choice", "choice": "buy", "probabilities": {"buy": 0.1, "hold": 0.9}},
        }})

    async def run(model):
        async with ZoroClient("http://localhost", model=model, transport=httpx.MockTransport(serve)) as client:
            await client.choice({}, "What to do?", {"buy": "buy it", "hold": "hold it"})

    with pytest.raises(ModelResponseError, match="최상위"):
        asyncio.run(run("zoro-1"))
    with pytest.raises(ModelError, match="체크포인트"):
        asyncio.run(run("a" * 40))


def test_guided_plan_records_quantity_and_owned_choices():
    from zorocart.runner import Run, SCENARIOS

    run = Run(SCENARIOS['pasta'])

    def serve(request):
        body = json.loads(request.content)
        if request.url.path == '/v1/chat/completions':
            prompt = body['messages'][-1]['content']
            if prompt == run.request:
                output = 'RECIPE'
            else:
                name = '스파게티면' if '마른 면' in prompt else '토마토 파스타 소스'
                output = json.dumps({'name': name, 'reason': '2인분 조리용'}, ensure_ascii=False)
            return httpx.Response(200, json={'choices': [{'message': {'content': output}, 'finish_reason': 'stop'}]})
        answers = {}
        for name, question in body['questions'].items():
            choice = 'buy' if name == 'choice' else 'exclude'
            answers[name] = {'type': 'choice', 'choice': choice,
                             'probabilities': {key: float(key == choice) for key in question['criteria']}}
        return httpx.Response(200, json={'answers': answers})

    async def plan():
        async with ZoroClient('http://localhost', transport=httpx.MockTransport(serve)) as client:
            return await plan_purchase(client, run.request, run.event)

    result = asyncio.run(plan())
    assert [item.minimum_quantity for item in result.items] == [180, 240]
    guidance = next(event for event in run.events if event['kind'] == 'plan_guidance')
    assert guidance['route'] == 'recipe'
    assert guidance['owned_items'] == ['올리브유', '소금', '마늘']
    assert [event['model_choice'] for event in run.events if event.get('decision') == 'exclude'] == ['exclude'] * 3
