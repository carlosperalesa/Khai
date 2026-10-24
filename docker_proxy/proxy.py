import asyncio
import hmac
import os
import sqlite3
from contextlib import asynccontextmanager
from typing import Literal
from fastapi import FastAPI, Header, HTTPException, Depends
from pydantic import BaseModel, Field
try:
    from .engine import Engine
except ImportError:
    from engine import Engine


def authorize(authorization: str = Header(default='')):
    token = os.getenv('PROXY_TOKEN', '')
    if len(token) < 32 or not hmac.compare_digest(authorization, 'Bearer ' + token):
        raise HTTPException(401, 'Unauthorized')


@asynccontextmanager
async def lifespan(app):
    if len(os.getenv('PROXY_TOKEN', '')) < 32:
        raise RuntimeError('Configura PROXY_TOKEN (mínimo 32 caracteres)')
    engine = Engine(os.getenv('DATA_DIR', '/app/data'), os.environ['HOST_DATA_DIR'], os.environ['HOST_RUNTIME_DIR'])
    # A separate SQLite lock prevents two proxy schedulers sharing one data directory.
    lock = sqlite3.connect(engine.root / 'proxy-owner.sqlite3', timeout=0)
    lock.execute('BEGIN EXCLUSIVE')
    app.state.engine = engine
    stop = asyncio.Event()
    async def loop():
        while not stop.is_set():
            await asyncio.to_thread(engine.tick)
            try:
                await asyncio.wait_for(stop.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass
    worker = asyncio.create_task(loop())
    try:
        yield
    finally:
        stop.set()
        await worker
        await asyncio.to_thread(engine.close)
        lock.close()


app = FastAPI(title='Khai Execution Proxy', lifespan=lifespan, dependencies=[Depends(authorize)])


class RunRequest(BaseModel):
    execution_id: str
    task_id: str
    artifact_hash: str
    mode: Literal['SAFE', 'AGENT', 'GOD']
    dependencies: list[str] = Field(default_factory=list, max_length=30)
    request: str = Field(max_length=64000)
    network: bool = False
    grants: list[dict] = Field(default_factory=list, max_length=20)
    deadline: float
    skill_name: str | None = None
    version_id: str | None = None
    skill_artifact_hash: str | None = None


@app.post('/executions')
def submit(req: RunRequest):
    try:
        return app.state.engine.submit(req.model_dump())
    except (ValueError, PermissionError, FileNotFoundError) as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get('/executions/{execution_id}')
def status(execution_id: str):
    try:
        return app.state.engine.get(execution_id)
    except KeyError as exc:
        raise HTTPException(404, 'Ejecución desconocida') from exc


@app.post('/executions/{execution_id}/cancel')
def cancel(execution_id: str):
    import re
    if not re.fullmatch(r'run_[a-f0-9]{32}', execution_id):
        raise HTTPException(400, 'ID inválido')
    return app.state.engine.cancel(execution_id)


@app.get('/docker-ps')
def processes():
    with app.state.engine.connect() as conn:
        rows = [dict(r) for r in conn.execute('SELECT id,result FROM executions ORDER BY rowid DESC LIMIT 100')]
    return {'ps': rows}


class LogRequest(BaseModel):
    resource_id: str
    tail: int = Field(default=100, ge=1, le=200)


@app.post('/docker-logs')
def logs(req: LogRequest):
    import json
    if not req.resource_id.startswith('agent_logs_run_'):
        raise HTTPException(400, 'Usa agent_logs_run_<id>')
    eid = req.resource_id[len('agent_logs_'):]
    try:
        result = app.state.engine.get(eid)
        return {'logs': json.dumps(result, ensure_ascii=False)[-16000:]}
    except KeyError as exc:
        raise HTTPException(404, 'Ejecución desconocida') from exc
