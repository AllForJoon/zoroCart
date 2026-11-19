from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from secrets import compare_digest, token_urlsafe

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from .runner import Run, SCENARIOS, execute_run
from .store import router


class NewRun(BaseModel):
    request: str = Field(min_length=1, max_length=1800)


class RunReply(BaseModel):
    answer: str = Field(min_length=1, max_length=750)


def create_app(*, store_url: str, log_dir: Path, headless: bool = False) -> FastAPI:
    runs: dict[str, Run] = {}
    tasks: set[asyncio.Task] = set()
    dashboard_token = token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(_app):
        yield
        for task in list(tasks):
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(title='ZoroCart', docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.include_router(router)

    def authorized(request: Request):
        token = request.headers.get('X-ZoroCart-Token', '')
        if not compare_digest(token, dashboard_token):
            raise HTTPException(403, '이 대시보드에서 다시 요청하세요.')

    @app.get('/', response_class=HTMLResponse)
    async def dashboard():
        html = Path(__file__).with_name('dashboard.html').read_text(encoding='utf-8')
        return HTMLResponse(html.replace('__DASHBOARD_TOKEN__', dashboard_token), headers={
            'Cache-Control': 'no-store', 'X-Frame-Options': 'DENY', 'Referrer-Policy': 'no-referrer',
        })

    @app.get('/api/scenarios')
    async def scenarios(request: Request):
        authorized(request)
        return SCENARIOS

    @app.post('/api/runs', status_code=202)
    async def start_run(data: NewRun, request: Request):
        authorized(request)
        if tasks:
            raise HTTPException(409, '현재 진행 중인 실행이 끝난 뒤 다시 시도하세요.')
        if not data.request.strip():
            raise HTTPException(422, '쇼핑 요청을 입력하세요.')
        if len(runs) >= 40:
            del runs[next(iter(runs))]
        run = Run(data.request.strip())
        runs[run.id] = run

        async def work():
            try:
                await execute_run(run, store_url, headless=headless, interactive=True)
            finally:
                run.save(log_dir / f'{run.id}.json')

        task = asyncio.create_task(work())
        tasks.add(task)

        def finished(completed):
            tasks.discard(completed)
            with suppress(asyncio.CancelledError):
                error = completed.exception()
                if error:
                    run.event('log_error', error_type=type(error).__name__)

        task.add_done_callback(finished)
        return {'id': run.id}

    def lookup(run_id: str) -> Run:
        if run_id not in runs:
            raise HTTPException(404, '실행을 찾을 수 없습니다.')
        return runs[run_id]

    @app.get('/api/runs/{run_id}')
    async def read_run(run_id: str, request: Request):
        authorized(request)
        return lookup(run_id).artifact()

    @app.post('/api/runs/{run_id}/reply')
    async def answer(run_id: str, data: RunReply, request: Request):
        authorized(request)
        run = lookup(run_id)
        if not run.reply or run.reply.done():
            raise HTTPException(409, '확인을 기다리는 요청이 없습니다.')
        value = data.answer.strip()
        if not value or (run.pending['type'] == 'partial' and value not in {'yes', 'no'}):
            raise HTTPException(422, '확인 답변을 검토해 주세요.')
        run.reply.set_result(value)
        return {'ok': True}

    return app
