import asyncio

import pytest

from zorocart.browser import Offer
from zorocart.engine import ShoppingEngine
from zorocart.model import ChoiceResult, ScoreResult
from zorocart.planning import ShoppingItem, ShoppingPlan


@pytest.mark.parametrize(('requirements', 'score', 'eligible'), [('match', 3, True), ('unverified', 4, False)])
def test_scoring_uses_sales_option_and_holds_missing_required_evidence(requirements, score, eligible):
    item = ShoppingItem(id='wipes', name='물티슈', source='direct', order_quantity=2, quantity_source='explicit',
                        purchase_unit='팩', required=['무향'], excluded=['알코올'], preferred=['로켓배송'])
    offer = Offer(product_id='123', option_id='456', title='물티슈 60매', label='무향 60매', unit_price=3_000,
                  pack_count=1, capacity=60, capacity_unit='piece', stock=2, delivery='일반배송', shipping_fee=None,
                  attributes=('무향', '알코올 무첨가'), category='물티슈', url='http://example.invalid/item', selected=True)

    class Decisions:
        async def decisions(self, state, questions):
            assert state['item']['required'] == ['무향']
            assert state['item']['excluded'] == ['알코올']
            assert state['item']['preferred'] == ['로켓배송']
            assert state['option']['label'] == '무향 60매'
            assert state['option']['capacity'] == 60
            assert state['option']['pack_count'] == 1
            assert state['order_quantity'] == 2
            assert state['verified_facts']['total_won_used_for_budget'] == 6_000
            assert questions['requirements']['type'] == 'choice'
            assert questions['fit']['type'] == 'score'
            return {'requirements': ChoiceResult(requirements, {requirements: 1}),
                    'fit': ScoreResult(score, {str(score): 1})}

    events = []
    engine = ShoppingEngine(Decisions(), None, lambda kind, **fields: events.append((kind, fields)))
    engine.plan = ShoppingPlan(kind='direct', items=[item], total_budget=10_000)

    candidate = asyncio.run(engine.evaluate(item, 'wipes-1', offer))

    assert (candidate is not None) is eligible
    assert events[0][1]['requirements'] == requirements
    if eligible:
        assert (candidate.quantity, candidate.score) == (2, 3)
    else:
        assert events[0][1]['reason'] == '필수 조건을 화면에서 확인할 수 없음'
