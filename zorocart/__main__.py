from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from statistics import mean
from urllib.parse import urlencode

import uvicorn

from .app import create_app
from .browser import CoupangApproval
from .model import ZoroClient
from .runner import CHECKPOINT, Run, SCENARIOS, execute_run


def options() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='ZoroCart 장바구니 에이전트')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('probe', help='Brev의 생성·판단 API 확인')
    serve = commands.add_parser('serve', help='로컬 상점과 한국어 대시보드 실행')
    serve.add_argument('--port', type=int, default=8765)
    serve.add_argument('--headless', action='store_true')
    serve.add_argument('--log-dir', type=Path, default=Path('logs'))
    run = commands.add_parser('run', help='브라우저 실행 및 JSON 로그 저장')
    source = run.add_mutually_exclusive_group(required=True)
    source.add_argument('--request')
    source.add_argument('--scenario', choices=list(SCENARIOS))
    run.add_argument('--fault')
    run.add_argument('--coupang-approval', type=Path)
    run.add_argument('--profile-dir', type=Path)
    run.add_argument('--headless', action='store_true')
    run.add_argument('--timeout', type=int, default=420)
    run.add_argument('--store-url', default='http://127.0.0.1:8765/store')
    run.add_argument('--output', type=Path)
    validate = commands.add_parser('validate', help='세 시나리오를 실제 Brev 모델로 연속 검증')
    validate.add_argument('--scenario', choices=list(SCENARIOS))
    validate.add_argument('--runs', type=int, default=5)
    validate.add_argument('--store-url', default='http://127.0.0.1:8765/store')
    validate.add_argument('--timeout', type=int, default=420)
    validate.add_argument('--headed', action='store_true')
    validate.add_argument('--output', type=Path, default=Path('logs/validation'))
    return parser.parse_args()


def check_result(scenario: str, artifact: dict) -> tuple[bool, bool | None]:
    result = artifact['result']
    rows = result['added']
    # Check the cart itself against independent demo acceptance criteria.
    expected = {
        'household': ({'wipes', 'bags'}, 30000),
        'pasta': ({'pasta', 'sauce'}, 20000),
        'camping': ({'chair', 'lantern'}, 60000),
    }
    requirements, budget = expected[scenario]
    checks = [event for event in artifact['events'] if event['kind'] == 'cart_verification']
    writes = [event for event in artifact['events'] if event['kind'] == 'browser_decision'
              and event.get('stage') == '검증한 판매 옵션과 수량을 장바구니에 담기']
    unchanged = all(event['preexisting_preserved'] for event in checks)
    if unchanged and writes and (not checks or writes[-1]['elapsed_ms'] > checks[-1]['elapsed_ms']):
        unchanged = None
    def item_type(row):
        text = row['title'] + ' ' + row['option']
        if '물티슈' in text and '무향' in text:
            return 'wipes'
        if '봉투' in text and '30L' in text and '손잡이' in text:
            return 'bags'
        if '파스타' in text and '토마토' in text and '소스' in text:
            return 'sauce'
        if ('파스타' in text or '스파게티' in text) and '소스' not in text:
            return 'pasta'
        if '의자' in text and '접이식' in text:
            return 'chair'
        if '랜턴' in text:
            return 'lantern'
        return None
    items = {item_type(row): row for row in rows}
    quantity_ok = True
    if scenario != 'pasta':
        quantity_ok = all(row['quantity'] == 1 and row['pack_count'] == 1
                          for row in rows if item_type(row) != 'chair')
    if 'chair' in items:
        chair = items['chair']
        quantity_ok &= chair['quantity'] * chair['pack_count'] == 2
    if scenario == 'pasta':
        chosen = {(e['evidence']['product_id'], e['evidence']['option_id']): e['evidence']
                  for e in artifact['events'] if e['kind'] == 'candidate_score'}
        for name, minimum in [('pasta', 180), ('sauce', 250)]:
            if name not in items:
                quantity_ok = False
                continue
            row = items[name]
            option = chosen.get((row['product_id'], row['option_id']), {})
            capacity, unit = (option.get('capacity') or 0) * row['quantity'], option.get('capacity_unit')
            quantity_ok &= ((unit in {'g', 'ml'} and capacity >= minimum)
                            or (unit in {'kg', 'l'} and capacity * 1000 >= minimum)
                            or (unit == 'serving' and capacity >= 2))
    return (result['status'] == 'complete' and len(rows) == 2 and set(items) == requirements
            and quantity_ok and result['merchandise_total'] <= budget and bool(checks)
            and unchanged is True), unchanged


async def command(args: argparse.Namespace) -> int:
    if args.command == 'probe':
        async with ZoroClient(os.environ.get('ZORO_URL', 'http://127.0.0.1:30001'),
                              model=os.environ.get('ZORO_MODEL', CHECKPOINT),
                              api_key=os.environ.get('ZORO_API_KEY'), timeout=90) as client:
            print(json.dumps(await client.probe(), ensure_ascii=False, indent=2))
        return 0
    if args.command == 'run':
        approval = CoupangApproval.from_file(args.coupang_approval) if args.coupang_approval else None
        if approval and (not args.profile_dir or args.fault or args.headless):
            raise ValueError('쿠팡 실행에는 전용 프로필과 보이는 브라우저가 필요합니다.')
        url = approval.home_url if approval else args.store_url
        if args.fault:
            url = f'{url}?{urlencode({"fault": args.fault})}'
        run = Run(args.request if args.request else SCENARIOS[args.scenario], 'coupang' if approval else 'local')
        result = await execute_run(run, url, headless=args.headless, timeout=args.timeout, approval=approval,
                                   profile_dir=args.profile_dir if approval else None)
        run.save(args.output or Path('logs') / f'{run.id}.json')
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result['status'] == 'complete' else 1
    if args.runs < 1:
        raise ValueError('반복 횟수는 최소 1회입니다.')
    report = {}
    for scenario in [args.scenario] if args.scenario else SCENARIOS:
        runs = []
        for number in range(1, args.runs + 1):
            run = Run(SCENARIOS[scenario])
            await execute_run(run, args.store_url, timeout=args.timeout, headless=not args.headed)
            artifact = run.artifact()
            passed, unchanged = check_result(scenario, artifact)
            run.save(args.output / f'{scenario}-{number}.json')
            runs.append({'run': number, 'passed': passed, 'preexisting_preserved': unchanged,
                         'status': run.result['status'], 'reason': run.result['reason'], **artifact['metrics']})
            print(json.dumps({'scenario': scenario, **runs[-1]}, ensure_ascii=False), flush=True)
            if passed and not (args.output / f'{scenario}.json').exists():
                run.save(args.output / f'{scenario}.json')
        report[scenario] = {'passed': sum(r['passed'] for r in runs), 'runs': args.runs,
                            'unintended_changes': sum(r['preexisting_preserved'] is False for r in runs),
                            'unverified_cart_runs': sum(r['preexisting_preserved'] is None for r in runs),
                            'mean_elapsed_ms': round(mean(r['elapsed_ms'] for r in runs)), 'details': runs}
    (args.output / 'summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return 0 if all(entry['passed'] * 5 >= entry['runs'] * 4 and not entry['unintended_changes']
                    and not entry['unverified_cart_runs']
                    for entry in report.values()) else 1


def main():
    args = options()
    if args.command == 'serve':
        uvicorn.run(create_app(store_url=f'http://127.0.0.1:{args.port}/store', log_dir=args.log_dir,
                               headless=args.headless), host='127.0.0.1', port=args.port, access_log=False)
    else:
        raise SystemExit(asyncio.run(command(args)))


if __name__ == '__main__':
    main()
