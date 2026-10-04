import asyncio
import contextlib
import fcntl
import json
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.config import Settings
from app.domain import ApprovalInput
from app.engine import Engine
from app.store import Conflict, Store, encode
from app.observability import sanitize, trace_summary

ROOT = Path(__file__).resolve().parents[1]


class RunInput(BaseModel):
    ticket_id: str
    feedback: str = Field(default='', max_length=2000)


class TicketInput(BaseModel):
    template_id: str = 'TF-1001'
    description: str = Field(default='', max_length=2000)


def create_app(settings=None):
    settings = settings or Settings()
    store = Store(settings.data_dir / 'tickets.sqlite')
    engine = Engine(store, settings)

    @asynccontextmanager
    async def lifespan(app):
        # Exactly one worker owns recovery; prevent a second server from stealing active work.
        lock = (settings.data_dir / 'worker.lock').open('a')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            raise RuntimeError('TicketFlow仅支持单Worker，请使用单个Uvicorn进程。')
        worker = asyncio.create_task(engine.worker())
        app.state.worker = worker
        try:
            yield
        finally:
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
            lock.close()

    app = FastAPI(title='TicketFlow', version='0.1.0', lifespan=lifespan)
    app.state.store, app.state.engine = store, engine

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return JSONResponse(status_code=404, content={'detail': '记录不存在。'})

    @app.exception_handler(Conflict)
    async def conflict(request, exc):
        return JSONResponse(status_code=409, content={'detail': str(exc)})

    @app.middleware('http')
    async def origin_guard(request, call_next):
        # Loopback demo has no login. Reject cross-origin browser mutations.
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            origin = request.headers.get('origin')
            if origin and origin.rstrip('/') != str(request.base_url).rstrip('/'):
                return JSONResponse(status_code=403, content={'detail': '不允许跨站修改。'})
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        return response

    @app.get('/api/health')
    def health():
        worker = getattr(app.state, 'worker', None)
        return {'status': 'ok' if worker and not worker.done() else 'degraded',
                'mode': settings.model_mode, 'model': settings.llm_model if settings.model_mode == 'real' else 'scripted',
                'model_ready': settings.model_mode == 'scripted' or bool(settings.llm_api_key.get_secret_value()),
                'business_environment': 'local_demo'}

    @app.get('/api/tickets')
    def tickets(deleted: bool = False):
        return store.tickets(deleted=deleted)

    @app.delete('/api/tickets/{ticket_id}')
    def delete_ticket(ticket_id: str):
        store.delete_ticket(ticket_id)
        return store.ticket(ticket_id)

    @app.post('/api/tickets/{ticket_id}/restore')
    def restore_ticket(ticket_id: str):
        store.restore_ticket(ticket_id)
        return store.ticket(ticket_id)

    @app.post('/api/tickets', status_code=201)
    def new_ticket(body: TicketInput):
        from app.domain import demo_tickets
        template = next((t for t in demo_tickets() if t['id'] == body.template_id), None)
        if template is None:
            raise HTTPException(422, '请选择四个内置场景之一。')
        ticket_id = f'TF-{uuid.uuid4().hex[:8].upper()}'
        with store.connect() as db:
            db.execute('INSERT INTO tickets(id,title,description,category,priority,facts) VALUES(?,?,?,?,?,?)',
                       (ticket_id, template['title'], body.description or template['description'], template['category'], template['priority'], encode(template['facts'])))
        return store.ticket(ticket_id)

    @app.get('/api/runs')
    def runs(deleted: bool = False):
        return store.runs(deleted=deleted)

    @app.post('/api/runs', status_code=202)
    def start(body: RunInput):
        if settings.model_mode == 'real' and not settings.llm_api_key.get_secret_value():
            raise HTTPException(422, '请先在.env配置LLM_API_KEY。')
        return store.create_run(body.ticket_id, settings.model_mode, body.feedback)

    @app.get('/api/runs/{run_id}')
    def run_detail(run_id: str):
        return {**store.run(run_id), 'findings': store.findings(run_id),
                'proposals': store.proposals(run_id), 'operations': store.operations(run_id),
                'trace':trace_summary(store.events(run_id))}

    @app.get('/api/runs/{run_id}/report')
    def report(run_id: str):
        data = run_detail(run_id)
        payload = {'schema_version':1, 'exported_at':time.time(),
                   'run':data, 'ticket':store.ticket(data['ticket_id']), 'events':store.events(run_id)}
        payload = sanitize(payload, settings.llm_api_key.get_secret_value())
        # run_id is looked up first and is always an application-generated UUID.
        return JSONResponse(payload, headers={'Content-Disposition':f'attachment; filename="ticketflow-{data["id"]}.json"'})

    @app.post('/api/runs/{run_id}/decision', status_code=202)
    def decide(run_id: str, body: ApprovalInput):
        store.decide(run_id, body.approval_id, body.decision, body.feedback)
        return store.run(run_id)

    @app.post('/api/runs/{run_id}/cancel')
    def cancel(run_id: str):
        store.cancel(run_id)
        return store.run(run_id)

    @app.post('/api/runs/{run_id}/retry', status_code=202)
    def retry(run_id: str):
        store.retry(run_id)
        return store.run(run_id)

    @app.get('/api/runs/{run_id}/events')
    async def events(run_id: str, request: Request, after: int = 0):
        store.run(run_id)
        try:
            cursor = max(after, int(request.headers.get('last-event-id', '0')))
        except ValueError:
            raise HTTPException(422, '无效事件游标。')
        async def stream():
            nonlocal cursor
            heartbeat = time.monotonic()
            while not await request.is_disconnected():
                for event in store.events(run_id, cursor):
                    cursor = event['id']
                    yield f"id: {cursor}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                if store.run(run_id)['status'] in ('completed', 'completed_with_warning', 'failed', 'needs_attention', 'canceled'):
                    yield 'event: done\ndata: {}\n\n'
                    break
                if time.monotonic() - heartbeat > 10:
                    yield ': heartbeat\n\n'
                    heartbeat = time.monotonic()
                await asyncio.sleep(0.3)
        return StreamingResponse(stream(), media_type='text/event-stream',
                                 headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})

    @app.get('/')
    def index():
        return FileResponse(ROOT / 'static/index.html')

    app.mount('/static', StaticFiles(directory=ROOT / 'static'), name='static')
    return app


app = create_app()
