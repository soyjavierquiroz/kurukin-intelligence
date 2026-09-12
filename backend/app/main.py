from contextlib import asynccontextmanager
import logging
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from starlette.formparsers import MultiPartParser
from starlette.concurrency import run_in_threadpool

from .audio import MIMES
from .config import get_settings
from .db import get_db, get_engine
from .models import Analysis
from .services import analysis_response, create_analysis


logging.basicConfig(level=logging.INFO)
log = logging.getLogger('kurukin.api')


@asynccontextmanager
async def lifespan(app):
    import asyncio
    from .maintenance import maintenance_loop
    task = asyncio.create_task(maintenance_loop())
    try:
        yield
    finally:
        task.cancel()
        from contextlib import suppress
        with suppress(asyncio.CancelledError):
            await task


app = FastAPI(title='Kurukin backend', version='0.4.4', lifespan=lifespan)


@app.exception_handler(HTTPException)
async def http_error(request, exc):
    content = exc.detail if isinstance(exc.detail, dict) else {'detail': exc.detail}
    return JSONResponse(status_code=exc.status_code, content=content, headers=exc.headers)


class BodyLimitMiddleware:
    """Cap raw bodies (including chunked requests) before multipart can spill to disk."""
    def __init__(self, app):
        import asyncio
        self.app = app
        self.body_slots = asyncio.Semaphore(2)

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope['method'] != 'POST':
            return await self.app(scope, receive, send)
        async with self.body_slots:
            return await self.bounded(scope, receive, send)

    async def bounded(self, scope, receive, send):
        limit = get_settings().max_audio_mb * 1024 * 1024 + 65536 if scope['path'].endswith('/audio') else 8 * 1024 * 1024
        body = bytearray()
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            body.extend(message.get('body', b''))
            if len(body) > limit:
                return await JSONResponse({'detail': 'Request too large'}, 413)(scope, receive, send)
            if not message.get('more_body', False):
                break
        async def replay():
            return {'type': 'http.request', 'body': bytes(body), 'more_body': False}
        await self.app(scope, replay, send)


app.add_middleware(BodyLimitMiddleware)


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    # Never echo the rejected payload, secrets, or exception context.
    return JSONResponse(status_code=422, content={'detail': [
        {'loc': e['loc'], 'msg': e['msg'], 'type': e['type']} for e in exc.errors()]})


@app.get('/health')
def health():
    return {'status': 'ok'}


@app.get('/ready')
def ready():
    try:
        with get_engine().connect() as connection:
            connection.execute(text('SELECT 1'))
    except Exception:
        return JSONResponse(status_code=503, content={'status': 'not_ready', 'database': 'unavailable'})
    return {'status': 'ready', 'database': 'ok'}


from sqlalchemy.exc import SQLAlchemyError
from .config import DatabaseConfigurationError

@app.exception_handler(SQLAlchemyError)
@app.exception_handler(DatabaseConfigurationError)
async def database_error(request, exc):
    return JSONResponse(status_code=503, content={'detail': 'Database unavailable'})


from .schemas import AnalysisInput, BrowserAcquisitionFailure


@app.post('/api/v1/analyses', status_code=201)
def ingest(payload: AnalysisInput, db: Session = Depends(get_db)):
    from .jobs import inbox_lock, capacity
    with inbox_lock() as root:
        capacity(db, root)
        analysis = create_analysis(db, payload)
        result = analysis_response(db, analysis)
        db.commit()
        return result


@app.get('/api/v1/analyses/{analysis_id}')
def get_analysis(analysis_id: UUID, db: Session = Depends(get_db)):
    analysis = db.scalar(select(Analysis).where(Analysis.id == analysis_id).with_for_update())
    if analysis is None:
        raise HTTPException(404, 'Unknown analysis')
    result = analysis_response(db, analysis)
    db.commit()
    return result


# Compatibility import for callers; all uploads now use the durable async path.
from .jobs import receive_audio as process_audio


@app.post('/api/v1/analyses/{analysis_id}/acquisition-batches')
def next_acquisition_batch(analysis_id: UUID, db: Session = Depends(get_db)):
    from .services import acquisition_batch
    return acquisition_batch(db, analysis_id)


@app.post('/api/v1/analyses/{analysis_id}/videos/{tiktok_id}/audio', status_code=202)
async def upload_audio(analysis_id: UUID, tiktok_id: str, request: Request,
                       db: Session = Depends(get_db)):
    limit = get_settings().max_audio_mb * 1024 * 1024
    # The raw body middleware bounds memory; keep multipart files in RAM until validated.
    MultiPartParser.spool_max_size = limit + 65537
    MultiPartParser.max_file_size = limit + 65537  # Starlette < 0.46 compatibility.
    async with request.form(max_files=1, max_fields=0) as form:
        from starlette.datastructures import UploadFile as StarletteUploadFile
        audio = form.get('audio')
        if list(form.keys()) != ['audio'] or not isinstance(audio, StarletteUploadFile):
            raise HTTPException(422, 'Exactly one multipart file named audio is required')
        if audio.content_type not in MIMES:
            raise HTTPException(415, 'Only audio/wav, audio/x-wav, audio/wave accepted')
        data = await audio.read(limit + 1)
        if len(data) > limit:
            raise HTTPException(413, 'Audio exceeds MAX_AUDIO_MB')
        try:
            return await run_in_threadpool(process_audio, analysis_id, tiktok_id, data, db)
        except OSError:
            return JSONResponse(status_code=503, content={'code': 'audio_storage_unavailable', 'retryable': True})


@app.post('/api/v1/analyses/{analysis_id}/videos/{tiktok_id}/acquisition-failure', status_code=202)
def report_acquisition_failure(analysis_id: UUID, tiktok_id: str, payload: BrowserAcquisitionFailure,
                               db: Session = Depends(get_db)):
    from .jobs import release_reserved_acquisition
    return release_reserved_acquisition(analysis_id, tiktok_id, payload.code, db)
