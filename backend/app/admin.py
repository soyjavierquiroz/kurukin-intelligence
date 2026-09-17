"""Small, server-rendered internal research backoffice.

This module intentionally owns no acquisition, worker, object-storage, or
Semantic DNA behaviour.  It reads the existing global corpus and writes only
new global ``Transcript`` records after an explicit import confirmation.
"""
from __future__ import annotations

import html
import io
import json
import re
import secrets
import threading
import time
import unicodedata
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .config import get_settings
from .db import get_db
from .channel_intelligence_contract import (
    CHANNEL_ANALYSIS_JSON_SCHEMA, CHANNEL_INTELLIGENCE_PROMPT_VERSION, CHANNEL_INTELLIGENCE_SCHEMA_VERSION,
    canonical_json_sha256, normalize_channel_analysis_evidence, validate_channel_analysis,
)
from .channel_update_contract import (CHANNEL_UPDATE_JSON_SCHEMA, CHANNEL_UPDATE_PROMPT_VERSION,
    CHANNEL_UPDATE_SCHEMA_VERSION, validate_channel_update)
from .content_pack_contract import CONTENT_PACK_SCHEMA_VERSION, validate_content_pack
from .knowledge_engine import (ANALYSIS_CONTRACT_VERSION, changed_ids, freshness, incremental_pack,
    performance_state_hash, semantic_corpus_hash, semantic_video)
from .models import (
    Channel, ChannelIntelligenceAnalysis, ChannelVideoIntelligence, Transcript,
    ChannelStrategicPlaybook, PrivateContentPack, PrivatePersonalStrategy, Video, VideoSnapshot,
)
from .strategist import (configured_personal_generator, configured_playbook_generator, get_or_create_playbook,
    personal_strategy_display, personal_strategy_input,
    validate_personal_strategy)
from .ranking import priority_view_cutoff, rank_snapshots
from .services import eligibility


security = HTTPBasic(auto_error=False)
router = APIRouter(prefix='/admin', dependencies=[])

IMPORT_MAX_BYTES = 5 * 1024 * 1024
IMPORT_MAX_LINES = 10_000
IMPORT_MAX_TRANSCRIPT_CHARS = 100_000
TRANSCRIPT_PART_CHARS = 450_000
PENDING_IMPORT_TTL_SECONDS = 15 * 60
HORMOZI_GPT_URL = 'https://chatgpt.com/g/g-68a6de0c7ec48191876f8297e467fc7c-alex-hormozi-100m'


def _unauthorized() -> HTTPException:
    return HTTPException(401, 'Admin authentication required', headers={'WWW-Authenticate': 'Basic'})


def require_admin(credentials: HTTPBasicCredentials | None = Depends(security)):
    """Protect every admin route with file-backed, constant-time Basic auth."""
    if credentials is None:
        raise _unauthorized()
    try:
        expected_username, expected_password = get_settings().resolve_admin_credentials()
    except RuntimeError:
        # Do not turn a missing deployment secret into an unprotected admin UI.
        raise HTTPException(503, 'Admin authentication unavailable') from None
    username_ok = secrets.compare_digest(credentials.username, expected_username)
    password_ok = secrets.compare_digest(credentials.password, expected_password)
    if not (username_ok and password_ok):
        raise _unauthorized()


def _e(value: Any) -> str:
    return html.escape('' if value is None else str(value), quote=True)


def _when(value: datetime | None) -> str:
    if value is None:
        return '—'
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def _number(value: Decimal | float | int | None) -> float | int | None:
    if isinstance(value, Decimal):
        return float(value)
    return value


def _build_marker() -> str:
    """Return safe, short runtime deployment provenance for internal HTML."""
    value = get_settings().build_sha or ''
    return value[:7].lower() if re.fullmatch(r'[0-9a-fA-F]{7,64}', value) else 'dev'


def _layout(title: str, content: str, *, product_journey: bool = True) -> HTMLResponse:
    journey = '''<nav class="journey" aria-label="Recorrido del producto"><span>1&nbsp; CANAL</span><span>2&nbsp; INTELIGENCIA</span><span>3&nbsp; USAR INTELIGENCIA</span></nav>''' if product_journey else ''
    return HTMLResponse(f'''<!doctype html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(title)} · Kurukin</title><style>
:root{{color-scheme:light;font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#172033;background:#f5f7fa}}
body{{margin:0}}main{{max-width:1000px;margin:auto;padding:28px 20px 48px}}header{{display:flex;gap:18px;align-items:baseline;justify-content:space-between;margin-bottom:16px}}h1{{font-size:1.55rem;margin:0}}h2{{font-size:1.1rem;margin:24px 0 10px}}h3{{margin:18px 0 8px}}a{{color:#1659b7;text-decoration:none}}a:hover{{text-decoration:underline}}.muted{{color:#64748b}}.card{{background:#fff;border:1px solid #dce3eb;border-radius:12px;padding:18px;margin:14px 0}}.hero{{border-color:#b8d0f3}}.table-wrap{{overflow-x:auto}}table{{border-collapse:collapse;width:100%;font-size:.9rem}}th,td{{text-align:left;padding:10px 8px;border-bottom:1px solid #e7edf3;vertical-align:top}}th{{white-space:nowrap;color:#526174}}.badge{{display:inline-block;padding:4px 8px;border-radius:999px;font-size:.78rem;font-weight:700;letter-spacing:.02em}}.NO_CORPUS{{background:#fee2e2;color:#991b1b}}.PARTIAL{{background:#fef3c7;color:#92400e}}.PRIORITY_READY,.ok{{background:#dcfce7;color:#166534}}.warn{{background:#fef3c7;color:#92400e}}.bad{{background:#fee2e2;color:#991b1b}}.legacy{{background:#f1f5f9;color:#475569}}button,.button{{font:inherit;background:#1659b7;color:#fff;border:0;border-radius:8px;padding:10px 14px;cursor:pointer;display:inline-block;min-height:44px;box-sizing:border-box}}button.secondary,.button.secondary{{background:#e7edf3;color:#172033}}input,select,textarea{{font:inherit;border:1px solid #b9c6d4;border-radius:6px;padding:10px;box-sizing:border-box;max-width:100%}}textarea{{width:100%;min-height:160px;white-space:pre-wrap}}form.inline{{display:flex;gap:8px;align-items:center;flex-wrap:wrap}}.actions{{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0}}.stat-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:10px}}.stat{{background:#f8fafc;border:1px solid #e7edf3;border-radius:7px;padding:10px}}.stat b{{display:block;font-size:1.2rem}}code{{font-size:.85em}}.step{{padding:0;overflow:hidden}}.step>summary{{cursor:pointer;list-style:none;padding:17px;font-size:1.05rem;min-height:24px}}.step>summary::-webkit-details-marker{{display:none}}.step-body{{padding:0 17px 17px}}.dropzone{{display:block;border:2px dashed #8ba3bd;border-radius:9px;padding:28px 16px;text-align:center;background:#f8fafc;cursor:pointer}}.dropzone input{{display:none}}.error-box{{background:#fee2e2;color:#7f1d1d;padding:12px;border-radius:7px}}.video-card{{border-left:4px solid #1659b7}}.channel-list{{display:grid;gap:10px}}.channel-list .card{{margin:0}}.journey{{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 22px}}.journey span{{padding:6px 10px;background:#e7edf3;border-radius:999px;font-size:.84rem;font-weight:600}}.eyebrow{{color:#526174;font-weight:700;font-size:.78rem;letter-spacing:.06em}}.insight-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px}}.insight-grid .card{{margin:0}}.technical{{font-size:.9rem}}@media(max-width:650px){{main{{padding:18px 12px}}header{{display:block}}header .muted{{display:block;margin-top:6px}}.actions{{display:grid}}.actions>*{{width:100%;text-align:center}}.journey{{display:grid;grid-template-columns:1fr 1fr;gap:6px}}.journey span{{text-align:center}}.stat-grid,.insight-grid{{grid-template-columns:1fr}}}}
.handoff-step{{border-left:4px solid #1659b7}}.handoff-step h2{{margin-top:4px}}.handoff-status{{min-height:1.4em}}.required-filename{{display:inline-block;background:#eff6ff;border:1px solid #b8d0f3;border-radius:6px;padding:4px 7px;font-size:1em;font-weight:700;overflow-wrap:anywhere}}.current-intelligence{{display:grid;gap:6px}}.executive-thesis{{padding:28px;border:0;border-radius:14px;background:linear-gradient(135deg,#eaf3ff,#fff)}}.formula{{font-size:1.15rem;line-height:1.7}}.decision-cta{{padding:22px;border:1px solid #b8d0f3;border-radius:12px;margin:20px 0 28px}}.mechanism-list{{display:grid;gap:0;border-top:1px solid #dce3eb}}.mechanism{{padding:20px 0;border-bottom:1px solid #dce3eb}}.mechanism-rank{{font-size:1.5rem;font-weight:800;color:#1659b7;margin-right:10px}}.proof-summary{{color:#526174;font-size:.9rem}}.evidence-detail{{margin-top:12px;background:#f8fafc;border-radius:8px;padding:10px}}.evidence-detail summary{{cursor:pointer;font-weight:700}}.compact-columns{{display:grid;grid-template-columns:1fr 1fr;gap:22px;margin:20px 0}}.compact-list{{margin:8px 0;padding-left:20px}}.compact-list li{{margin:5px 0}}.secondary-intelligence{{margin:10px 0;border:1px solid #e7edf3;border-radius:8px;padding:12px}}.strategy-result{{margin:26px 0;padding:24px;border:1px solid #b8d0f3;border-radius:14px}}.strategy-recommendation{{padding:16px 0;border-bottom:1px solid #e7edf3}}.strategy-recommendation:last-child{{border-bottom:0}}@media(max-width:650px){{.handoff-step{{padding:16px}}.dropzone{{padding:24px 12px}}.compact-columns{{grid-template-columns:1fr;gap:12px}}.executive-thesis{{padding:20px}}.decision-cta{{padding:18px}}}}
.channel-context{{display:flex;align-items:baseline;gap:6px;flex-wrap:wrap;margin:0 0 18px;padding:10px 12px;background:#eff6ff;border:1px solid #b8d0f3;border-radius:8px}}.channel-context-label{{color:#526174;font-size:.84rem;font-weight:700}}@media(max-width:650px){{.channel-context{{align-items:flex-start;display:block;line-height:1.55}}.channel-context-label{{display:block}}}}
</style></head><body><main><header><h1><a href="/admin/research">Kurukin</a></h1><span class="muted">KURUKIN PRODUCT LITE v1.1 · SCI v1 · Build {_e(_build_marker())}</span></header>{journey}{content}</main></body></html>''')


def _latest_snapshot_subquery():
    return select(
        VideoSnapshot.id, VideoSnapshot.video_id,
        func.row_number().over(
            partition_by=VideoSnapshot.video_id,
            order_by=(VideoSnapshot.created_at.desc(), VideoSnapshot.id.desc()),
        ).label('position'),
    ).subquery()


def _channels_page(db: Session, search: str, page: int, per_page: int = 50):
    query = select(Channel.id).order_by(Channel.username, Channel.id)
    count_query = select(func.count()).select_from(Channel)
    if search:
        predicate = func.lower(Channel.username).contains(search.lower())
        query = query.where(predicate)
        count_query = count_query.where(predicate)
    total = db.scalar(count_query) or 0
    ids = list(db.scalars(query.offset((page - 1) * per_page).limit(per_page)))
    return ids, total


def _corpus_data(db: Session, channel_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict[str, Any]]:
    """Fetch corpus metadata in one joined query, avoiding channel N+1s."""
    if not channel_ids:
        return {}
    latest = _latest_snapshot_subquery()
    rows = db.execute(
        select(Channel, Video, VideoSnapshot, Transcript)
        .join(Video, Video.channel_id == Channel.id, isouter=True)
        .outerjoin(latest, (latest.c.position == 1) & (latest.c.video_id == Video.id))
        .outerjoin(VideoSnapshot, VideoSnapshot.id == latest.c.id)
        .outerjoin(Transcript, Transcript.video_id == Video.id)
        .where(Channel.id.in_(channel_ids))
        .order_by(Channel.username, Video.tiktok_id)
    ).all()
    grouped: dict[uuid.UUID, dict[str, Any]] = {}
    for channel, video, snapshot, transcript in rows:
        group = grouped.setdefault(channel.id, {'channel': channel, 'records': []})
        if video is not None:
            group['records'].append((video, snapshot, transcript))
    return grouped


def _channel_summary(group: dict[str, Any]) -> dict[str, Any]:
    channel = group['channel']
    records = group['records']
    ranked_source = [(video, snapshot) for video, snapshot, _ in records if snapshot is not None]
    _median, ranked = rank_snapshots(ranked_source)
    rates = {video.id: current_rates for video, _snapshot, current_rates in ranked}
    _target, cutoff = priority_view_cutoff(ranked_source)
    threshold = Decimal(str(get_settings().enrichment_min_outlier_score))
    priority: list[tuple[Video, VideoSnapshot, Transcript | None, list[str]]] = []
    for video, snapshot, transcript in records:
        if snapshot is None or not eligibility(video.duration)[0]:
            continue
        current = rates[video.id]
        top_views = cutoff is not None and snapshot.views >= cutoff
        outlier = current['outlier_score'] >= threshold
        if top_views or outlier:
            reasons = ([] if not top_views else ['top_views']) + ([] if not outlier else ['outlier_override'])
            priority.append((video, snapshot, transcript, reasons))
    transcripts_total = sum(transcript is not None for _video, _snapshot, transcript in records)
    priority_transcripts = sum(transcript is not None for _video, _snapshot, transcript, _reasons in priority)
    missing = len(priority) - priority_transcripts
    latest_seen = max(
        (snapshot.created_at if snapshot is not None else video.last_seen_at for video, snapshot, _ in records),
        default=None,
    )
    status = ('NO_CORPUS' if transcripts_total == 0 else
              'PRIORITY_READY' if priority and missing == 0 else 'PARTIAL')
    return {
        'channel': channel, 'records': records, 'priority': priority, 'rates': rates,
        'total_videos': len(records), 'transcripts_total': transcripts_total,
        'priority_count': len(priority), 'priority_transcripts': priority_transcripts,
        'missing_priority': missing, 'latest_seen': latest_seen, 'status': status,
    }


def _summary_for_channel(db: Session, channel_id: uuid.UUID) -> dict[str, Any] | None:
    groups = _corpus_data(db, [channel_id])
    group = groups.get(channel_id)
    return _channel_summary(group) if group else None


def _human_knowledge_state(state: str) -> str:
    """Human-facing status. Raw knowledge-engine values stay in technical details."""
    return {
        'NO_INTELLIGENCE': 'Falta analizar',
        'FRESH': 'Inteligencia actualizada',
        'PERFORMANCE_CHANGED': 'Hay nuevas métricas',
        'SEMANTIC_DELTA': 'Hay contenido nuevo por analizar',
        'CONTRACT_STALE': 'La inteligencia necesita actualizarse',
    }.get(state, 'Inteligencia pendiente')


def _knowledge_action(state: str, channel_id: uuid.UUID) -> tuple[str, str]:
    """One contextual intelligence action; the route selects the existing workflow."""
    label = 'Generar inteligencia' if state == 'NO_INTELLIGENCE' else 'Actualizar inteligencia'
    return label, f'/admin/research/channels/{channel_id}/intelligence/action'


def _product_channel_status(data: dict[str, Any], analysis: ChannelIntelligenceAnalysis | None,
                            state: str | None) -> tuple[str, str, str]:
    """Small product list state: status, next action, and action route label."""
    channel_id = data['channel'].id
    if analysis is not None:
        if state == 'FRESH':
            return 'Inteligencia lista', 'Ver inteligencia', f'/admin/research/channels/{channel_id}/intelligence'
        return 'Actualización disponible', 'Actualizar', _knowledge_action(state or 'CONTRACT_STALE', channel_id)[1]
    if data['status'] == 'PRIORITY_READY':
        return 'Listo para analizar', 'Analizar', _knowledge_action('NO_INTELLIGENCE', channel_id)[1]
    return 'Necesita transcripciones', 'Ver corpus', f'/admin/research/channels/{channel_id}'


def _status_badge(status: str) -> str:
    return f'<span class="badge legacy">{_e(status)}</span>'


@router.get('/research', response_class=HTMLResponse)
def research_index(q: str = '', page: int = 1,
                   _auth: None = Depends(require_admin), db: Session = Depends(get_db)):
    page = max(1, page)
    ids, total = _channels_page(db, q.strip(), page)
    groups = _corpus_data(db, ids)
    summaries = [_channel_summary(groups[channel_id]) for channel_id in ids if channel_id in groups]
    channel_ids = [row['channel'].id for row in summaries]
    analyses = list(db.scalars(select(ChannelIntelligenceAnalysis).where(
        ChannelIntelligenceAnalysis.channel_id.in_(channel_ids)
    ).order_by(ChannelIntelligenceAnalysis.updated_at.desc(), ChannelIntelligenceAnalysis.id.desc()))) if channel_ids else []
    latest_by_channel: dict[uuid.UUID, ChannelIntelligenceAnalysis] = {}
    for analysis in analyses:
        latest_by_channel.setdefault(analysis.channel_id, analysis)
    channel_cards = []
    for row in summaries:
        analysis = latest_by_channel.get(row['channel'].id)
        state = _knowledge_state(db, row, analysis)[0]['state'] if analysis else None
        status, action, href = _product_channel_status(row, analysis, state)
        channel_cards.append(
            f'<article class="card"><h3><a href="/admin/research/channels/{row["channel"].id}">@{_e(row["channel"].username)}</a></h3>'
            f'<p class="muted">{_e(row["channel"].nickname)}</p><div class="stat-grid"><div class="stat"><b>{row["total_videos"]}</b>videos</div><div class="stat"><b>{row["priority_transcripts"]}</b>transcripciones prioritarias</div></div>'
            f'<p><span class="eyebrow">ESTADO</span><br>{_status_badge(status)}</p><p><span class="eyebrow">Siguiente paso</span></p><a class="button" href="{_e(href)}">{_e(action)}</a></article>'
        )
    channel_cards = ''.join(channel_cards) or '<p class="muted">No hay canales todavía.</p>'
    next_link = '' if page * 50 >= total else f'<a class="button secondary" href="?q={_e(q)}&page={page + 1}">Next page</a>'
    content = f'''<h2>Canales</h2><p class="muted">Elige un canal para ver qué funciona y adaptarlo a tu negocio.</p>
<form class="inline" method="get"><label>Buscar canal <input name="q" value="{_e(q)}" placeholder="@creador"></label><button>Buscar</button></form>
<div class="channel-list">{channel_cards}</div>
<p class="muted">{total} canal(es), página {page}.</p>{next_link}<p><a href="/admin/system">Admin / Debug</a></p>'''
    return _layout('Canales', content)


@router.get('/system', response_class=HTMLResponse)
def admin_system(_auth: None = Depends(require_admin)):
    """Operational entry point kept separate from the product journey."""
    content = '''<p><a href="/admin/research">← Volver al producto</a></p><h2>Admin / Debug</h2><p class="muted">Controles operativos y diagnóstico; no forman parte del recorrido de Canal → Inteligencia → Estrategia → Contenido.</p><div class="card"><h3>Operación</h3><p>Estado de workers, colas, reintentos, migraciones y diagnósticos de hashes se consultan desde las herramientas operativas autorizadas.</p><p><a class="button secondary" href="/admin/debug">Abrir diagnóstico</a></p></div><div class="card" id="auto-curator"><h3>Auto Curator</h3><p>El panel de diagnóstico de Auto Curator sigue siendo una herramienta de administración de la extensión; no se muestra en las páginas de canal.</p></div>'''
    return _layout('Admin / Debug', content, product_journey=False)


@router.get('/debug', response_class=HTMLResponse)
def admin_debug(_auth: None = Depends(require_admin)):
    content = '''<p><a href="/admin/system">← Admin / Debug</a></p><h2>Diagnóstico del sistema</h2><div class="card"><p>Este espacio reserva los diagnósticos técnicos de Auto Curator, colas, recuperación y estados internos para administradores.</p><p class="muted">No hay controles de producto aquí.</p></div>'''
    return _layout('Diagnóstico', content, product_journey=False)


def _channel_or_404(db: Session, channel_id: uuid.UUID) -> dict[str, Any]:
    summary = _summary_for_channel(db, channel_id)
    if summary is None:
        raise HTTPException(404, 'Unknown global channel')
    return summary


@router.get('/research/channels/{channel_id}', response_class=HTMLResponse)
def research_channel(channel_id: uuid.UUID, _auth: None = Depends(require_admin), db: Session = Depends(get_db)):
    return channel_intelligence_page(channel_id, db=db)


@dataclass
class PendingImport:
    channel_id: uuid.UUID
    new_rows: list[dict[str, str | None]]
    rejected: list[dict[str, Any]]
    created: float


_pending_imports: dict[str, PendingImport] = {}
_pending_lock = threading.Lock()


def _store_pending_import(value: PendingImport) -> str:
    token = secrets.token_urlsafe(32)
    with _pending_lock:
        cutoff = time.time() - PENDING_IMPORT_TTL_SECONDS
        for key in [key for key, pending in _pending_imports.items() if pending.created < cutoff]:
            del _pending_imports[key]
        _pending_imports[token] = value
    return token


def _take_pending_import(token: str, channel_id: uuid.UUID) -> PendingImport:
    with _pending_lock:
        pending = _pending_imports.pop(token, None)
    if pending is None or pending.channel_id != channel_id or pending.created < time.time() - PENDING_IMPORT_TTL_SECONDS:
        raise HTTPException(410, 'Import preview expired; upload the JSONL again')
    return pending


def _source_model(value: str | None) -> str:
    source = (value or 'manual_ai_import_v1').strip()
    # Keep the preferred human-readable provenance in the existing model field.
    return 'manual-ai-import-v1' if source == 'manual_ai_import_v1' else source


def dry_run_import(db: Session, channel_id: uuid.UUID, raw: bytes) -> tuple[dict[str, int], list[dict[str, Any]], list[dict[str, str | None]]]:
    if len(raw) > IMPORT_MAX_BYTES:
        raise HTTPException(413, 'Import file exceeds 5 MiB')
    try:
        text = raw.decode('utf-8', errors='strict')
    except UnicodeDecodeError:
        raise HTTPException(422, 'Import file must be valid UTF-8') from None
    lines = text.splitlines()
    if len(lines) > IMPORT_MAX_LINES:
        raise HTTPException(422, f'Import file exceeds {IMPORT_MAX_LINES} lines')
    parsed: list[tuple[int, dict[str, Any] | None, str | None]] = []
    ids: list[str] = []
    for line_number, line in enumerate(lines, start=1):
        try:
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError('JSON value is not an object')
            parsed.append((line_number, value, None))
            if isinstance(value.get('video_id'), str) and value['video_id']:
                ids.append(value['video_id'])
        except (json.JSONDecodeError, ValueError):
            parsed.append((line_number, None, 'Malformed JSON object'))
    existing = {video.tiktok_id: (video, transcript) for video, transcript in db.execute(
        select(Video, Transcript).outerjoin(Transcript, Transcript.video_id == Video.id)
        .where(Video.tiktok_id.in_(set(ids)))
    )} if ids else {}
    counts = {key: 0 for key in ('total', 'matched', 'new', 'already_existing', 'not_found', 'channel_mismatch', 'invalid', 'duplicates')}
    counts['total'] = len(lines)
    rejected: list[dict[str, Any]] = []
    new_rows: list[dict[str, str | None]] = []
    seen: set[str] = set()
    for line_number, value, error in parsed:
        status = ''
        video_id = value.get('video_id') if value else None
        transcript = value.get('transcript') if value else None
        if error or not isinstance(video_id, str) or not video_id:
            status = 'INVALID'
        elif video_id in seen:
            status = 'DUPLICATE_IN_FILE'
        else:
            # A syntactically present ID is still a duplicate identity even if
            # this particular row has an invalid transcript payload.
            seen.add(video_id)
            if not isinstance(transcript, str) or not transcript.strip():
                status = 'INVALID'
            elif len(transcript) > IMPORT_MAX_TRANSCRIPT_CHARS:
                status = 'INVALID'
                error = f'Transcript exceeds {IMPORT_MAX_TRANSCRIPT_CHARS} characters'
            elif value.get('language') is not None and (not isinstance(value['language'], str) or len(value['language'].strip()) > 32):
                status = 'INVALID'; error = 'Invalid language'
            elif value.get('source') is not None and (not isinstance(value['source'], str) or not value['source'].strip() or len(value['source'].strip()) > 32):
                status = 'INVALID'; error = 'Invalid source'
            else:
                found = existing.get(video_id)
                if found is None:
                    status = 'VIDEO_NOT_FOUND'
                elif found[0].channel_id != channel_id:
                    status = 'CHANNEL_MISMATCH'
                elif found[1] is not None:
                    status = 'ALREADY_EXISTS'
                    counts['matched'] += 1
                else:
                    status = 'NEW'
                    counts['matched'] += 1
                    new_rows.append({'video_id': video_id, 'transcript': transcript.strip(),
                                     'language': value.get('language', None).strip() if value.get('language') else None,
                                     'model': _source_model(value.get('source'))})
        key = {'NEW': 'new', 'ALREADY_EXISTS': 'already_existing', 'VIDEO_NOT_FOUND': 'not_found',
               'CHANNEL_MISMATCH': 'channel_mismatch', 'INVALID': 'invalid', 'DUPLICATE_IN_FILE': 'duplicates'}[status]
        counts[key] += 1
        if status != 'NEW':
            rejected.append({'line': line_number, 'video_id': video_id, 'status': status, 'detail': error or ''})
    return counts, rejected, new_rows


@router.get('/research/channels/{channel_id}/import', response_class=HTMLResponse)
def import_page(channel_id: uuid.UUID, _auth: None = Depends(require_admin), db: Session = Depends(get_db)):
    channel = _channel_or_404(db, channel_id)['channel']
    content = f'''<p><a href="/admin/research/channels/{channel.id}">← @{_e(channel.username)}</a></p><h2>Import historical transcripts</h2><p class="muted">Upload UTF-8 JSONL only. The file is parsed for a dry run first; nothing is imported until confirmation. Matching is exact TikTok <code>video_id</code> only.</p>
<div class="card"><form method="post" action="/admin/research/channels/{channel.id}/import/dry-run" enctype="multipart/form-data"><label>kurukin-import.jsonl<br><input required type="file" name="file" accept=".jsonl,application/json,text/plain"></label><div class="actions"><button>Upload and dry run</button></div></form></div><p class="muted">Limits: 5 MiB, 10,000 lines, 100,000 transcript characters per line. Existing global transcripts are never overwritten.</p>'''
    return _layout('Import transcripts', content)


@router.post('/research/channels/{channel_id}/import/dry-run', response_class=HTMLResponse)
async def import_dry_run(channel_id: uuid.UUID, file: UploadFile = File(...), _auth: None = Depends(require_admin), db: Session = Depends(get_db)):
    channel = _channel_or_404(db, channel_id)['channel']
    raw = await file.read(IMPORT_MAX_BYTES + 1)
    counts, rejected, new_rows = dry_run_import(db, channel_id, raw)
    token = _store_pending_import(PendingImport(channel_id, new_rows, rejected, time.time()))
    rejected_rows = ''.join(f'<tr><td>{row["line"]}</td><td>{_e(row["video_id"] or "—")}</td><td>{_e(row["status"])}</td><td>{_e(row["detail"])}</td></tr>' for row in rejected[:200]) or '<tr><td colspan="4" class="muted">No rejected rows.</td></tr>'
    content = f'''<p><a href="/admin/research/channels/{channel.id}/import">← Import another file</a></p><h2>Dry run: @{_e(channel.username)}</h2><div class="stat-grid"><div class="stat"><b>{counts['total']}</b>total rows</div><div class="stat"><b>{counts['matched']}</b>matched</div><div class="stat"><b>{counts['new']}</b>new transcripts</div><div class="stat"><b>{counts['already_existing']}</b>already existing</div><div class="stat"><b>{counts['not_found']}</b>not found</div><div class="stat"><b>{counts['channel_mismatch']}</b>channel mismatch</div><div class="stat"><b>{counts['invalid']}</b>invalid</div><div class="stat"><b>{counts['duplicates']}</b>duplicates</div></div>
<p class="muted">Only the {counts['new']} NEW rows below are eligible. Confirmation rechecks each video and never overwrites an existing transcript.</p><form method="post" action="/admin/research/channels/{channel.id}/import/confirm"><input type="hidden" name="token" value="{_e(token)}"><button {'disabled' if not new_rows else ''}>Import {counts['new']} transcripts</button></form><h2>Rejected / skipped rows</h2><div class="card table-wrap"><table><thead><tr><th>Line</th><th>Video ID</th><th>Status</th><th>Detail</th></tr></thead><tbody>{rejected_rows}</tbody></table></div>'''
    return _layout('Import dry run', content)


@router.post('/research/channels/{channel_id}/import/confirm', response_class=HTMLResponse)
def import_confirm(channel_id: uuid.UUID, token: str = Form(...), _auth: None = Depends(require_admin), db: Session = Depends(get_db)):
    pending = _take_pending_import(token, channel_id)
    imported = 0
    # Locking each known video closes the normal concurrent-import race; the
    # schema's unique transcripts.video_id invariant remains the final guard.
    for row in pending.new_rows:
        video = db.scalar(select(Video).where(Video.tiktok_id == row['video_id']).with_for_update())
        if video is None or video.channel_id != channel_id:
            continue
        if db.scalar(select(Transcript.id).where(Transcript.video_id == video.id)) is not None:
            continue
        db.add(Transcript(video_id=video.id, text=str(row['transcript']), language=row['language'],
                          duration=video.duration, model=str(row['model'])))
        imported += 1
    db.commit()
    data = _channel_or_404(db, channel_id)
    content = f'''<p><a href="/admin/research/channels/{channel_id}">← @{_e(data['channel'].username)}</a></p><h2>Import complete</h2><div class="card"><p><b>{imported}</b> new global transcript(s) imported.</p><p class="muted">They are immediately available to corpus views and Research Pack exports. Existing transcript records were not changed.</p></div>'''
    return _layout('Import complete', content)


def _video_export_record(video: Video, snapshot: VideoSnapshot | None, transcript: Transcript,
                         rates: dict[str, Decimal], reason: list[str]) -> dict[str, Any]:
    return {
        'video_id': video.tiktok_id, 'url': video.url, 'author': video.author, 'nickname': video.nickname,
        'published_at': _iso(video.published_at), 'duration_seconds': _number(video.duration),
        'views': snapshot.views if snapshot else None, 'likes': snapshot.likes if snapshot else None,
        'comments': snapshot.comments if snapshot else None, 'shares': snapshot.shares if snapshot else None,
        'favorites': snapshot.favorites if snapshot else None,
        'like_rate': _number(snapshot.like_rate) if snapshot else None,
        'comment_rate': _number(snapshot.comment_rate) if snapshot else None,
        'share_rate': _number(snapshot.share_rate) if snapshot else None,
        'favorite_rate': _number(snapshot.favorite_rate) if snapshot else None,
        'engagement_rate': _number(snapshot.engagement_rate) if snapshot else None,
        'outlier_score': _number(snapshot.outlier_score) if snapshot else None,
        'caption': video.caption, 'transcript': transcript.text, 'transcript_language': transcript.language,
        'transcript_source': transcript.model, 'priority_reason': ','.join(reason) if reason else None,
    }


def _selection(data: dict[str, Any], mode: str) -> list[dict[str, Any]]:
    priority = {video.id: reason for video, _snapshot, _transcript, reason in data['priority']}
    rows = [(video, snapshot, transcript, priority.get(video.id, []))
            for video, snapshot, transcript in data['records'] if transcript is not None]
    if mode == 'recommended':
        rows = [row for row in rows if row[0].id in priority]
    elif mode not in {'all', 'top25', 'top50', 'top100'}:
        raise HTTPException(422, 'Unknown export selection')
    rows.sort(key=lambda row: (
        -(data['rates'].get(row[0].id, {}).get('outlier_score', Decimal('-1'))),
        -(row[1].views if row[1] else -1), row[0].tiktok_id,
    ))
    if mode.startswith('top'):
        rows = rows[:int(mode[3:])]
    return [_video_export_record(video, snapshot, transcript, data['rates'].get(video.id, {}), reason)
            for video, snapshot, transcript, reason in rows]


def _markdown_video(record: dict[str, Any], position: int) -> str:
    def val(key: str) -> str:
        value = record.get(key)
        return '—' if value is None else str(value)
    return f'''## VIDEO {position:03d}\n\nVideo ID: {val('video_id')}\nURL: {val('url')}\nPublished: {val('published_at')}\n\nViews: {val('views')}\nLikes: {val('likes')}\nComments: {val('comments')}\nShares: {val('shares')}\nEngagement rate: {val('engagement_rate')}\nOutlier score: {val('outlier_score')}\n\n### Caption\n\n{val('caption')}\n\n### Transcript\n\n{val('transcript')}\n\n---\n\n'''


def research_pack_hash(data: dict[str, Any], mode: str) -> str:
    """Stable content identity of the exact evidence pack, not of its ZIP bytes.

    The ZIP records an export timestamp, so hashing its bytes would make a
    round-trip import impossible.  This fingerprint includes every field
    supplied to an external analyst and changes whenever its evidence changes.
    """
    channel = data['channel']
    return canonical_json_sha256({
        'schema': 'kurukin-research-pack-v1', 'platform': 'tiktok',
        'channel': {'id': str(channel.id), 'author_id': channel.tiktok_user_id,
                    'username': channel.username, 'nickname': channel.nickname},
        'selection': {'mode': mode}, 'videos': _selection(data, mode),
    })


def channel_analysis_filename(username: str, pack_hash: str) -> str:
    """Human-friendly output filename; import validity never depends on it."""
    safe_username = re.sub(r'[^a-z0-9_-]+', '-', username.lstrip('@').casefold()).strip('-_') or 'channel'
    return f'kurukin-channel-analysis-{safe_username}-{pack_hash[:8]}.json'


def _channel_context(username: str, nickname: str | None = None) -> str:
    """Persistent identity for every normal Product Lite channel subflow."""
    nickname_html = f'<span>· {_e(nickname)}</span>' if nickname else ''
    return (f'<section class="channel-context" aria-label="Canal actual">'
            f'<span class="channel-context-label">Trabajando en:</span> '
            f'<b>@{_e(username)}</b>{nickname_html}</section>')


def _research_pack_by_hash(data: dict[str, Any], value: str) -> tuple[str, list[dict[str, Any]]] | None:
    """Resolve an import hash to the currently reproducible exported pack."""
    for mode in ('recommended', 'all', 'top25', 'top50', 'top100'):
        records = _selection(data, mode)
        if records and research_pack_hash(data, mode) == value:
            return mode, records
    return None


def research_pack(data: dict[str, Any], mode: str) -> bytes:
    channel = data['channel']
    records = _selection(data, mode)
    pack_hash = research_pack_hash(data, mode)
    generated = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    selection_name = {'all': 'all transcripts', 'top25': 'top 25', 'top50': 'top 50', 'top100': 'top 100'}.get(mode, mode)
    header = f'''# TikTok Channel Research Corpus\n\n## CHANNEL\n\nUsername: @{channel.username}\nVideos discovered: {data['total_videos']}\nVideos included: {len(records)}\nGenerated: {generated}\n\n---\n\n'''
    parts: list[str] = []
    current = header
    for position, record in enumerate(records, start=1):
        block = _markdown_video(record, position)
        if current != header and len(current) + len(block) > TRANSCRIPT_PART_CHARS:
            parts.append(current); current = header + block
        else:
            current += block
    if current != header or not parts:
        parts.append(current)
    part_names = (['transcripts.md'] if len(parts) == 1 else
                  [f'transcripts_{index:03d}.md' for index in range(1, len(parts) + 1)])
    manifest = {
        'schema': 'kurukin-research-pack-v1', 'platform': 'tiktok',
        'channel': {'id': str(channel.id), 'author_id': channel.tiktok_user_id,
                    'username': channel.username, 'nickname': channel.nickname},
        'selection': {'mode': mode, 'video_count': len(records)}, 'research_pack_hash': pack_hash,
        'generated_at': generated,
        'transcript_parts': part_names,
    }
    readme = f'''# Kurukin Research Pack\n\n- Channel: @{channel.username} ({channel.nickname})\n- Platform: TikTok\n- Generated: {generated}\n- Total discovered videos: {data['total_videos']}\n- Included videos: {len(records)}\n- Selection mode: {selection_name}\n- Research Pack hash: `{pack_hash}`\n\nMetrics are observations and may change. Transcripts may contain recognition or manual errors. Creator statements are not externally verified facts. `outlier_score` is relative performance, not viral probability. The Research Pack hash is the required evidence identity for structured Channel Intelligence imports.\n'''
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('README.md', readme)
        archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
        archive.writestr('videos.jsonl', ''.join(json.dumps(record, ensure_ascii=False, separators=(',', ':')) + '\n' for record in records))
        for name, content in zip(part_names, parts):
            archive.writestr(name, content)
    return output.getvalue()


@router.get('/research/channels/{channel_id}/export', response_class=HTMLResponse)
def export_page(channel_id: uuid.UUID, _auth: None = Depends(require_admin), db: Session = Depends(get_db)):
    data = _channel_or_404(db, channel_id)
    options = ''.join(f'<option value="{mode}">{label}</option>' for mode, label in (
        ('recommended', 'Recommended (priority pool + resolved transcripts)'), ('all', 'All transcripts'),
        ('top25', 'Top 25'), ('top50', 'Top 50'), ('top100', 'Top 100'),
    ))
    content = f'''<p><a href="/admin/research/channels/{channel_id}">← @{_e(data['channel'].username)}</a></p><h2>Export for AI</h2><div class="card"><form method="get" action="/admin/research/channels/{channel_id}/export.zip"><label>Corpus <select name="mode">{options}</select></label><div class="actions"><button>Download Research Pack ZIP</button></div></form></div><p class="muted">Recommended currently includes {data['priority_transcripts']} globally resolved transcript(s) from the {data['priority_count']}-video priority pool.</p>'''
    return _layout('Export for AI', content)


@router.get('/research/channels/{channel_id}/export.zip')
def export_zip(channel_id: uuid.UUID, mode: str = 'recommended', _auth: None = Depends(require_admin), db: Session = Depends(get_db)):
    data = _channel_or_404(db, channel_id)
    body = research_pack(data, mode)
    filename = f'kurukin-research-{data["channel"].username}-{mode}.zip'
    return Response(body, media_type='application/zip', headers={'Content-Disposition': f'attachment; filename="{filename}"'})


CHANNEL_INTELLIGENCE_IMPORT_MAX_BYTES = 5 * 1024 * 1024


@dataclass
class PendingChannelIntelligenceImport:
    channel_id: uuid.UUID
    payload: dict[str, Any]
    selection_mode: str
    payload_sha256: str
    created: float


_pending_channel_intelligence_imports: dict[str, PendingChannelIntelligenceImport] = {}
_pending_channel_intelligence_lock = threading.Lock()


@dataclass
class PendingChannelIntelligenceUpdate:
    channel_id: uuid.UUID
    payload: dict[str, Any]
    created: float


_pending_channel_intelligence_updates: dict[str, PendingChannelIntelligenceUpdate] = {}
_pending_channel_intelligence_updates_lock = threading.Lock()


@dataclass
class PendingContentPackImport:
    channel_id: uuid.UUID
    analysis_id: uuid.UUID
    payload: dict[str, Any]
    private_context: dict[str, str]
    payload_sha256: str
    created: float


_pending_content_pack_imports: dict[str, PendingContentPackImport] = {}
_pending_content_pack_lock = threading.Lock()


def _store_pending_content_pack(value: PendingContentPackImport) -> str:
    token = secrets.token_urlsafe(32)
    with _pending_content_pack_lock:
        cutoff = time.time() - PENDING_IMPORT_TTL_SECONDS
        for key in [key for key, pending in _pending_content_pack_imports.items() if pending.created < cutoff]:
            del _pending_content_pack_imports[key]
        _pending_content_pack_imports[token] = value
    return token


def _take_pending_content_pack(token: str, channel_id: uuid.UUID) -> PendingContentPackImport:
    with _pending_content_pack_lock:
        pending = _pending_content_pack_imports.pop(token, None)
    if pending is None or pending.channel_id != channel_id or pending.created < time.time() - PENDING_IMPORT_TTL_SECONDS:
        raise HTTPException(410, 'Content Pack preview expired; upload the JSON again')
    return pending


def _store_pending_channel_intelligence(value: PendingChannelIntelligenceImport) -> str:
    token = secrets.token_urlsafe(32)
    with _pending_channel_intelligence_lock:
        cutoff = time.time() - PENDING_IMPORT_TTL_SECONDS
        for key in [key for key, pending in _pending_channel_intelligence_imports.items() if pending.created < cutoff]:
            del _pending_channel_intelligence_imports[key]
        _pending_channel_intelligence_imports[token] = value
    return token


def _take_pending_channel_intelligence(token: str, channel_id: uuid.UUID) -> PendingChannelIntelligenceImport:
    with _pending_channel_intelligence_lock:
        pending = _pending_channel_intelligence_imports.pop(token, None)
    if pending is None or pending.channel_id != channel_id or pending.created < time.time() - PENDING_IMPORT_TTL_SECONDS:
        raise HTTPException(410, 'Channel Intelligence preview expired; upload the JSON again')
    return pending


def _store_pending_channel_update(value: PendingChannelIntelligenceUpdate) -> str:
    token = secrets.token_urlsafe(32)
    with _pending_channel_intelligence_updates_lock:
        cutoff = time.time() - PENDING_IMPORT_TTL_SECONDS
        for key in [key for key, pending in _pending_channel_intelligence_updates.items() if pending.created < cutoff]:
            del _pending_channel_intelligence_updates[key]
        _pending_channel_intelligence_updates[token] = value
    return token


def _take_pending_channel_update(token: str, channel_id: uuid.UUID) -> PendingChannelIntelligenceUpdate:
    with _pending_channel_intelligence_updates_lock:
        pending = _pending_channel_intelligence_updates.pop(token, None)
    if pending is None or pending.channel_id != channel_id or pending.created < time.time() - PENDING_IMPORT_TTL_SECONDS:
        raise HTTPException(410, 'Channel Intelligence update preview expired; upload the JSON again')
    return pending


def channel_intelligence_prompt(data: dict[str, Any], mode: str = 'all') -> str:
    """Contractual prompt used beside the downloaded immutable evidence pack."""
    records = _selection(data, mode)
    if not records:
        raise HTTPException(422, 'The selected Research Pack has no resolved transcript videos')
    pack_hash = research_pack_hash(data, mode)
    expected_filename = channel_analysis_filename(data['channel'].username, pack_hash)
    schema = json.dumps(CHANNEL_ANALYSIS_JSON_SCHEMA, ensure_ascii=False, indent=2)
    return f'''# Kurukin Structured Channel Intelligence v1

YOUR TASK IS NOT TO WRITE A REPORT IN CHAT.

YOUR TASK IS TO CREATE A FILE.

Required output filename: `{expected_filename}`

The file must be valid JSON matching EXACTLY the JSON Schema contained below. Do NOT write Markdown. Do NOT paste the complete JSON as the normal chat response. Do NOT rename schema fields.

The root structure MUST use exactly: `schema`, `prompt_version`, `processor`, `research_pack`, `videos`, `channel_intelligence`.

Explicitly prohibited aliases: `video_intelligence`, `video_analysis`, `items`, `results`.

Set `schema` to `{CHANNEL_INTELLIGENCE_SCHEMA_VERSION}`, `prompt_version` to `{CHANNEL_INTELLIGENCE_PROMPT_VERSION}`, and `research_pack.hash` exactly to `{pack_hash}`. Set `research_pack.channel_username` and `research_pack.selection_mode` from this pack, and `research_pack.video_count` to {len(records)}.

If the Research Pack contains N videos, `videos[]` MUST contain exactly N video objects. Every exported `video_id` appears exactly once: no missing videos, unknown videos, or duplicates. Every `evidence.video_ids` reference must be an exact `video_id` from this Research Pack. Within every `evidence.video_ids` array, each `video_id` must appear at most once. Never repeat the same `video_id` inside a single evidence object.

For a low-information video use `analysis_status = insufficient_content`; do not fabricate semantic conclusions. Canonical metrics belong to Kurukin. Per-video intelligence MUST NOT return or restate `views`, `likes`, `comments`, `shares`, `favorites`, `engagement_rate`, or `outlier_score`. Do NOT create `performance_interpretation` or any field that duplicates metrics.

Every meaningful channel-level insight must reference evidence video IDs. Creator claims are not externally verified facts. Transcripts may contain ASR/manual errors.

For every channel-level pattern:
- `description` explains WHAT the pattern or mechanism is and HOW it appears.
- `why_it_matters` explains the DISTINCT strategic implication: why a marketer or creator should care, which behavior or content principle it suggests, or what makes the mechanism useful.
- DO NOT repeat or paraphrase `description` inside `why_it_matters`.

Example:
- `description`: "Convierte una idea espiritual abstracta en una acción concreta."
- `why_it_matters`: "Reducir la distancia entre concepto y acción hace que el contenido sea más fácil de aplicar, recordar y compartir."

Short canonical root example (structural illustration only; the supplied JSON Schema is authoritative):
{{
  "schema": "kurukin-channel-analysis-v1",
  "prompt_version": "kurukin-channel-analysis-prompt-v1",
  "processor": "...",
  "research_pack": {{"hash": "...", "channel_username": "...", "selection_mode": "...", "video_count": {len(records)}}},
  "videos": [{{"video_id": "...", "analysis_status": "analyzed", "evidence": []}}],
  "channel_intelligence": {{...}}
}}

FINAL OUTPUT REQUIREMENT:

Create and attach/downloadable file: `{expected_filename}`

Do not paste the complete JSON into the conversation.

If the AI environment truly cannot create a downloadable file, respond exactly:
FILE_GENERATION_UNAVAILABLE

JSON Schema (authoritative):

{schema}
'''


def channel_intelligence_update_prompt(data: dict[str, Any], prior: ChannelIntelligenceAnalysis,
                                       state: dict[str, Any], delta: list[str]) -> str:
    """Contractual prompt for the existing immutable incremental-update import."""
    schema = json.dumps(CHANNEL_UPDATE_JSON_SCHEMA, ensure_ascii=False, indent=2)
    expected_filename = channel_analysis_filename(data['channel'].username,
                                                  research_pack_hash(data, prior.selection_mode))
    return f'''# Kurukin Structured Channel Intelligence Update v1

YOUR TASK IS NOT TO WRITE A REPORT IN CHAT.

YOUR TASK IS TO CREATE A FILE.

Required output filename: `{expected_filename}`

Analyze only the new or changed videos in the attached incremental Research Pack. Reuse the supplied
previous structured intelligence and compact known-video intelligence only as context for the refreshed
channel synthesis. Do not invent evidence, transcripts, metrics, or video IDs.

The file must be valid JSON matching EXACTLY the JSON Schema below. Do NOT write Markdown and do NOT
paste the complete JSON as the normal chat response. Set `schema` to `{CHANNEL_UPDATE_SCHEMA_VERSION}` and
`prompt_version` to `{CHANNEL_UPDATE_PROMPT_VERSION}`. Set `processor` to the processor used.

Set `base_state.analysis_id` to `{prior.id}`, `base_state.payload_sha256` to `{prior.payload_sha256}`, and
`base_state.semantic_corpus_hash` to `{prior.semantic_corpus_hash}`. Set
`target_state.semantic_corpus_hash` to `{state['semantic_corpus_hash']}`.

`upsert_videos` MUST contain exactly these {len(delta)} new or changed video IDs, once each:
{json.dumps(delta, ensure_ascii=False)}

Every evidence video ID must be in the merged corpus provided by the pack. Within every `evidence.video_ids`
array, each `video_id` must appear at most once. Never repeat the same `video_id` inside a single evidence
object. For low-information videos use `analysis_status = insufficient_content`; do not fabricate conclusions.
Canonical metrics belong to Kurukin.

For every channel-level pattern, use `description` for WHAT the mechanism is and HOW it appears. Use
`why_it_matters` only for its DISTINCT strategic implication. DO NOT repeat or paraphrase `description`
inside `why_it_matters`.

FINAL OUTPUT REQUIREMENT:

Create and attach/downloadable file: `{expected_filename}`

If the AI environment truly cannot create a downloadable file, respond exactly:
FILE_GENERATION_UNAVAILABLE

JSON Schema (authoritative):

{schema}
'''


def _external_ai_handoff_page(*, channel_id: uuid.UUID, username: str, nickname: str | None,
                              expected_filename: str, title: str, prompt: str, download_href: str, import_base: str,
                              has_current_intelligence: bool = False) -> HTMLResponse:
    """Render the explicit evidence → external AI → import handoff for new and update analyses."""
    current_status = ('''<div class="current-intelligence"><p><b>Inteligencia actual:</b> <span class="badge ok">✓ disponible</span></p>
<p><b>Nuevo análisis:</b> <span id="new-analysis-status" class="badge warn">pendiente de importar</span></p></div>'''
                      if has_current_intelligence else
                      '''<p><b>Nuevo análisis:</b> <span id="new-analysis-status" class="badge warn">pendiente</span></p>''')
    storage_key = f'kurukin-research-pack-downloaded-{channel_id}'
    content = f'''<p><a href="/admin/research/channels/{channel_id}/intelligence">← Inteligencia</a></p>{_channel_context(username, nickname)}<h2>{_e(title)}</h2>
<p class="muted">Completa estos tres pasos en orden. No necesitas volver atrás para importar el resultado.</p>{current_status}
<section class="card handoff-step" aria-labelledby="step-1-title"><p class="eyebrow">Paso 1</p><h2 id="step-1-title">Preparar evidencia</h2>
<p>Este ZIP contiene los videos, transcripciones, evidencia y contrato que la IA necesita para analizar el canal.</p>
<div class="actions"><a class="button" id="download-research-pack" href="{_e(download_href)}">Descargar paquete de investigación</a></div>
<p class="muted handoff-status" id="research-pack-status" aria-live="polite"></p></section>
<section class="card handoff-step" aria-labelledby="step-2-title"><p class="eyebrow">Paso 2</p><h2 id="step-2-title">Analizar con IA</h2>
<p><b>Procesador recomendado:</b> Alex Hormozi — $100M</p>
<div class="actions"><button type="button" id="open-hormozi" data-hormozi-url="{HORMOZI_GPT_URL}">Copiar instrucciones y abrir Alex Hormozi GPT</button></div>
<p class="handoff-status" id="copy-status" aria-live="polite"></p>
<ol><li>Adjunta el paquete de investigación descargado.</li><li>Pega las instrucciones copiadas.</li><li>La IA debe devolverte:<br><span class="required-filename">{_e(expected_filename)}</span></li><li>Descarga ese archivo.</li><li>Regresa a Kurukin.</li></ol>
<div class="actions"><button type="button" id="copy-channel-prompt" class="secondary">Copiar instrucciones</button></div>
<details class="card technical" id="channel-prompt-details"><summary><b>Ver instrucciones</b></summary><div class="step-body"><p class="muted">Úsalo para revisar o copiar manualmente el contrato.</p><textarea id="channel-prompt" readonly>{_e(prompt)}</textarea><div class="actions"><button type="button" id="copy-channel-prompt-manual" class="secondary">Copiar instrucciones</button></div></div></details></section>
<section class="card handoff-step" aria-labelledby="step-3-title"><p class="eyebrow">Paso 3</p><h2 id="step-3-title">Importar resultado</h2>
<p>Cuando ChatGPT te entregue <span class="required-filename">{_e(expected_filename)}</span>, súbelo aquí.</p>
<form id="analysis-upload"><label class="dropzone">Sube <b>{_e(expected_filename)}</b><br><span class="button secondary">Seleccionar { _e(expected_filename) }</span><input id="analysis-file" type="file" accept=".json,application/json"></label></form><div id="analysis-result" aria-live="polite"></div></section>
<script>(function(){{
const prompt=document.getElementById('channel-prompt'),copyStatus=document.getElementById('copy-status'),newAnalysis=document.getElementById('new-analysis-status'),download=document.getElementById('download-research-pack'),downloadStatus=document.getElementById('research-pack-status'),storageKey='{storage_key}',gptUrl='{HORMOZI_GPT_URL}',input=document.getElementById('analysis-file'),base='{import_base}';
function markDownloaded(){{try{{localStorage.setItem(storageKey,'1')}}catch(_error){{}}download.textContent='✓ Paquete de investigación descargado';downloadStatus.textContent='Paso 1 completado: paquete descargado.';if(newAnalysis)newAnalysis.textContent='listo para analizar';}}
try{{if(localStorage.getItem(storageKey)==='1')markDownloaded()}}catch(_error){{}}
download.addEventListener('click',markDownloaded);
async function copyCurrentPrompt(){{try{{if(!navigator.clipboard||!navigator.clipboard.writeText)throw new Error('Clipboard unavailable');await navigator.clipboard.writeText(prompt.value);return true;}}catch(_error){{return false;}}}}
async function manualCopy(){{const copied=await copyCurrentPrompt();copyStatus.textContent=copied?'Instrucciones copiadas.':'No se pudieron copiar automáticamente. Abre “Ver instrucciones” y copia el texto manualmente.';}}
document.getElementById('copy-channel-prompt').addEventListener('click',manualCopy);document.getElementById('copy-channel-prompt-manual').addEventListener('click',manualCopy);
document.getElementById('open-hormozi').addEventListener('click',()=>{{window.open(gptUrl,'_blank','noopener');copyCurrentPrompt().then(copied=>{{copyStatus.textContent=copied?'Instrucciones copiadas. Alex Hormozi GPT se abrió en una nueva pestaña.':'Alex Hormozi GPT se abrió en una nueva pestaña, pero no se pudieron copiar las instrucciones. Abre “Ver instrucciones” y cópialas manualmente.';if(newAnalysis)newAnalysis.textContent='en progreso en ChatGPT';}});}});
input.addEventListener('change',async()=>{{if(!input.files[0])return;if(newAnalysis)newAnalysis.textContent='listo para importar';const body=new FormData();body.append('file',input.files[0]);const r=await fetch(base+'/dry-run',{{method:'POST',body}}),x=await r.json(),box=document.getElementById('analysis-result');if(!x.ok){{box.innerHTML='<div class="error-box"><b>No se puede importar.</b><ul>'+((x.errors||['No se pudo validar el archivo.']).map(v=>'<li>'+v+'</li>').join(''))+'</ul></div>';return}}const warningHtml=(x.warnings||[]).length?'<div class="card warn"><b>Advertencia</b><ul>'+x.warnings.map(v=>'<li>'+v+'</li>').join('')+'</ul></div>':'';box.innerHTML=warningHtml+'<div class="card"><h3>Listo para confirmar</h3><button id="confirm-analysis">Confirmar inteligencia</button></div>';document.getElementById('confirm-analysis').addEventListener('click',async()=>{{const confirm=new FormData();confirm.append('token',x.token);const done=await fetch(base+'/confirm',{{method:'POST',body:confirm}});if((await done.json()).ok)location.href='/admin/research/channels/{channel_id}/intelligence';}});}});
}})();</script>'''
    return _layout(title, content)


def dry_run_channel_intelligence_import(db: Session, channel_id: uuid.UUID, raw: bytes) -> tuple[dict[str, Any] | None, list[str], list[str], str | None, str | None]:
    """Parse and validate an import without writing any intelligence records."""
    if len(raw) > CHANNEL_INTELLIGENCE_IMPORT_MAX_BYTES:
        raise HTTPException(413, 'Channel Intelligence file exceeds 5 MiB')
    try:
        value = json.loads(raw.decode('utf-8', errors='strict'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, ['Upload one valid UTF-8 JSON object.'], [], None, None
    if not isinstance(value, dict):
        return None, ['The upload must be one JSON object.'], [], None, None
    data = _channel_or_404(db, channel_id)
    research_pack = value.get('research_pack')
    pack_hash = research_pack.get('hash') if isinstance(research_pack, dict) else None
    resolved = _research_pack_by_hash(data, pack_hash) if isinstance(pack_hash, str) else None
    if resolved is None:
        return value, ['research_pack_hash does not match a current non-empty Research Pack for this channel. Re-export, re-analyze, and upload again.'], [], None, None
    mode, records = resolved
    if isinstance(research_pack, dict):
        if research_pack.get('channel_username') != data['channel'].username:
            return value, [f"research_pack.channel_username must equal @{data['channel'].username}."], [], None, None
        if research_pack.get('selection_mode') != mode:
            return value, ['research_pack.selection_mode does not match the Research Pack hash.'], [], None, None
    removed_duplicate_references = normalize_channel_analysis_evidence(value)
    warnings = (['Se eliminó 1 referencia de evidencia duplicada.'] if removed_duplicate_references == 1 else
                [f'Se eliminaron {removed_duplicate_references} referencias de evidencia duplicadas.']
                if removed_duplicate_references else [])
    errors = validate_channel_analysis(value, {record['video_id'] for record in records})
    payload_sha256 = canonical_json_sha256(value)
    existing = db.scalar(select(ChannelIntelligenceAnalysis).where(
        ChannelIntelligenceAnalysis.channel_id == channel_id,
        ChannelIntelligenceAnalysis.research_pack_hash == pack_hash,
        ChannelIntelligenceAnalysis.schema_version == CHANNEL_INTELLIGENCE_SCHEMA_VERSION,
    ))
    if existing is not None and existing.payload_sha256 != payload_sha256:
        errors.append('This Research Pack already has a historical analysis. Generate a new Research Pack before importing another analysis; existing history is never overwritten.')
    status = ('ALREADY_IMPORTED' if existing is not None and existing.payload_sha256 == payload_sha256 else
              'PACK_ALREADY_IMPORTED' if existing is not None else 'NEW')
    return value, errors, warnings, mode, status


def _human_video_title(record: dict[str, Any]) -> str:
    """Never make the opaque TikTok ID the primary evidence label."""
    for candidate in (record.get('caption'), record.get('transcript')):
        if isinstance(candidate, str):
            meaningful = candidate.strip().split('.')[0].strip()
            if meaningful:
                return meaningful[:140]
    return f"Publicado {record.get('published_at') or 'sin fecha'}"


def _video_label(record: dict[str, Any]) -> str:
    return _human_video_title(record)


def _evidence_html(entries: list[dict[str, Any]], records_by_tiktok_id: dict[str, dict[str, Any]]) -> str:
    if not entries:
        return '<span class="muted">No cited video evidence.</span>'
    rows = []
    for evidence in entries:
        links = []
        for video_id in evidence.get('video_ids', []):
            record = records_by_tiktok_id.get(video_id)
            if record is None:
                links.append(_e(video_id))
            else:
                links.append(f'<a href="{_e(record["url"])}" target="_blank" rel="noopener">{_e(_video_label(record))}</a>')
        rows.append(f'<li>{_e(evidence.get("claim", ""))}<br><span class="muted">Evidence: {"; ".join(links)}</span></li>')
    return '<ul>' + ''.join(rows) + '</ul>'


def _channel_analysis_items(value: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Flatten named channel-level evidence sections for safe human rendering."""
    result: list[tuple[str, dict[str, Any]]] = []
    labels = {'pains': 'Dolores', 'desires': 'Deseos', 'hooks': 'Hooks', 'topics': 'Temas',
              'winning_patterns': 'Patrones ganadores', 'narratives': 'Narrativas',
              'repetition_clusters': 'Repetición', 'ctas': 'CTAs', 'offers': 'Ofertas', 'opportunities': 'Oportunidades'}
    for section, label in labels.items():
        for item in value.get(section, []):
            if isinstance(item, dict):
                result.append((label, item))
    return result


_PLAYBOOK_SECTIONS = (
    ('winning_patterns', 'LO QUE MÁS REPITEN'), ('hooks', 'HOOKS QUE FUNCIONAN'),
    ('pains', 'DOLORES QUE ACTIVAN'), ('desires', 'DESEOS QUE ACTIVAN'),
    ('narratives', 'ESTRUCTURAS NARRATIVAS'), ('ctas', 'QUÉ HACEN CON LA OFERTA'),
    ('repetition_clusters', 'ESTRATEGIA DE REPETICIÓN'),
)


def _known_patterns(analysis: ChannelIntelligenceAnalysis) -> dict[str, dict[str, Any]]:
    """The imported named patterns are the sole source for the Create contract."""
    patterns: dict[str, dict[str, Any]] = {}
    for _section, item in _channel_analysis_items(analysis.channel_intelligence):
        name = item.get('name')
        if isinstance(name, str) and name.strip():
            patterns.setdefault(name, item)
    return patterns


def _evidence_badges(entries: list[dict[str, Any]], records: dict[str, dict[str, Any]]) -> str:
    badges: list[str] = []
    for entry in entries:
        for video_id in entry.get('video_ids', []):
            record = records.get(video_id)
            if record is None:
                continue
            views = record.get('views')
            outlier = record.get('outlier_score')
            value = f'{views:,} vistas' if isinstance(views, int) else 'Evidencia'
            if outlier is not None:
                value += f' · rendimiento relativo {float(outlier):.1f}'
            badges.append(f'<span class="badge legacy">{_e(value)}</span>')
    return ' '.join(badges[:4]) or '<span class="muted">Evidence cited in imported intelligence.</span>'


def _format_metric(value: Any, suffix: str = '') -> str:
    if isinstance(value, (int, float)):
        return f'{value:,.0f}{suffix}'
    return '—'


def _evidence_detail(entries: list[dict[str, Any]], records: dict[str, dict[str, Any]], *, adaptation: str = '') -> str:
    """Evidence is deliberately collapsed: it supports the conclusion, not the hierarchy."""
    rows = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        for video_id in entry.get('video_ids', []) if isinstance(entry.get('video_ids'), list) else []:
            record = records.get(video_id)
            if record is None:
                continue
            metrics = f"{_format_metric(record.get('views'), ' vistas')} · outlier {_number(record.get('outlier_score')) if record.get('outlier_score') is not None else '—'} · engagement {_number(record.get('engagement_rate')) if record.get('engagement_rate') is not None else '—'} · shares {_format_metric(record.get('shares'))}"
            rows.append(f'<li><b>{_e(_video_label(record))}</b><br><span class="muted">{_e(metrics)}</span><br>{_e(entry.get("claim") or "Patrón observado en este video.")} · <a href="{_e(record["url"])}" target="_blank" rel="noopener">Ver en TikTok</a></li>')
    if not rows:
        return '<span class="muted">Sin evidencia de video disponible.</span>'
    adaptation_html = f'<p><b>Adaptación recomendada:</b> {_e(adaptation)}</p>' if adaptation else ''
    return f'<details class="evidence-detail"><summary>Ver evidencia</summary><ol>{"".join(rows)}</ol>{adaptation_html}</details>'


def _proof_summary(item: dict[str, Any], records: dict[str, dict[str, Any]]) -> str:
    ids = [video_id for entry in item.get('evidence', []) if isinstance(entry, dict)
           for video_id in entry.get('video_ids', []) if isinstance(video_id, str)]
    evidence = [records[video_id] for video_id in dict.fromkeys(ids) if video_id in records]
    if not evidence:
        return 'Evidencia citada en la inteligencia.'
    strongest = max((record.get('views') or 0 for record in evidence), default=0)
    video_label = 'video' if len(evidence) == 1 else 'videos'
    return f'{len(evidence)} {video_label} con evidencia · mejor ejemplo {_format_metric(strongest, " vistas")}'


def _normalized_copy(value: Any) -> str:
    """Compare stored copy conservatively across harmless formatting changes."""
    if not isinstance(value, str):
        return ''
    value = unicodedata.normalize('NFKD', value).casefold()
    value = ''.join(char for char in value if not unicodedata.combining(char))
    return re.sub(r'[^\w]+', ' ', value, flags=re.UNICODE).strip()


def _has_distinct_why(description: Any, why_it_matters: Any) -> bool:
    """Do not render an old duplicated explanation as a second paragraph."""
    normalized_description = _normalized_copy(description)
    normalized_why = _normalized_copy(why_it_matters)
    return bool(normalized_why) and normalized_why != normalized_description


def _compact_items(items: Any, *, limit: int = 5, description: bool = False) -> str:
    rows = [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []
    if not rows:
        return '<p class="muted">Aún no hay suficiente evidencia importada.</p>'
    initial = ''.join(f'<li><b>{_e(item.get("name") or "Patrón")}</b>{(" — " + _e(item.get("description") or "")) if description else ""}</li>' for item in rows[:limit])
    extra = ''.join(f'<li><b>{_e(item.get("name") or "Patrón")}</b>{(" — " + _e(item.get("description") or "")) if description else ""}</li>' for item in rows[limit:])
    more = f'<details><summary>Ver todos</summary><ul class="compact-list">{extra}</ul></details>' if extra else ''
    return f'<ul class="compact-list">{initial}</ul>{more}'


def _actionable_playbook(data: dict[str, Any], analysis: ChannelIntelligenceAnalysis) -> str:
    """Executive-first Channel Intelligence; full source analysis remains available below."""
    records = {record['video_id']: record for record in _selection(data, analysis.selection_mode)}
    value = analysis.channel_intelligence
    winning = [item for item in value.get('winning_patterns', []) if isinstance(item, dict)]
    mechanisms = winning[:5]
    if not mechanisms:
        mechanisms = [item for item in value.get('hooks', []) if isinstance(item, dict)][:5]
    formula = ' → '.join(str(item.get('name')) for item in mechanisms[:4] if item.get('name')) or 'Problema específico → explicación → significado → transformación → CTA'
    mechanism_html = ''.join(
        f'<article class="mechanism"><span class="mechanism-rank">{index}</span><b>{_e(item.get("name") or "Mecanismo")}</b>'
        f'<p>{_e(item.get("description") or "Mecanismo repetido respaldado por evidencia del canal.")}</p>'
        f'{("<p><b>Por qué importa:</b> " + _e(item.get("why_it_matters")) + "</p>") if _has_distinct_why(item.get("description"), item.get("why_it_matters")) else ""}'
        f'<p class="proof-summary">{_e(_proof_summary(item, records))}</p>{_evidence_detail(item.get("evidence", []), records)}</article>'
        for index, item in enumerate(mechanisms, 1)
    ) or '<p class="muted">Aún no hay mecanismos importados.</p>'
    secondary = ''.join(
        f'<details class="secondary-intelligence"><summary><b>{label}</b></summary>{_compact_items(value.get(key), limit=5, description=True)}</details>'
        for key, label in (('narratives', 'Narrativas'), ('ctas', 'Patrones de CTA'), ('offers', 'Patrones de oferta'),
                           ('repetition_clusters', 'Estrategia de repetición'), ('topics', 'Temas y persuasión'))
    )
    return f'''<section id="what-works"><h2>¿POR QUÉ FUNCIONA ESTE CANAL?</h2><div class="executive-thesis"><p>{_e(value.get('summary') or 'La inteligencia disponible identifica mecanismos repetidos con evidencia.')}</p><p class="eyebrow">FÓRMULA DOMINANTE</p><p class="formula"><b>{_e(formula)}</b></p></div></section>
<section id="what-next" class="decision-cta"><h2>USAR ESTA INTELIGENCIA</h2><p>Usa estos mecanismos en tu contexto, sin copiar la identidad, las afirmaciones ni las expresiones del creador.</p><div class="actions"><a class="button" href="#usar-inteligencia">Preparar contexto creativo</a></div></section>
<section id="top-mechanisms"><h2>MECANISMOS PRINCIPALES</h2><p class="muted">Los 3–5 mecanismos más importantes. Las métricas están dentro de la evidencia.</p><div class="mechanism-list">{mechanism_html}</div></section>
<section class="compact-columns"><div><h2>DOLORES QUE ACTIVAN</h2>{_compact_items(value.get('pains'))}</div><div><h2>DESEOS QUE ACTIVAN</h2>{_compact_items(value.get('desires'))}</div></section>
<section><h2>HOOKS QUE FUNCIONAN</h2>{_compact_items(value.get('hooks'), limit=3, description=True)}</section>
<section><h2>INTELIGENCIA SECUNDARIA</h2>{secondary}</section>'''


def _strategic_playbook_html(playbook: ChannelStrategicPlaybook, records: dict[str, dict[str, Any]]) -> str:
    """Human-first renderer; raw SCI remains below it in collapsed details."""
    value = playbook.payload_json
    def items(name: str) -> str:
        rows = value.get(name, [])
        if not isinstance(rows, list): return '<p class="muted">No disponible.</p>'
        output = []
        for row in rows[:7]:
            if isinstance(row, str):
                output.append(f'<div class="card"><p>{_e(row)}</p></div>'); continue
            if not isinstance(row, dict): continue
            ids = row.get('evidence_video_ids', [])
            evidence = _evidence_badges([{'video_ids': ids}], records) if isinstance(ids, list) else ''
            output.append(f'<div class="card"><h3>{_e(row.get("title") or row.get("name") or "Movimiento")}</h3><p>{_e(row.get("what_it_is") or row.get("description") or "")}</p><p><b>Por qué</b><br>{_e(row.get("why_it_works") or row.get("why") or "")}</p><p><b>Cómo aplicarlo</b><br>{_e(row.get("how_to_apply") or row.get("how") or "")}</p><p>{evidence}</p></div>')
        return ''.join(output) or '<p class="muted">No disponible.</p>'
    return f'''<section id="strategic-playbook"><h2>EL PLAYBOOK DEL CANAL</h2><div class="card"><h3>Tesis central</h3><p>{_e(value.get('executive_thesis') or '—')}</p><h3>Fórmula dominante</h3><p>{_e(value.get('dominant_formula') or '—')}</p></div><h2>Lo que este canal hace excepcionalmente bien</h2>{items('top_moves')}<h2>Qué repetir</h2>{items('what_to_repeat')}<h2>Qué no copiar</h2>{items('what_to_avoid')}<h2>Experimentos recomendados</h2>{items('recommended_experiments')}</section>'''


def _latest_playbook(db: Session, analysis: ChannelIntelligenceAnalysis) -> ChannelStrategicPlaybook | None:
    return db.scalar(select(ChannelStrategicPlaybook).where(ChannelStrategicPlaybook.source_analysis_id == analysis.id,
        ChannelStrategicPlaybook.performance_state_hash == analysis.performance_state_hash).order_by(ChannelStrategicPlaybook.created_at.desc()))


@router.post('/research/channels/{channel_id}/intelligence/playbook/generate')
def generate_channel_playbook(channel_id: uuid.UUID, _auth: None = Depends(require_admin), db: Session = Depends(get_db)):
    data = _channel_or_404(db, channel_id)
    analysis = db.scalar(select(ChannelIntelligenceAnalysis).where(ChannelIntelligenceAnalysis.channel_id == channel_id).order_by(
        ChannelIntelligenceAnalysis.created_at.desc(), ChannelIntelligenceAnalysis.id.desc()))
    if analysis is None: raise HTTPException(409, 'Import Channel Intelligence first')
    state, records, _delta = _knowledge_state(db, data, analysis)
    if state['state'] == 'SEMANTIC_DELTA': raise HTTPException(409, 'Import the semantic delta before generating a Playbook')
    if state['state'] == 'PERFORMANCE_CHANGED':
        analysis.performance_state_hash = state['performance_state_hash']; db.commit()
    children = [child.intelligence for child, _video in _analysis_video_rows(db, analysis)]
    try:
        provider, model, generate = configured_playbook_generator(get_settings())
        playbook, reused = get_or_create_playbook(db, channel_id=channel_id, analysis=analysis, records=records,
            videos=children, provider=provider, model=model, generate=generate)
    except Exception:
        return JSONResponse({'ok': False, 'error': 'No pudimos generar el Playbook en este momento.'}, status_code=503)
    return JSONResponse({'ok': True, 'playbook_id': str(playbook.id), 'reused': reused})


@router.post('/research/channels/{channel_id}/intelligence/personal-strategy')
def generate_personal_strategy(channel_id: uuid.UUID, business: str = Form(...), offer: str = Form(...), audience: str = Form(...),
                               market: str = Form(...), goal: str = Form(...), execution: str = Form(...),
                               tone: str = Form(''), constraints: str = Form(''), additional_context: str = Form(''),
                               _auth: None = Depends(require_admin), db: Session = Depends(get_db)):
    analysis = db.scalar(select(ChannelIntelligenceAnalysis).where(ChannelIntelligenceAnalysis.channel_id == channel_id).order_by(
        ChannelIntelligenceAnalysis.created_at.desc(), ChannelIntelligenceAnalysis.id.desc()))
    if analysis is None: raise HTTPException(409, 'Import Channel Intelligence first')
    playbook = _latest_playbook(db, analysis)
    if playbook is None:
        # The product flow should not make a user choose or understand the
        # intermediate public Playbook. Reuse the existing generator here.
        data = _channel_or_404(db, channel_id)
        records = _selection(data, analysis.selection_mode)
        children = [child.intelligence for child, _video in _analysis_video_rows(db, analysis)]
        try:
            provider, model, generate = configured_playbook_generator(get_settings())
            playbook, _reused = get_or_create_playbook(db, channel_id=channel_id, analysis=analysis,
                records=records, videos=children, provider=provider, model=model, generate=generate)
        except Exception:
            return JSONResponse({'ok': False, 'error': 'No pudimos preparar la estrategia del canal en este momento.'}, status_code=503)
    context = _private_context_from_form(business, offer, audience, goal, tone, constraints, market=market,
                                         execution=execution, additional_context=additional_context,
                                         require_strategy_profile=True)
    data = _channel_or_404(db, channel_id)
    records = _selection(data, analysis.selection_mode)
    payload = personal_strategy_input(playbook.payload_json, context, _personal_strategy_evidence(playbook.payload_json, records))
    digest = canonical_json_sha256(payload)
    existing = db.scalar(select(PrivatePersonalStrategy).where(PrivatePersonalStrategy.payload_sha256 == digest))
    if existing is not None: return JSONResponse({'ok': True, 'reused': True, 'strategy': existing.strategy_json})
    try:
        result = configured_personal_generator(get_settings())(payload)
    except Exception:
        return JSONResponse({'ok': False, 'error': 'No pudimos generar tu estrategia personal en este momento.'}, status_code=503)
    if validate_personal_strategy(result, known_video_ids={record['video_id'] for record in records}):
        return JSONResponse({'ok': False, 'error': 'No pudimos generar tu estrategia personal en este momento.'}, status_code=503)
    db.add(PrivatePersonalStrategy(channel_id=channel_id, playbook_id=playbook.id, payload_sha256=digest,
        private_context=context, strategy_json=result)); db.commit()
    return JSONResponse({'ok': True, 'reused': False, 'strategy': result})


def _private_context_from_form(business: str, offer: str, audience: str, goal: str, tone: str, constraints: str,
                               *, market: str = '', execution: str = '', additional_context: str = '',
                               require_strategy_profile: bool = False) -> dict[str, str]:
    values = {'business': business, 'offer': offer, 'audience': audience, 'market': market, 'goal': goal,
              'execution': execution, 'tone': tone, 'constraints': constraints, 'additional_context': additional_context}
    required = ('business', 'offer', 'audience', 'goal') + (('market', 'execution') if require_strategy_profile else ())
    errors = [f'{field} is required' for field in required if not values[field].strip()]
    errors.extend(f'{field} is too long' for field, value in values.items() if len(value) > 4000)
    if errors:
        raise HTTPException(422, {'errors': errors})
    return {field: value.strip() for field, value in values.items()}


def _personal_strategy_evidence(playbook: dict[str, Any], records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Resolve only cited public evidence into a small, human-meaningful strategist input."""
    by_id = {record['video_id']: record for record in records}
    output: list[dict[str, Any]] = []
    for section in ('top_moves', 'what_to_repeat', 'hook_playbook', 'narrative_playbook', 'pain_desire_playbook'):
        rows = playbook.get(section, [])
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            pattern = row.get('title') or row.get('name') or section
            for video_id in row.get('evidence_video_ids', []) if isinstance(row.get('evidence_video_ids'), list) else []:
                record = by_id.get(video_id)
                if record is None:
                    continue
                output.append({'video_id': video_id, 'title': _video_label(record), 'url': record['url'],
                               'views': record.get('views'), 'engagement_rate': record.get('engagement_rate'),
                               'outlier_score': record.get('outlier_score'), 'shares': record.get('shares'),
                               'source_pattern': pattern, 'why_it_matters': row.get('why_it_works') or row.get('why') or ''})
    unique = {item['video_id']: item for item in output}
    return list(unique.values())[:20]


def _latest_personal_strategy(db: Session, channel_id: uuid.UUID) -> PrivatePersonalStrategy | None:
    candidates = list(db.scalars(select(PrivatePersonalStrategy).where(
        PrivatePersonalStrategy.channel_id == channel_id
    ).order_by(PrivatePersonalStrategy.updated_at.desc(), PrivatePersonalStrategy.id.desc())))
    return next((row for row in candidates if not validate_personal_strategy(row.strategy_json)), None)


def _strategy_text_list(rows: Any) -> str:
    if not isinstance(rows, list) or not rows:
        return '<p class="muted">—</p>'
    values = []
    for row in rows[:6]:
        if isinstance(row, str): values.append(f'<li>{_e(row)}</li>')
        elif isinstance(row, dict): values.append(f'<li><b>{_e(row.get("name") or row.get("title") or row.get("hypothesis") or "Prioridad")}</b>{(" — " + _e(row.get("description") or row.get("adaptation") or row.get("what_to_test") or ""))}</li>')
    return '<ul class="compact-list">' + ''.join(values) + '</ul>'


def _strategy_narrative(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ('recommendation', 'summary', 'description', 'strategy', 'text'):
            if isinstance(value.get(key), str) and value[key].strip():
                return value[key]
        return ' · '.join(f'{key}: {item}' for key, item in value.items() if isinstance(item, str)) or '—'
    return '—'


def _personal_strategy_html(strategy: dict[str, Any], records: dict[str, dict[str, Any]]) -> str:
    value = personal_strategy_display(strategy)
    patterns = value.get('patterns_to_adapt', [])
    recommendations = []
    for item in patterns[:5] if isinstance(patterns, list) else []:
        if not isinstance(item, dict):
            continue
        evidence = _evidence_detail([{'video_ids': item.get('evidence_video_ids', []), 'claim': item.get('why_it_works_in_source', '')}], records,
                                    adaptation=item.get('adaptation', ''))
        recommendations.append(f'<article class="strategy-recommendation"><h3>{_e(item.get("name") or "Recomendación")}</h3><p>{_e(item.get("fit_for_business") or "")}</p><p><b>Cómo adaptarlo:</b> {_e(item.get("adaptation") or "")}</p><p><b>Qué no copiar:</b> {_e(item.get("what_not_to_copy") or "")}</p><p><b>Uso recomendado:</b> {_e(item.get("recommended_use") or "")}</p>{evidence}</article>')
    tests = []
    for item in value.get('test_priorities', [])[:5] if isinstance(value.get('test_priorities'), list) else []:
        if not isinstance(item, dict):
            tests.append(f'<li>{_e(item)}</li>'); continue
        tests.append(f'<li><b>{_e(item.get("priority") or "Prioridad")}: {_e(item.get("hypothesis") or "")}</b><br>{_e(item.get("what_to_test") or "")}<br><span class="muted">Señal: {_e(item.get("success_signal") or "")}</span>{_evidence_detail([{"video_ids": item.get("evidence_video_ids", []), "claim": "Patrón que sustenta la prueba."}], records)}</li>')
    legacy = '<p class="muted">Estrategia histórica V1: se conserva legible; genera una nueva estrategia para actualizarla a V2.</p>' if value.get('legacy') else ''
    return f'''<section id="personal-strategy-result" class="strategy-result"><h2>TU ESTRATEGIA</h2>{legacy}<h3>RECOMENDACIÓN EJECUTIVA</h3><p>{_e(_strategy_narrative(value.get('executive_recommendation') or value.get('strategic_fit')))}</p><h3>QUÉ ADAPTAR</h3>{''.join(recommendations) or '<p class="muted">—</p>'}<h3>QUÉ NO COPIAR</h3>{_strategy_text_list(value.get('patterns_to_avoid'))}<div class="compact-columns"><div><h3>DOLORES A TRABAJAR</h3>{_strategy_text_list(value.get('pain_opportunities'))}</div><div><h3>DESEOS A ACTIVAR</h3>{_strategy_text_list(value.get('desire_opportunities'))}</div></div><h3>HOOKS A ADAPTAR</h3>{_strategy_text_list(value.get('hook_adaptations'))}<h3>FÓRMULA DE CONTENIDO</h3>{_strategy_text_list(value.get('narrative_adaptations'))}<h3>CTA RECOMENDADO</h3><p>{_e(_strategy_narrative(value.get('cta_strategy')))}</p><h3>ALINEACIÓN DE OFERTA</h3><p>{_e(_strategy_narrative(value.get('offer_alignment')))}</p><h3>QUÉ PROBAR PRIMERO</h3><ol>{''.join(tests) or '<li>—</li>'}</ol><h3>FIRST CONTENT PLAN</h3>{_strategy_text_list(value.get('first_content_plan'))}<div class="actions"><button type="button" id="create-content-from-strategy">Crear contenido con esta estrategia</button></div></section>'''


def content_pack_prompt(data: dict[str, Any], analysis: ChannelIntelligenceAnalysis, context: dict[str, str],
                        personal_strategy: dict[str, Any] | None = None) -> str:
    patterns = _known_patterns(analysis)
    pattern_lines = '\n'.join(f'- {name}: {item.get("description", "")}' for name, item in patterns.items())
    source = {'channel_id': str(data['channel'].id), 'username': data['channel'].username}
    return f'''# Kurukin Content Pack v1

YOUR TASK IS TO CREATE A DOWNLOADABLE FILE, NOT A MARKDOWN REPORT.

Create and attach exactly one valid JSON file named `kurukin-content-pack.json`.
Do NOT paste a giant JSON blob into chat. If file generation is unavailable, respond exactly:
FILE_GENERATION_UNAVAILABLE

Adapt the proven structures and mechanisms below to the user's business. Do NOT re-analyze the competitor. Do NOT copy the competitor's identity, exact wording, claims, or creative expression. Do not include competitor metrics in user-generated content fields.

PRIVATE USER CONTEXT (do not treat as competitor intelligence):
- Business/product: {context['business']}
- Offer: {context['offer']}
- Target audience: {context['audience']}
- Goal: {context['goal']}
- Tone/style: {context['tone']}
- Constraints: {context['constraints'] or 'None supplied'}

PERSONAL STRATEGY BRIEF (private; creative execution must follow this rather than re-analyzing the channel):
{json.dumps(personal_strategy, ensure_ascii=False, separators=(',', ':')) if personal_strategy else 'No saved Personal Strategy Brief; use only the compact source patterns below.'}

KNOWN SOURCE PATTERNS (use names exactly in source_patterns):
{pattern_lines or '- No named patterns were imported.'}

Every generated idea and script MUST have non-empty `source_patterns`. When a specific imported video inspired it, add its Kurukin ID to `source_evidence_video_ids`; only use IDs supplied by Kurukin in the original intelligence context.

Required root shape (no extra root fields):
{{
  "schema": "{CONTENT_PACK_SCHEMA_VERSION}",
  "source_channel": {json.dumps(source, ensure_ascii=False)},
  "strategy": {{"primary_patterns": ["..."], "recommended_positioning": "...", "content_formula": "...", "recommended_cta_strategy": "...", "recommended_content_mix": "..."}},
  "content_ideas": [{{"title": "...", "objective": "...", "hook": "...", "angle": "...", "pain": "...", "desire": "...", "mechanism": "...", "cta": "...", "source_patterns": ["..."], "source_evidence_video_ids": []}}],
  "scripts": [{{"title": "...", "objective": "...", "duration_target": "...", "hook": "...", "body": "...", "cta": "...", "source_patterns": ["..."], "source_evidence_video_ids": []}}]
}}

Return only the downloadable `kurukin-content-pack.json` file. Do not return a Markdown report.'''


def _creative_context_from_form(business: str = '', offer: str = '', audience: str = '', objective: str = '',
                                market: str = '', tone: str = '', cta_preference: str = '',
                                restrictions: str = '', additional_context: str = '') -> dict[str, str]:
    """Validate a lightweight, ephemeral handoff profile; it is never persisted."""
    values = {
        'business': business, 'offer': offer, 'audience': audience, 'objective': objective,
        'market': market, 'tone': tone, 'cta_preference': cta_preference,
        'restrictions': restrictions, 'additional_context': additional_context,
    }
    errors = [f'{field} is too long' for field, value in values.items() if len(value) > 4000]
    if errors:
        raise HTTPException(422, {'errors': errors})
    return {field: value.strip() for field, value in values.items()}


def _context_item_lines(value: dict[str, Any], key: str, *, limit: int = 8) -> list[str]:
    rows = value.get(key, [])
    if not isinstance(rows, list):
        return []
    result = []
    for item in rows[:limit]:
        if isinstance(item, str) and item.strip():
            result.append(item.strip())
        elif isinstance(item, dict):
            name = str(item.get('name') or item.get('title') or '').strip()
            description = str(item.get('description') or item.get('why_it_matters') or '').strip()
            if name:
                result.append(f'{name} — {description}' if description else name)
    return result


def _representative_evidence(data: dict[str, Any], analysis: ChannelIntelligenceAnalysis,
                             db: Session, *, limit: int = 15) -> list[dict[str, Any]]:
    """Resolve compact public evidence from SCI claims, then fill from its analyzed corpus."""
    records = {record['video_id']: record for record in _selection(data, analysis.selection_mode)}
    children = {video.tiktok_id: child.intelligence for child, video in _analysis_video_rows(db, analysis)}
    reasons: dict[str, list[str]] = {}
    for _section, item in _channel_analysis_items(analysis.channel_intelligence):
        label = str(item.get('name') or item.get('description') or 'Mecanismo observado').strip()
        for evidence in item.get('evidence', []) if isinstance(item.get('evidence'), list) else []:
            if not isinstance(evidence, dict):
                continue
            mechanism = str(evidence.get('claim') or label).strip()
            for video_id in evidence.get('video_ids', []) if isinstance(evidence.get('video_ids'), list) else []:
                if isinstance(video_id, str) and video_id in records:
                    reasons.setdefault(video_id, []).append(mechanism)
    ordered_ids = list(reasons)
    fallback = sorted(records.items(), key=lambda item: (
        -(item[1].get('outlier_score') or -1), -(item[1].get('views') or -1), item[0],
    ))
    ordered_ids.extend(video_id for video_id, _record in fallback if video_id not in reasons)
    output = []
    for video_id in ordered_ids[:limit]:
        record = records[video_id]
        intelligence = children.get(video_id, {})
        hooks = intelligence.get('hooks', []) if isinstance(intelligence, dict) else []
        opening = next((str(item).strip() for item in hooks if isinstance(item, str) and item.strip()), '')
        if not opening:
            opening = (record.get('transcript') or record.get('caption') or '').strip()[:280]
        output.append({
            'title': _human_video_title(record), 'url': record.get('url') or '',
            'views': record.get('views'), 'likes': record.get('likes'), 'comments': record.get('comments'),
            'shares': record.get('shares'), 'engagement_rate': record.get('engagement_rate'),
            'outlier_score': record.get('outlier_score'), 'hook': opening,
            'transcript_excerpt': (record.get('transcript') or '').strip()[:1200],
            'mechanism': '; '.join(dict.fromkeys(reasons.get(video_id, []))) or 'Video representativo del corpus analizado.',
            'why_it_matters': 'Aporta evidencia pública para los patrones y mecanismos descritos arriba.',
        })
    return output


def creative_context_markdown(data: dict[str, Any], analysis: ChannelIntelligenceAnalysis,
                              context: dict[str, str], db: Session) -> str:
    """Create a local Markdown attachment solely from current SCI, corpus, and form inputs."""
    value = analysis.channel_intelligence
    mechanisms = _context_item_lines(value, 'winning_patterns', limit=6) or _context_item_lines(value, 'hooks', limit=6)
    formula = ' → '.join(item.split(' — ', 1)[0] for item in mechanisms[:4]) or 'No se importó una fórmula dominante explícita.'
    sections = [
        ('FÓRMULA DOMINANTE', [formula]), ('PATRONES MÁS FUERTES', mechanisms),
        ('HOOKS', _context_item_lines(value, 'hooks')), ('DOLORES', _context_item_lines(value, 'pains')),
        ('DESEOS', _context_item_lines(value, 'desires')), ('NARRATIVAS', _context_item_lines(value, 'narratives')),
        ('CTAS Y OFERTAS', _context_item_lines(value, 'ctas') + _context_item_lines(value, 'offers')),
        ('MECANISMOS DE REPETICIÓN', _context_item_lines(value, 'repetition_clusters')),
    ]
    intelligence = '\n\n'.join(
        f'## {title}\n\n' + ('\n'.join(f'- {item}' for item in rows) if rows else '- Sin información explícita importada.')
        for title, rows in sections
    )
    evidence_blocks = []
    for index, item in enumerate(_representative_evidence(data, analysis, db), 1):
        metrics = ' · '.join(f'{label}: {value}' for label, value in (
            ('Views', item['views']), ('Likes', item['likes']), ('Comments', item['comments']),
            ('Shares', item['shares']), ('Engagement', item['engagement_rate']), ('Outlier', item['outlier_score']),
        ) if value is not None) or 'Métricas no disponibles.'
        excerpt = item['transcript_excerpt'] or 'No hay extracto de transcripción disponible.'
        evidence_blocks.append(
            f"## {index}. {item['title']}\n\nTikTok: {item['url']}\n\nMétricas: {metrics}\n\n"
            f"Hook / apertura: {item['hook'] or 'No disponible.'}\n\nExtracto de transcripción:\n\n> {excerpt}\n\n"
            f"Mecanismo observado: {item['mechanism']}\n\nPor qué importa: {item['why_it_matters']}"
        )
    profile = '\n'.join(f'- {label}: {context[key] or "No especificado"}' for key, label in (
        ('business', 'Negocio / producto'), ('offer', 'Oferta'), ('audience', 'Audiencia'),
        ('objective', 'Objetivo'), ('market', 'Mercado'), ('tone', 'Tono'),
        ('cta_preference', 'Preferencia de CTA'), ('restrictions', 'Restricciones'),
        ('additional_context', 'Contexto adicional'),
    ))
    return f'''# Kurukin Creative Context

Canal de referencia: @{data['channel'].username}

Este documento contiene inteligencia y evidencia observada. Transfiere mecanismos, no expresiones, identidad ni afirmaciones del creador.

# CHANNEL INTELLIGENCE

{intelligence}

# REPRESENTATIVE EVIDENCE

{chr(10).join(evidence_blocks) or 'No hay videos representativos disponibles.'}

# USER CONTEXT

{profile}
'''


def creative_super_prompt(context: dict[str, str]) -> str:
    return f'''Study the attached Kurukin Creative Context before proposing creative work. Understand the channel intelligence, its evidence, and the user's business context.

Do not copy competitor wording, identity, claims, examples, or creative expression. Transfer mechanisms, not expressions. Treat representative successful scripts as modeling evidence, not templates to reproduce. Work interactively with the user and respect the stated restrictions.

Your first response must propose the 5 strongest campaigns. For each campaign include:
- Concept
- Audience pain or desire
- Mechanism
- 3 hook options
- Objective
- CTA direction
- Evidence rationale grounded in the attached Kurukin context

Then ask which campaign the user wants to develop. After that, continue freely: the user may ask for a script, a shorter or more provocative version, more hooks, a CTA change, a Reels adaptation, or another variation. Do not require JSON and do not ask the user to return or import anything into Kurukin.

User context summary:
- Business/product: {context['business'] or 'Not specified'}
- Offer: {context['offer'] or 'Not specified'}
- Audience: {context['audience'] or 'Not specified'}
- Objective: {context['objective'] or 'Not specified'}
- Market: {context['market'] or 'Not specified'}
- Tone: {context['tone'] or 'Not specified'}
- CTA preference: {context['cta_preference'] or 'Not specified'}
- Restrictions: {context['restrictions'] or 'None specified'}
- Additional context: {context['additional_context'] or 'None specified'}'''


@router.post('/research/channels/{channel_id}/intelligence/creative-context.md')
def download_creative_context(channel_id: uuid.UUID, business: str = Form(''), offer: str = Form(''),
                              audience: str = Form(''), objective: str = Form(''), market: str = Form(''),
                              tone: str = Form(''), cta_preference: str = Form(''), restrictions: str = Form(''),
                              additional_context: str = Form(''), _auth: None = Depends(require_admin),
                              db: Session = Depends(get_db)):
    data = _channel_or_404(db, channel_id)
    analysis = db.scalar(select(ChannelIntelligenceAnalysis).where(ChannelIntelligenceAnalysis.channel_id == channel_id).order_by(
        ChannelIntelligenceAnalysis.updated_at.desc(), ChannelIntelligenceAnalysis.id.desc()))
    if analysis is None:
        raise HTTPException(409, 'Import Channel Intelligence before preparing creative context')
    context = _creative_context_from_form(business, offer, audience, objective, market, tone, cta_preference,
                                          restrictions, additional_context)
    return Response(creative_context_markdown(data, analysis, context, db), media_type='text/markdown; charset=utf-8',
                    headers={'Content-Disposition': 'attachment; filename="kurukin-creative-context.md"'})


@router.post('/research/channels/{channel_id}/intelligence/creative-super-prompt', response_class=PlainTextResponse)
def creative_super_prompt_text(channel_id: uuid.UUID, business: str = Form(''), offer: str = Form(''),
                               audience: str = Form(''), objective: str = Form(''), market: str = Form(''),
                               tone: str = Form(''), cta_preference: str = Form(''), restrictions: str = Form(''),
                               additional_context: str = Form(''), _auth: None = Depends(require_admin),
                               db: Session = Depends(get_db)):
    _channel_or_404(db, channel_id)
    if db.scalar(select(ChannelIntelligenceAnalysis.id).where(
        ChannelIntelligenceAnalysis.channel_id == channel_id
    )) is None:
        raise HTTPException(409, 'Import Channel Intelligence before preparing the Super Prompt')
    return PlainTextResponse(creative_super_prompt(_creative_context_from_form(
        business, offer, audience, objective, market, tone, cta_preference, restrictions, additional_context)))


@router.get('/research/channels/{channel_id}/intelligence', response_class=HTMLResponse)
def channel_intelligence_page(channel_id: uuid.UUID, _auth: None = Depends(require_admin), db: Session = Depends(get_db)):
    """Product Lite's complete public journey; legacy creation systems remain on unlinked routes."""
    data = _channel_or_404(db, channel_id)
    channel = data['channel']
    analyses = list(db.scalars(select(ChannelIntelligenceAnalysis).where(
        ChannelIntelligenceAnalysis.channel_id == channel_id
    ).order_by(ChannelIntelligenceAnalysis.updated_at.desc(), ChannelIntelligenceAnalysis.id.desc())))
    latest = analyses[0] if analyses else None
    channel_header = f'''<p><a href="/admin/research">← Canales</a></p>{_channel_context(channel.username, channel.nickname)}
<section><p class="eyebrow">CANAL</p><div class="stat-grid"><div class="stat"><b>{data['total_videos']}</b>videos encontrados</div><div class="stat"><b>{data['priority_transcripts']}</b>transcripciones disponibles</div></div></section>'''
    history = ''.join(f'<li><a href="/admin/research/channels/{channel_id}/intelligence/{analysis.id}">Inteligencia importada · {_when(analysis.updated_at)}</a></li>' for analysis in analyses)
    history_section = f'<details><summary>Historial de inteligencia importada</summary><ul>{history}</ul></details>' if history else ''
    advanced = f'''<details class="card technical"><summary><b>Advanced / Legacy</b></summary><div class="step-body"><p>Herramientas técnicas y flujos preservados, fuera del recorrido normal.</p>{history_section}<div class="actions"><a class="button secondary" href="/admin/research/channels/{channel_id}/import">Importar transcripciones históricas</a><a class="button secondary" href="/admin/research/channels/{channel_id}/export">Exportar Research Pack</a><a class="button secondary" href="/admin/research/channels/{channel_id}/prompt">Generador legacy</a><a class="button secondary" href="/admin/system">Admin / Debug</a></div></div></details>'''
    if latest is None:
        content = f'''{channel_header}<section><h2>INTELIGENCIA</h2><div class="card hero"><p>Cuando el corpus esté listo, analiza el canal para descubrir qué funciona.</p><div class="actions"><a class="button" href="/admin/research/channels/{channel_id}/intelligence/action">Preparar Structured Channel Intelligence</a></div></div></section>{advanced}'''
        return _layout(f'@{channel.username}', content)
    state, records, _delta = _knowledge_state(db, data, latest)
    update_notice = '' if state['state'] == 'FRESH' else f'''<div class="card warn"><p>Hay evidencia nueva disponible. La última inteligencia sigue disponible.</p><div class="actions"><a class="button secondary" href="/admin/research/channels/{channel_id}/intelligence/action">Actualizar inteligencia</a></div></div>'''
    evidence = _representative_evidence(data, latest, db)
    evidence_html = ''.join(
        f'''<article class="card video-card"><h3>{_e(item['title'])}</h3><p><a href="{_e(item['url'])}" target="_blank" rel="noopener">Abrir en TikTok</a></p><p class="muted">Views: {_e(item['views'] if item['views'] is not None else '—')} · Engagement: {_e(item['engagement_rate'] if item['engagement_rate'] is not None else '—')} · Outlier: {_e(item['outlier_score'] if item['outlier_score'] is not None else '—')}</p><p><b>Hook / apertura</b><br>{_e(item['hook'] or 'No disponible.')}</p><p><b>Mecanismo observado</b><br>{_e(item['mechanism'])}</p><p><b>Por qué importa</b><br>{_e(item['why_it_matters'])}</p><details><summary>Ver extracto de transcripción</summary><p>{_e(item['transcript_excerpt'] or 'No disponible.')}</p></details></article>'''
        for item in evidence
    ) or '<p class="muted">No hay evidencia representativa disponible.</p>'
    content = f'''{channel_header}{update_notice}
<section id="inteligencia"><p class="eyebrow">1. ¿QUÉ FUNCIONA?</p>{_actionable_playbook(data, latest)}</section>
<section id="evidencia"><p class="eyebrow">2. EVIDENCIA</p><h2>Videos representativos</h2><p class="muted">Una selección compacta de evidencia pública y transcripciones ya importadas.</p><details class="card"><summary><b>Ver evidencia representativa ({len(evidence)} videos)</b></summary><div class="step-body">{evidence_html}</div></details></section>
<section id="usar-inteligencia" class="card"><p class="eyebrow">3. USAR ESTA INTELIGENCIA</p><h2>Contexto de tu negocio</h2><p class="muted">Este formulario es privado y solo prepara tus archivos en el navegador. No genera una estrategia ni guarda una campaña.</p><form id="creative-context-form"><label>Negocio / producto<br><textarea name="business" maxlength="4000" style="min-height:70px"></textarea></label><label>Oferta<br><textarea name="offer" maxlength="4000" style="min-height:70px"></textarea></label><label>Audiencia<br><textarea name="audience" maxlength="4000" style="min-height:70px"></textarea></label><label>Objetivo<br><input name="objective" maxlength="4000"></label><label>Mercado<br><input name="market" maxlength="4000"></label><label>Tono<br><input name="tone" maxlength="4000"></label><label>Preferencia de CTA<br><input name="cta_preference" maxlength="4000"></label><label>Restricciones<br><textarea name="restrictions" maxlength="4000" style="min-height:70px"></textarea></label><label>Contexto adicional<br><textarea name="additional_context" maxlength="4000" style="min-height:70px"></textarea></label><div class="actions"><button id="download-creative-context" type="button">Descargar contexto creativo</button><button id="open-hormozi" type="button" class="secondary">Copiar Super Prompt y abrir Alex Hormozi GPT</button></div></form><p id="creative-context-status" class="muted" aria-live="polite"></p><p class="muted">Adjunta <code>kurukin-creative-context.md</code> al GPT después de descargarlo.</p></section>
<details class="card"><summary><b>Ver análisis completo</b></summary><div class="step-body">{_channel_intelligence_results(data, latest, db)}</div></details>{advanced}
<script>(function(){{const form=document.getElementById('creative-context-form'),status=document.getElementById('creative-context-status'),base='/admin/research/channels/{channel_id}/intelligence',gptUrl='{HORMOZI_GPT_URL}';function body(){{return new FormData(form)}}async function download(){{const r=await fetch(base+'/creative-context.md',{{method:'POST',body:body()}});if(!r.ok)throw new Error('No se pudo preparar el contexto creativo.');const blob=await r.blob(),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='kurukin-creative-context.md';document.body.appendChild(a);a.click();a.remove();URL.revokeObjectURL(url)}}document.getElementById('download-creative-context').addEventListener('click',async()=>{{try{{await download();status.textContent='Contexto creativo descargado.'}}catch(error){{status.textContent=error.message}}}});document.getElementById('open-hormozi').addEventListener('click',async()=>{{const tab=window.open(gptUrl,'_blank','noopener');try{{const r=await fetch(base+'/creative-super-prompt',{{method:'POST',body:body()}});if(!r.ok)throw new Error();const prompt=await r.text();await navigator.clipboard.writeText(prompt);status.textContent='Super Prompt copiado. Alex Hormozi GPT se abrió en otra pestaña.'}}catch(error){{status.textContent=tab?'Alex Hormozi GPT se abrió. No se pudo copiar el Super Prompt automáticamente.':'No se pudo abrir el GPT ni copiar el Super Prompt.'}}}})}})();</script>'''
    return _layout('Inteligencia del canal', content)

    # Kept below solely as source-level legacy implementation reference. Every
    # normal request returns through the Product Lite flow above.
    data = _channel_or_404(db, channel_id)
    analyses = list(db.scalars(select(ChannelIntelligenceAnalysis).where(
        ChannelIntelligenceAnalysis.channel_id == channel_id
    ).order_by(ChannelIntelligenceAnalysis.updated_at.desc(), ChannelIntelligenceAnalysis.id.desc())))
    latest = analyses[0] if analyses else None
    channel = data['channel']
    channel_header = f'''<p><a href="/admin/research">← Canales</a></p><h2>@{_e(channel.username)}</h2><p class="muted">{_e(channel.nickname)}</p>
<section><p class="eyebrow">DATOS DEL CANAL</p><div class="stat-grid"><div class="stat"><b>✓ {data['total_videos']}</b>videos encontrados</div><div class="stat"><b>✓ {data['priority_count']}</b>videos prioritarios</div><div class="stat"><b>✓ {data['priority_transcripts']}</b>transcripciones prioritarias</div></div></section>'''
    if latest is None:
        label, href = _knowledge_action('NO_INTELLIGENCE', channel_id)
        content = f'''{channel_header}<section><h2>INTELIGENCIA</h2><div class="card hero"><span class="badge warn">Falta analizar</span><p>Este canal está listo para convertir sus transcripciones en inteligencia útil.</p><div class="actions"><a class="button" href="{href}">{label}</a></div></div></section>
<details class="card technical"><summary><b>Opciones avanzadas</b></summary><div class="step-body"><p>Herramientas de corpus y diagnóstico para operadores.</p><div class="actions"><a class="button secondary" href="/admin/research/channels/{channel_id}/import">Importar transcripciones históricas</a><a class="button secondary" href="/admin/research/channels/{channel_id}/export">Exportar Research Pack</a><a class="button secondary" href="/admin/research/channels/{channel_id}/prompt">Generador legacy</a></div><details><summary>Detalles técnicos</summary><p>Estado de corpus: <code>{_e(data['status'])}</code></p></details></div></details>'''
        return _layout(f'@{channel.username}', content)
    state, state_records, delta_ids = _knowledge_state(db, data, latest)
    status_text = _human_knowledge_state(state['state'])
    pending_notice = '' if state['state'] == 'FRESH' else '<p class="muted">Mostrando la última inteligencia disponible. Hay una actualización pendiente.</p>'
    action_label, action_href = _knowledge_action(state['state'], channel_id)
    primary = (f'<button type="button" id="adapt-business">Adaptar esto a mi negocio</button>' if state['state'] == 'FRESH'
               else f'<a class="button" href="{action_href}">{action_label}</a>')
    playbook = _latest_playbook(db, latest)
    records_by_id = {record['video_id']: record for record in state_records}
    packs = list(db.scalars(select(PrivateContentPack).where(PrivateContentPack.channel_id == channel_id).order_by(PrivateContentPack.updated_at.desc())))
    latest_pack = packs[0] if packs else None
    pack_results = _content_pack_results(data, latest, latest_pack) if latest_pack else '<p class="muted">Aún no has importado contenido privado.</p>'
    advanced_playbook = _strategic_playbook_html(playbook, records_by_id) if playbook else '<p>El Playbook estratégico generado sigue disponible para operaciones cuando se necesite.</p><button type="button" id="generate-playbook" class="secondary">Generar Playbook técnico</button><p id="playbook-status" class="muted"></p>'
    latest_strategy = _latest_personal_strategy(db, channel_id)
    profile = latest_strategy.private_context if latest_strategy and isinstance(latest_strategy.private_context, dict) else {}
    def profile_value(name: str) -> str: return _e(profile.get(name, ''))
    latest_strategy_html = _personal_strategy_html(latest_strategy.strategy_json, records_by_id) if latest_strategy else ''
    strategy_start = '<details id="strategy-flow" class="card"><summary><b>Actualizar perfil y estrategia</b></summary><div class="step-body">' if latest_strategy else '<section id="strategy-flow" class="card">'
    strategy_end = '</div></details>' if latest_strategy else '</section>'
    content = f'''{channel_header}<section><h2>INTELIGENCIA</h2><div class="card hero"><span class="badge {'ok' if state['state'] == 'FRESH' else 'warn'}">{_e(status_text)}</span>{pending_notice}<div class="actions">{primary}</div></div></section>{_actionable_playbook(data, latest)}
<details class="card"><summary><b>Ver análisis completo</b></summary><div class="step-body">{_channel_intelligence_results(data, latest, db)}</div></details>
{latest_strategy_html}{strategy_start}<h2>ADAPTAR ESTA INTELIGENCIA A MI NEGOCIO</h2><p class="muted">Kurukin no copiará el contenido del competidor. Usará patrones respaldados por evidencia para construir una estrategia compatible con tu negocio.</p><form id="private-context"><label>¿Qué vendes?<br><textarea name="business" required maxlength="4000" style="min-height:80px">{profile_value('business')}</textarea></label><label>¿Cuál es tu oferta principal?<br><textarea name="offer" required maxlength="4000" style="min-height:80px">{profile_value('offer')}</textarea></label><label>¿A quién vendes?<br><textarea name="audience" required maxlength="4000" style="min-height:80px">{profile_value('audience')}</textarea></label><label>¿En qué mercado operas?<br><input name="market" required maxlength="4000" value="{profile_value('market')}"></label><label>¿Qué quieres conseguir?<br><select name="goal" required>{''.join(f'<option value="{choice}" {"selected" if profile.get("goal", "Leads") == choice else ""}>{choice}</option>' for choice in ('Leads', 'Ventas', 'Autoridad', 'Audiencia'))}</select></label><label>¿Qué puedes ejecutar actualmente?<br><textarea name="execution" required maxlength="4000" style="min-height:80px">{profile_value('execution')}</textarea></label><label>Tono / estilo <span class="muted">(opcional)</span><br><input name="tone" maxlength="4000" value="{profile_value('tone')}"></label><label>Restricciones <span class="muted">(opcional)</span><br><textarea name="constraints" maxlength="4000" style="min-height:80px">{profile_value('constraints')}</textarea></label><label>Contexto adicional <span class="muted">(opcional)</span><br><textarea name="additional_context" maxlength="4000" style="min-height:80px">{profile_value('additional_context')}</textarea></label><div class="actions"><button id="generate-personal-strategy" type="button">Generar mi estrategia</button></div></form><p id="strategy-status" class="muted" aria-live="polite"></p>{strategy_end}
<details class="card step" id="create-flow"><summary><b>CREAR CONTENIDO</b></summary><div class="step-body"><p>Convierte tu estrategia privada en ideas, hooks, CTAs y guiones.</p><div class="actions"><button id="open-hormozi" type="button">Crear contenido</button></div><ol><li>Las instrucciones se copian y se abre el creador.</li><li>Descarga <code>kurukin-content-pack.json</code>.</li><li>Súbelo para ver tus ideas y guiones aquí.</li></ol><form id="content-pack-upload"><label class="dropzone">Sube <b>kurukin-content-pack.json</b><br><span class="button secondary">Seleccionar archivo</span><input id="content-pack-file" type="file" accept=".json,application/json"></label></form><div id="content-pack-dry-run" aria-live="polite"></div></div></details>
<section id="content-plan"><h2>IDEAS DE CONTENIDO Y GUIONES</h2><div id="content-pack-results">{pack_results}</div></section>
<details class="card technical"><summary><b>Opciones avanzadas</b></summary><div class="step-body">{advanced_playbook}<div class="actions"><a class="button secondary" href="/admin/research/channels/{channel_id}/import">Importar transcripciones históricas</a><a class="button secondary" href="/admin/research/channels/{channel_id}/export">Exportar Research Pack</a><a class="button secondary" href="/admin/research/channels/{channel_id}/prompt">Generador legacy</a><a class="button secondary" href="/admin/system">Admin / Debug</a></div><details><summary>Detalles técnicos</summary><p>Estado interno: <code>{_e(state['state'])}</code> · hash semántico <code>{_e(state['semantic_corpus_hash'][:12])}</code> · hash de rendimiento <code>{_e(state['performance_state_hash'][:12])}</code></p></details></div></details>
<script>(function(){{const base='/admin/research/channels/{channel_id}/intelligence',form=document.getElementById('private-context'),strategy=document.getElementById('strategy-flow'),create=document.getElementById('create-flow'),personal=document.getElementById('generate-personal-strategy'),adapt=document.getElementById('adapt-business'),technical=document.getElementById('generate-playbook'),status=document.getElementById('strategy-status');if(adapt)adapt.onclick=()=>{{strategy.open=true;strategy.scrollIntoView({{behavior:'smooth',block:'start'}})}};if(technical)technical.onclick=async()=>{{const r=await fetch(base+'/playbook/generate',{{method:'POST'}}),x=await r.json();if(x.ok)location.reload();else document.getElementById('playbook-status').textContent=x.error||'No se pudo generar el Playbook.'}};function contextData(){{return new FormData(form)}}if(personal)personal.onclick=async()=>{{status.textContent='Generando estrategia...';personal.disabled=true;const r=await fetch(base+'/personal-strategy',{{method:'POST',body:contextData()}}),x=await r.json();if(!r.ok||!x.ok){{status.textContent=(x.error||'Strategy failed — retry');personal.disabled=false;return}}status.textContent='Strategy ready';location.reload()}};const createFromStrategy=document.getElementById('create-content-from-strategy');if(createFromStrategy)createFromStrategy.onclick=()=>{{create.open=true;create.scrollIntoView({{behavior:'smooth',block:'start'}})}};document.getElementById('open-hormozi').onclick=async()=>{{const r=await fetch(base+'/content-pack/prompt',{{method:'POST',body:contextData()}});if(!r.ok){{status.textContent='Genera primero tu estrategia y completa los campos requeridos.';return}}await navigator.clipboard.writeText(await r.text());window.open('https://chatgpt.com/g/g-68a6de0c7ec48191876f8297e467fc7c-alex-hormozi-100m','_blank','noopener');status.textContent='Instrucciones copiadas. El creador se abrió en otra pestaña.'}};async function dryRun(file){{const body=contextData();body.append('file',file,file.name);const r=await fetch(base+'/content-pack/import/dry-run',{{method:'POST',body}}),x=await r.json(),box=document.getElementById('content-pack-dry-run');if(!x.ok){{box.innerHTML='<div class="error-box"><b>No se puede importar.</b><ul>'+x.errors.map(v=>'<li>'+v+'</li>').join('')+'</ul></div>';return}}box.innerHTML='<div class="card"><h3>Listo para confirmar</h3><p>Ideas: '+x.summary.ideas+' · Guiones: '+x.summary.scripts+'</p><button id="confirm-content-pack">Confirmar contenido</button></div>';document.getElementById('confirm-content-pack').onclick=async()=>{{const body=new FormData();body.append('token',x.token);const confirmed=await fetch(base+'/content-pack/import/confirm',{{method:'POST',body}});if((await confirmed.json()).ok)location.reload()}}}}document.getElementById('content-pack-file').addEventListener('change',e=>{{if(e.target.files[0])dryRun(e.target.files[0])}})}})();</script>'''
    return _layout('Inteligencia del canal', content)


@router.get('/research/channels/{channel_id}/intelligence/action', response_class=HTMLResponse)
def channel_intelligence_action(channel_id: uuid.UUID, _auth: None = Depends(require_admin), db: Session = Depends(get_db)):
    """Select the pre-existing analysis/update workflow from the live knowledge state."""
    data = _channel_or_404(db, channel_id)
    latest = db.scalar(select(ChannelIntelligenceAnalysis).where(
        ChannelIntelligenceAnalysis.channel_id == channel_id
    ).order_by(ChannelIntelligenceAnalysis.updated_at.desc(), ChannelIntelligenceAnalysis.id.desc()))
    if latest is None:
        return channel_intelligence_prompt_page(channel_id, db=db)
    state, _records, delta = _knowledge_state(db, data, latest)
    if state['state'] != 'SEMANTIC_DELTA':
        return channel_intelligence_prompt_page(channel_id, db=db)
    return _external_ai_handoff_page(
        channel_id=channel_id, username=data['channel'].username, nickname=data['channel'].nickname,
        expected_filename=channel_analysis_filename(data['channel'].username,
                                                    research_pack_hash(data, latest.selection_mode)),
        title='Actualizar inteligencia',
        prompt=channel_intelligence_update_prompt(data, latest, state, delta),
        download_href=f'/admin/research/channels/{channel_id}/intelligence/update.zip',
        import_base=f'/admin/research/channels/{channel_id}/intelligence/update/import',
        has_current_intelligence=True,
    )


@router.get('/research/channels/{channel_id}/intelligence/prompt', response_class=HTMLResponse)
def channel_intelligence_prompt_page(channel_id: uuid.UUID, mode: str = 'all', _auth: None = Depends(require_admin), db: Session = Depends(get_db)):
    data = _channel_or_404(db, channel_id)
    return _external_ai_handoff_page(
        channel_id=channel_id, username=data['channel'].username, nickname=data['channel'].nickname,
        expected_filename=channel_analysis_filename(data['channel'].username, research_pack_hash(data, mode)),
        title='Generar inteligencia',
        prompt=channel_intelligence_prompt(data, mode),
        download_href=f'/admin/research/channels/{channel_id}/export.zip?mode={_e(mode)}',
        import_base=f'/admin/research/channels/{channel_id}/intelligence/import',
    )


@router.get('/research/channels/{channel_id}/intelligence/prompt.txt', response_class=PlainTextResponse)
def channel_intelligence_prompt_text(channel_id: uuid.UUID, mode: str = 'recommended', _auth: None = Depends(require_admin), db: Session = Depends(get_db)):
    return PlainTextResponse(channel_intelligence_prompt(_channel_or_404(db, channel_id), mode))


def _dry_run_summary(value: dict[str, Any], data: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, Any]:
    videos = value['videos']; expected = {record['video_id'] for record in records}
    received = [item.get('video_id') for item in videos if isinstance(item, dict)]
    channel_value = value['channel_intelligence']
    counts = {key: len(channel_value.get(key, [])) for key in ('hooks', 'pains', 'desires', 'topics', 'narratives', 'repetition_clusters', 'ctas', 'offers', 'winning_patterns', 'opportunities')}
    evidence = [video_id for item in videos for entry in item.get('evidence', []) for video_id in entry.get('video_ids', [])]
    return {'schema': value['schema'], 'channel': data['channel'].username, 'expected': len(expected), 'received': len(videos),
            'missing': len(expected - set(received)), 'unknown': len(set(received) - expected), 'duplicates': len(received) - len(set(received)),
            'invalid_evidence': len(set(evidence) - expected), 'counts': counts}


@router.post('/research/channels/{channel_id}/intelligence/import/dry-run')
async def channel_intelligence_import_dry_run(channel_id: uuid.UUID, file: UploadFile = File(...), _auth: None = Depends(require_admin), db: Session = Depends(get_db)):
    data = _channel_or_404(db, channel_id)
    value, errors, warnings, mode, status = dry_run_channel_intelligence_import(
        db, channel_id, await file.read(CHANNEL_INTELLIGENCE_IMPORT_MAX_BYTES + 1)
    )
    if value is None or errors:
        return JSONResponse({'ok': False, 'errors': errors or ['Upload one valid JSON object.']}, status_code=422)
    assert mode is not None and status is not None
    token = _store_pending_channel_intelligence(PendingChannelIntelligenceImport(
        channel_id, value, mode, canonical_json_sha256(value), time.time()))
    return JSONResponse({'ok': True, 'token': token, 'status': status, 'warnings': warnings,
                         'summary': _dry_run_summary(value, data, _selection(data, mode))})


@router.post('/research/channels/{channel_id}/intelligence/import/confirm')
def channel_intelligence_import_confirm(channel_id: uuid.UUID, token: str = Form(...), _auth: None = Depends(require_admin), db: Session = Depends(get_db)):
    pending = _take_pending_channel_intelligence(token, channel_id)
    data = _channel_or_404(db, channel_id)
    # Re-validate the live corpus: a transcript/import change between preview
    # and confirmation must not silently attach analysis to different evidence.
    normalized_payload, errors, _warnings, mode, _status = dry_run_channel_intelligence_import(
        db, channel_id, json.dumps(pending.payload, ensure_ascii=False).encode('utf-8'))
    if errors or normalized_payload is None or mode != pending.selection_mode:
        raise HTTPException(409, 'Research Pack changed since dry run; export and analyze it again')
    payload_sha256 = canonical_json_sha256(normalized_payload)
    pack_hash = normalized_payload['research_pack']['hash']
    analysis = db.scalar(select(ChannelIntelligenceAnalysis).where(
        ChannelIntelligenceAnalysis.channel_id == channel_id,
        ChannelIntelligenceAnalysis.research_pack_hash == pack_hash,
        ChannelIntelligenceAnalysis.schema_version == CHANNEL_INTELLIGENCE_SCHEMA_VERSION,
    ).with_for_update())
    if analysis is not None and analysis.payload_sha256 == payload_sha256:
        return JSONResponse({'ok': True, 'already_imported': True, 'analysis_id': str(analysis.id)})
    changed = analysis is not None
    if analysis is None:
        analysis = ChannelIntelligenceAnalysis(channel_id=channel_id, research_pack_hash=pack_hash,
            schema_version=CHANNEL_INTELLIGENCE_SCHEMA_VERSION, selection_mode=mode,
            payload_sha256=payload_sha256, channel_intelligence=normalized_payload['channel_intelligence'],
            semantic_corpus_hash=semantic_corpus_hash(_selection(data, mode)),
            performance_state_hash=performance_state_hash(_selection(data, mode)),
            analysis_contract_version=ANALYSIS_CONTRACT_VERSION)
        db.add(analysis); db.flush()
    else:
        raise HTTPException(409, 'Historical Channel Intelligence snapshots are immutable')
    ids = [item['video_id'] for item in normalized_payload['videos']]
    videos = {video.tiktok_id: video for video in db.scalars(select(Video).where(
        Video.channel_id == channel_id, Video.tiktok_id.in_(ids)
    ))}
    if set(videos) != set(ids):
        raise HTTPException(409, 'Research Pack videos changed since dry run; export and analyze it again')
    for item in normalized_payload['videos']:
        db.add(ChannelVideoIntelligence(analysis_id=analysis.id, video_id=videos[item['video_id']].id,
                                        intelligence=item,
                                        semantic_source_hash=canonical_json_sha256(semantic_video(next(
                                            row for row in _selection(data, mode) if row['video_id'] == item['video_id'])))))
    db.commit()
    return JSONResponse({'ok': True, 'analysis_id': str(analysis.id), 'action': 'updated' if changed else 'imported'})


def _analysis_video_rows(db: Session, analysis: ChannelIntelligenceAnalysis) -> list[tuple[ChannelVideoIntelligence, Video]]:
    return list(db.execute(select(ChannelVideoIntelligence, Video).join(Video, Video.id == ChannelVideoIntelligence.video_id).where(
        ChannelVideoIntelligence.analysis_id == analysis.id)).all())


def _knowledge_state(db: Session, data: dict[str, Any], analysis: ChannelIntelligenceAnalysis | None) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    records = _selection(data, analysis.selection_mode if analysis else 'all')
    state = freshness(analysis, records)
    if analysis is None:
        return state, records, state['new_or_changed_ids']
    known = {video.tiktok_id: child.semantic_source_hash for child, video in _analysis_video_rows(db, analysis)}
    delta = changed_ids(records, known) if state['state'] == 'SEMANTIC_DELTA' else []
    state['new_or_changed_ids'] = delta
    return state, records, delta


def incremental_intelligence_update_pack(db: Session, channel_id: uuid.UUID) -> tuple[bytes, dict[str, Any]]:
    data = _channel_or_404(db, channel_id)
    prior = db.scalar(select(ChannelIntelligenceAnalysis).where(ChannelIntelligenceAnalysis.channel_id == channel_id).order_by(
        ChannelIntelligenceAnalysis.created_at.desc(), ChannelIntelligenceAnalysis.id.desc()))
    if prior is None:
        raise HTTPException(409, 'A baseline Channel Intelligence analysis is required before an incremental update')
    state, records, delta = _knowledge_state(db, data, prior)
    if state['state'] != 'SEMANTIC_DELTA' or not delta:
        raise HTTPException(409, 'No semantic video delta is available for an incremental update')
    known = [child.intelligence for child, _video in _analysis_video_rows(db, prior)]
    return incremental_pack(channel=data['channel'], prior=prior, known=known, records=records, delta_ids=delta,
                            semantic_hash=state['semantic_corpus_hash'], performance_hash=state['performance_state_hash']), state


@router.get('/research/channels/{channel_id}/intelligence/update.zip')
def incremental_intelligence_update_export(channel_id: uuid.UUID, _auth: None = Depends(require_admin), db: Session = Depends(get_db)):
    body, _state = incremental_intelligence_update_pack(db, channel_id)
    channel = _channel_or_404(db, channel_id)['channel']
    return Response(body, media_type='application/zip', headers={'Content-Disposition': f'attachment; filename="kurukin-channel-update-{channel.username}.zip"'})


def dry_run_channel_intelligence_update(db: Session, channel_id: uuid.UUID, value: dict[str, Any]) -> tuple[list[str], ChannelIntelligenceAnalysis | None, list[dict[str, Any]], list[str]]:
    data = _channel_or_404(db, channel_id)
    prior = db.scalar(select(ChannelIntelligenceAnalysis).where(ChannelIntelligenceAnalysis.channel_id == channel_id).order_by(
        ChannelIntelligenceAnalysis.created_at.desc(), ChannelIntelligenceAnalysis.id.desc()))
    if prior is None: return ['A baseline Channel Intelligence analysis is required'], None, [], []
    state, records, delta = _knowledge_state(db, data, prior)
    if state['state'] != 'SEMANTIC_DELTA' or not delta:
        return ['No current semantic delta matches this update'], prior, records, delta
    merged_ids = {video.tiktok_id for _child, video in _analysis_video_rows(db, prior)} | set(delta)
    errors = validate_channel_update(value, base_analysis_id=str(prior.id), base_payload_sha=prior.payload_sha256,
        base_semantic_hash=prior.semantic_corpus_hash, target_semantic_hash=state['semantic_corpus_hash'],
        expected_ids=set(delta), merged_ids=merged_ids)
    return errors, prior, records, delta


def import_channel_intelligence_update(db: Session, channel_id: uuid.UUID, value: dict[str, Any]) -> ChannelIntelligenceAnalysis:
    errors, prior, records, delta = dry_run_channel_intelligence_update(db, channel_id, value)
    if errors or prior is None: raise HTTPException(422, {'errors': errors})
    digest = canonical_json_sha256(value)
    existing = db.scalar(select(ChannelIntelligenceAnalysis).where(ChannelIntelligenceAnalysis.channel_id == channel_id,
        ChannelIntelligenceAnalysis.payload_sha256 == digest))
    if existing is not None: return existing
    videos = {video.tiktok_id: video for video in db.scalars(select(Video).where(Video.channel_id == channel_id))}
    if not set(delta) <= set(videos): raise HTTPException(409, 'Current delta videos no longer exist')
    analysis = ChannelIntelligenceAnalysis(channel_id=channel_id, research_pack_hash=canonical_json_sha256({'update': digest}),
        schema_version=CHANNEL_UPDATE_SCHEMA_VERSION, selection_mode=prior.selection_mode, payload_sha256=digest,
        channel_intelligence=value['channel_intelligence'], semantic_corpus_hash=semantic_corpus_hash(records),
        performance_state_hash=performance_state_hash(records), analysis_contract_version=ANALYSIS_CONTRACT_VERSION)
    db.add(analysis); db.flush()
    delta_items = {item['video_id']: item for item in value['upsert_videos']}
    by_record = {row['video_id']: row for row in records}
    for child, video in _analysis_video_rows(db, prior):
        if video.tiktok_id not in delta_items:
            db.add(ChannelVideoIntelligence(analysis_id=analysis.id, video_id=video.id, intelligence=child.intelligence,
                semantic_source_hash=child.semantic_source_hash))
    for video_id, item in delta_items.items():
        db.add(ChannelVideoIntelligence(analysis_id=analysis.id, video_id=videos[video_id].id, intelligence=item,
            semantic_source_hash=canonical_json_sha256(semantic_video(by_record[video_id]))))
    db.commit()
    return analysis


@router.post('/research/channels/{channel_id}/intelligence/update/import/dry-run')
async def channel_update_import_dry_run(channel_id: uuid.UUID, file: UploadFile = File(...), _auth: None = Depends(require_admin), db: Session = Depends(get_db)):
    try: value = json.loads((await file.read(CHANNEL_INTELLIGENCE_IMPORT_MAX_BYTES + 1)).decode('utf-8', errors='strict'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JSONResponse({'ok': False, 'errors': ['Upload one valid UTF-8 JSON object.']}, status_code=422)
    errors, _prior, _records, delta = dry_run_channel_intelligence_update(db, channel_id, value)
    if errors: return JSONResponse({'ok': False, 'errors': errors}, status_code=422)
    return JSONResponse({'ok': True, 'token': _store_pending_channel_update(PendingChannelIntelligenceUpdate(channel_id, value, time.time())),
                         'videos_new_or_changed': len(delta)})


@router.post('/research/channels/{channel_id}/intelligence/update/import/confirm')
def channel_update_import_confirm(channel_id: uuid.UUID, token: str = Form(...), _auth: None = Depends(require_admin), db: Session = Depends(get_db)):
    pending = _take_pending_channel_update(token, channel_id)
    analysis = import_channel_intelligence_update(db, channel_id, pending.payload)
    return JSONResponse({'ok': True, 'analysis_id': str(analysis.id), 'action': 'merged_immutable_snapshot'})


@router.post('/research/channels/{channel_id}/intelligence/content-pack/prompt', response_class=PlainTextResponse)
def content_pack_prompt_text(channel_id: uuid.UUID, business: str = Form(...), offer: str = Form(...), audience: str = Form(...),
                             goal: str = Form(...), tone: str = Form(...), constraints: str = Form(''), market: str = Form(''),
                             execution: str = Form(''), additional_context: str = Form(''),
                             _auth: None = Depends(require_admin), db: Session = Depends(get_db)):
    data = _channel_or_404(db, channel_id)
    analysis = db.scalar(select(ChannelIntelligenceAnalysis).where(ChannelIntelligenceAnalysis.channel_id == channel_id).order_by(
        ChannelIntelligenceAnalysis.updated_at.desc(), ChannelIntelligenceAnalysis.id.desc()))
    if analysis is None:
        raise HTTPException(409, 'Import Channel Intelligence before creating content')
    # This request only composes a prompt. It intentionally has no database write.
    context = _private_context_from_form(business, offer, audience, goal, tone, constraints, market=market,
                                         execution=execution, additional_context=additional_context)
    saved = _latest_personal_strategy(db, channel_id)
    return PlainTextResponse(content_pack_prompt(data, analysis, context, saved.strategy_json if saved else None))


def _content_pack_analysis_or_404(db: Session, channel_id: uuid.UUID) -> ChannelIntelligenceAnalysis:
    analysis = db.scalar(select(ChannelIntelligenceAnalysis).where(ChannelIntelligenceAnalysis.channel_id == channel_id).order_by(
        ChannelIntelligenceAnalysis.updated_at.desc(), ChannelIntelligenceAnalysis.id.desc()))
    if analysis is None:
        raise HTTPException(409, 'Import Channel Intelligence before importing a Content Pack')
    return analysis


def _analysis_video_ids(db: Session, analysis: ChannelIntelligenceAnalysis) -> set[str]:
    return set(db.scalars(select(Video.tiktok_id).join(ChannelVideoIntelligence, ChannelVideoIntelligence.video_id == Video.id).where(
        ChannelVideoIntelligence.analysis_id == analysis.id
    )))


@router.post('/research/channels/{channel_id}/intelligence/content-pack/import/dry-run')
async def content_pack_import_dry_run(channel_id: uuid.UUID, file: UploadFile = File(...), business: str = Form(...),
                                      offer: str = Form(...), audience: str = Form(...), goal: str = Form(...), tone: str = Form(...),
                                      constraints: str = Form(''), market: str = Form(''), execution: str = Form(''),
                                      additional_context: str = Form(''), _auth: None = Depends(require_admin), db: Session = Depends(get_db)):
    data = _channel_or_404(db, channel_id)
    analysis = _content_pack_analysis_or_404(db, channel_id)
    context = _private_context_from_form(business, offer, audience, goal, tone, constraints, market=market,
                                         execution=execution, additional_context=additional_context)
    raw = await file.read(CHANNEL_INTELLIGENCE_IMPORT_MAX_BYTES + 1)
    if len(raw) > CHANNEL_INTELLIGENCE_IMPORT_MAX_BYTES:
        raise HTTPException(413, 'Content Pack file exceeds 5 MiB')
    try:
        value = json.loads(raw.decode('utf-8', errors='strict'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JSONResponse({'ok': False, 'errors': ['Upload one valid UTF-8 JSON object.']}, status_code=422)
    errors = validate_content_pack(value, channel_id=str(channel_id), username=data['channel'].username,
                                   known_patterns=set(_known_patterns(analysis)), known_video_ids=_analysis_video_ids(db, analysis))
    if errors:
        return JSONResponse({'ok': False, 'errors': errors}, status_code=422)
    digest = canonical_json_sha256(value)
    token = _store_pending_content_pack(PendingContentPackImport(channel_id, analysis.id, value, context, digest, time.time()))
    return JSONResponse({'ok': True, 'token': token, 'summary': {'ideas': len(value['content_ideas']), 'scripts': len(value['scripts'])}})


@router.post('/research/channels/{channel_id}/intelligence/content-pack/import/confirm')
def content_pack_import_confirm(channel_id: uuid.UUID, token: str = Form(...), _auth: None = Depends(require_admin), db: Session = Depends(get_db)):
    pending = _take_pending_content_pack(token, channel_id)
    analysis = db.get(ChannelIntelligenceAnalysis, pending.analysis_id)
    if analysis is None:
        raise HTTPException(409, 'The Channel Intelligence source is no longer available')
    data = _channel_or_404(db, channel_id)
    errors = validate_content_pack(pending.payload, channel_id=str(channel_id), username=data['channel'].username,
                                   known_patterns=set(_known_patterns(analysis)), known_video_ids=_analysis_video_ids(db, analysis))
    if errors:
        raise HTTPException(409, 'Source patterns changed since dry run; upload the Content Pack again')
    existing = db.scalar(select(PrivateContentPack).where(PrivateContentPack.payload_sha256 == pending.payload_sha256))
    if existing is not None:
        return JSONResponse({'ok': True, 'already_imported': True, 'content_pack_id': str(existing.id)})
    pack = PrivateContentPack(channel_id=channel_id, analysis_id=analysis.id, payload_sha256=pending.payload_sha256,
                              private_context=pending.private_context, content_pack=pending.payload)
    db.add(pack)
    db.commit()
    return JSONResponse({'ok': True, 'content_pack_id': str(pack.id), 'action': 'imported'})


def _source_pattern_html(data: dict[str, Any], analysis: ChannelIntelligenceAnalysis, names: list[str]) -> str:
    records = {record['video_id']: record for record in _selection(data, analysis.selection_mode)}
    patterns = _known_patterns(analysis)
    cards = []
    for name in names:
        item = patterns.get(name)
        if item is None:
            continue
        videos = []
        for evidence in item.get('evidence', []):
            for video_id in evidence.get('video_ids', []):
                record = records.get(video_id)
                if record:
                    metric = f"{record.get('views', 0):,} views · outlier {float(record.get('outlier_score') or 0):.1f}"
                    videos.append(f'<li><a href="{_e(record["url"])}" target="_blank" rel="noopener">{_e(_human_video_title(record))}</a><br><span class="muted">{_e(metric)}</span></li>')
        why = (f'<p><b>Por qué importa</b><br>{_e(item.get("why_it_matters"))}</p>'
               if _has_distinct_why(item.get('description'), item.get('why_it_matters')) else '')
        cards.append(f'<div class="card"><b>{_e(name)}</b><p>{_e(item.get("description") or "")}</p>{why}<ul>{"".join(videos) or "<li class=\"muted\">No video evidence.</li>"}</ul></div>')
    return ''.join(cards) or '<p class="muted">No matching source pattern.</p>'


def _content_pack_results(data: dict[str, Any], analysis: ChannelIntelligenceAnalysis, pack: PrivateContentPack) -> str:
    value = pack.content_pack
    strategy = value['strategy']
    def source_button(item: dict[str, Any], token: str) -> str:
        names = item.get('source_patterns', [])
        return f'<button type="button" class="secondary" onclick="document.getElementById(\'{token}\').open=true;document.getElementById(\'{token}\').scrollIntoView({{behavior:\'smooth\'}})">Ver patrón de origen</button><details id="{token}" class="card"><summary>Patrón de origen</summary>{_source_pattern_html(data, analysis, names)}</details>'
    ideas = ''.join(f'<div class="card"><h3>{_e(item["title"])}</h3><p><b>Hook</b><br>{_e(item["hook"])}</p><p><b>Angle</b><br>{_e(item["angle"])}</p><p><b>Objective</b><br>{_e(item["objective"])}</p><p><b>CTA</b><br>{_e(item["cta"])}</p><div class="actions"><button type="button" class="secondary" onclick="navigator.clipboard.writeText({json.dumps(item["hook"] + "\\n\\n" + item["angle"] + "\\n\\nCTA: " + item["cta"])})">Copiar</button>{source_button(item, 'idea-source-' + str(index))}</div></div>' for index, item in enumerate(value['content_ideas']))
    scripts = ''.join(f'<div class="card"><h3>{_e(item["title"])}</h3><p><b>Hook</b><br>{_e(item["hook"])}</p><p><b>Body</b><br>{_e(item["body"])}</p><p><b>CTA</b><br>{_e(item["cta"])}</p><p class="muted">Duration target: {_e(item["duration_target"])}</p><div class="actions"><button type="button" class="secondary" onclick="navigator.clipboard.writeText({json.dumps(item["hook"] + "\\n\\n" + item["body"] + "\\n\\n" + item["cta"])})">Copiar</button>{source_button(item, 'script-source-' + str(index))}</div></div>' for index, item in enumerate(value['scripts']))
    return f'<div class="card"><h3>Tu estrategia de contenido</h3><p><b>Posicionamiento</b><br>{_e(strategy["recommended_positioning"])}</p><p><b>Fórmula de contenido</b><br>{_e(strategy["content_formula"])}</p><p><b>Estrategia de CTA</b><br>{_e(strategy["recommended_cta_strategy"])}</p><p><b>Mezcla recomendada</b><br>{_e(strategy["recommended_content_mix"])}</p></div><h3>Ideas de contenido</h3>{ideas}<h3>Guiones</h3>{scripts}'


def _channel_intelligence_results(data: dict[str, Any], analysis: ChannelIntelligenceAnalysis, db: Session) -> str:
    """Safe embedded result renderer shared by the primary page and legacy detail URL."""
    records = _selection(data, analysis.selection_mode)
    by_tiktok_id = {record['video_id']: record for record in records}
    channel_value = analysis.channel_intelligence
    sections = ''.join(
        f'<h3>{_e(section)}</h3><div class="card"><h4>{_e(item.get("name") or "Insight")}</h4><p>{_e(item.get("description") or "")}</p>{_evidence_html(item.get("evidence", []), by_tiktok_id)}</div>'
        for section, item in _channel_analysis_items(channel_value)
    ) or '<p class="muted">No channel-level patterns were supplied.</p>'
    children = db.execute(select(ChannelVideoIntelligence, Video).join(Video, Video.id == ChannelVideoIntelligence.video_id).where(
        ChannelVideoIntelligence.analysis_id == analysis.id
    ).order_by(Video.published_at.desc(), Video.tiktok_id)).all()
    summary = channel_value.get('summary', channel_value.get('channel_summary', '—'))
    audience = channel_value.get('audience', channel_value.get('audience_profile', '—'))
    caveats = ''.join(f'<li>{_e(item)}</li>' for item in channel_value.get('caveats', [])) or '<li class="muted">None supplied.</li>'
    video_cards = []
    for child, video in children:
        record = by_tiktok_id.get(video.tiktok_id, {'caption': None, 'transcript': None, 'published_at': _iso(video.published_at), 'url': video.url, 'views': None, 'outlier_score': None, 'engagement_rate': None, 'shares': None})
        item = child.intelligence
        insight = item.get('summary') or ('Contenido insuficiente para una conclusión semántica.' if item.get('analysis_status') == 'insufficient_content' else '—')
        metrics = ' · '.join(f'{label}: {_e(record.get(key) if record.get(key) is not None else "—")}' for label, key in (('views', 'views'), ('outlier', 'outlier_score'), ('engagement', 'engagement_rate'), ('shares', 'shares')))
        video_cards.append(f'<details class="card video-card"><summary><b>{_e(_human_video_title(record))}</b></summary><p class="muted">{metrics}</p><p><b>AI insight:</b> {_e(insight)}</p><p><a class="button secondary" href="{_e(record.get("url") or video.url)}" target="_blank" rel="noopener">Abrir TikTok</a></p><details><summary>Detalle técnico</summary><code>{_e(video.tiktok_id)}</code>{_evidence_html(item.get("evidence", []), by_tiktok_id)}</details></details>')
    return f'''<p class="muted">Research Pack <code>{_e(analysis.research_pack_hash[:12])}</code> · {_when(analysis.updated_at)}</p><div class="card"><h3>Resumen</h3><p>{_e(summary)}</p><h3>Audiencia</h3><p>{_e(audience)}</p><h3>Caveats</h3><ul>{caveats}</ul></div>{sections}<h3>Videos</h3>{''.join(video_cards) or '<p class="muted">No video records.</p>'}'''


@router.get('/research/channels/{channel_id}/intelligence/{analysis_id}', response_class=HTMLResponse)
def channel_intelligence_detail(channel_id: uuid.UUID, analysis_id: uuid.UUID, _auth: None = Depends(require_admin), db: Session = Depends(get_db)):
    analysis = db.scalar(select(ChannelIntelligenceAnalysis).where(
        ChannelIntelligenceAnalysis.id == analysis_id, ChannelIntelligenceAnalysis.channel_id == channel_id
    ))
    if analysis is None:
        raise HTTPException(404, 'Unknown Channel Intelligence analysis')
    data = _channel_or_404(db, channel_id)
    channel = data['channel']
    return _layout('Channel Intelligence detail', f'''<p><a href="/admin/research/channels/{channel_id}/intelligence">← Inteligencia</a></p>{_channel_context(channel.username, channel.nickname)}<h2>Inteligencia del canal</h2>{_channel_intelligence_results(data, analysis, db)}''')


PROMPT_PRESETS = {
    'analyze': ('Analyze channel', 'Produce: Executive summary; Top patterns; Hooks; Themes; Narrative structures; Emotions; CTA patterns; Differences between winners and normal content; Opportunities; Evidence/examples.'),
    'viral': ('Find viral patterns', 'Identify repeated high-performing patterns, then contrast winners with normal content using performance and video-level evidence.'),
    'themes': ('Find themes and angles', 'Map recurring themes, audience angles, tensions, and unexplored adjacent opportunities supported by the corpus.'),
    'ideas': ('Generate content ideas', 'Generate original content ideas from the observed patterns. Distinguish inspiration and pattern extraction from copying text, scenes, or scripts.'),
    'scripts': ('Generate scripts', 'Generate original scripts informed by the patterns. Do not copy transcript wording, distinctive phrasing, or a creator\'s specific expression.'),
}


def generated_prompt(channel: Channel, preset: str, business: str, goal: str, instructions: str) -> str:
    if preset not in PROMPT_PRESETS:
        raise HTTPException(422, 'Unknown prompt preset')
    title, task = PROMPT_PRESETS[preset]
    optional = '\n'.join(part for part in (
        f'Business/context: {business.strip()}' if business.strip() else '',
        f'Goal: {goal.strip()}' if goal.strip() else '',
        f'Additional instructions: {instructions.strip()}' if instructions.strip() else '',
    ) if part)
    return f'''# {title}: @{channel.username}\n\nUse the supplied Kurukin Research Pack as the evidence base. {task}\n\nRules:\n- Cite `video_id` for important conclusions and examples.\n- Prioritize outlier_score, views, and engagement in context; do not treat any one metric as universal proof.\n- Do not invent examples, metrics, transcripts, or creator intent.\n- Creator claims are not verified facts.\n- Transcripts may contain recognition or manual errors.\n- Distinguish repeated patterns from isolated examples.\n- Treat outlier_score as relative performance, not viral probability.\n{'\n' + optional if optional else ''}\n\nReturn a clear, evidence-led response.\n'''


@router.get('/research/channels/{channel_id}/prompt', response_class=HTMLResponse)
def prompt_page(channel_id: uuid.UUID, _auth: None = Depends(require_admin), db: Session = Depends(get_db)):
    channel = _channel_or_404(db, channel_id)['channel']
    options = ''.join(f'<option value="{key}">{_e(value[0])}</option>' for key, value in PROMPT_PRESETS.items())
    content = f'''<p><a href="/admin/research/channels/{channel_id}">← @{_e(channel.username)}</a></p><h2><span class="badge legacy">LEGACY PROMPT GENERATOR</span></h2><p class="muted">Structured Channel Intelligence is the current primary workflow. This free-form generator remains available for advanced use.</p><div class="actions"><a class="button" href="/admin/research/channels/{channel_id}/intelligence">Ir a Structured Channel Intelligence</a></div><details class="card"><summary><b>Opciones avanzadas · legacy</b></summary><form method="post" action="/admin/research/channels/{channel_id}/prompt"><p><label>Preset <select name="preset">{options}</select></label></p><p><label>Business/context<br><input name="business" maxlength="4000"></label></p><p><label>Goal<br><input name="goal" maxlength="4000"></label></p><p><label>Additional instructions<br><textarea name="instructions" style="min-height:120px" maxlength="8000"></textarea></label></p><button class="secondary">Generate legacy prompt</button></form></details>'''
    return _layout('Legacy Prompt Generator', content)


def _prompt_result_page(channel_id: uuid.UUID, prompt: str) -> HTMLResponse:
    escaped = _e(prompt)
    content = f'''<p><a href="/admin/research/channels/{channel_id}/prompt">← Generate another</a></p><h2>Generated prompt</h2><textarea id="prompt" readonly>{escaped}</textarea><div class="actions"><button type="button" class="secondary" onclick="navigator.clipboard.writeText(document.getElementById('prompt').value)">Copy</button><form method="post" action="/admin/research/channels/{channel_id}/prompt/download"><input type="hidden" name="prompt" value="{escaped}"><button>Download .md</button></form></div>'''
    return _layout('Generated prompt', content)


@router.post('/research/channels/{channel_id}/prompt', response_class=HTMLResponse)
def prompt_generate(channel_id: uuid.UUID, preset: str = Form(...), business: str = Form(''), goal: str = Form(''), instructions: str = Form(''), _auth: None = Depends(require_admin), db: Session = Depends(get_db)):
    channel = _channel_or_404(db, channel_id)['channel']
    return _prompt_result_page(channel_id, generated_prompt(channel, preset, business, goal, instructions))


@router.post('/research/channels/{channel_id}/prompt/download')
def prompt_download(channel_id: uuid.UUID, prompt: str = Form(...), _auth: None = Depends(require_admin), db: Session = Depends(get_db)):
    # The POST form is an intentional Basic-auth mutation-safe transport; it
    # does not create server state and leaves the supplied prompt untouched.
    if len(prompt) > 24_000:
        raise HTTPException(422, 'Prompt is too large')
    return PlainTextResponse(prompt, headers={'Content-Disposition': 'attachment; filename="kurukin-research-prompt.md"'})


HISTORICAL_IMPORT_PROMPT = '''Transform the supplied `metadata.json` plus `transcripciones.md` into `kurukin-import.jsonl`.

Output valid JSONL only: one JSON object per line, with `video_id`, `transcript`, and when available `author`, `language`, and `source` (`manual_ai_import_v1`).

Rules:
- Match transcript material to metadata only by exact TikTok video ID.
- Never invent IDs, missing transcript text, captions as transcripts, or a transcript-to-video association.
- Do not substitute captions for transcript text.
- Preserve the original meaning; make only conservative corrections of obvious ASR errors.
- Keep the original language.
- Do not output duplicate video IDs.
- Do not add Markdown, explanations, fences, or any text outside JSONL.
'''


@router.get('/research/historical-import-prompt', response_class=HTMLResponse)
def historical_import_prompt(_auth: None = Depends(require_admin)):
    content = f'''<p><a href="/admin/research">← Global corpus</a></p><h2>Historical normalization prompt</h2><p class="muted">Use this with ChatGPT, Gemini, or Claude alongside the old files. Kurukin only receives the resulting canonical JSONL.</p><textarea id="historical" readonly>{_e(HISTORICAL_IMPORT_PROMPT)}</textarea><div class="actions"><button type="button" class="secondary" onclick="navigator.clipboard.writeText(document.getElementById('historical').value)">Copy historical import prompt</button></div>'''
    return _layout('Historical import prompt', content)
