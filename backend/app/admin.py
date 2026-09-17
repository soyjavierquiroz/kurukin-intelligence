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
    canonical_json_sha256, validate_channel_analysis,
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
from .strategist import (PLAYBOOK_SCHEMA_VERSION, PERSONAL_STRATEGY_SCHEMA_VERSION, configured_personal_generator,
    configured_playbook_generator, get_or_create_playbook, personal_strategy_input)
from .ranking import priority_view_cutoff, rank_snapshots
from .services import eligibility


security = HTTPBasic(auto_error=False)
router = APIRouter(prefix='/admin', dependencies=[])

IMPORT_MAX_BYTES = 5 * 1024 * 1024
IMPORT_MAX_LINES = 10_000
IMPORT_MAX_TRANSCRIPT_CHARS = 100_000
TRANSCRIPT_PART_CHARS = 450_000
PENDING_IMPORT_TTL_SECONDS = 15 * 60


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


def _layout(title: str, content: str) -> HTMLResponse:
    return HTMLResponse(f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(title)} · Kurukin</title><style>
:root{{color-scheme:light;font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#172033;background:#f5f7fa}}
body{{margin:0}}main{{max-width:1280px;margin:auto;padding:28px 20px 48px}}header{{display:flex;gap:18px;align-items:baseline;justify-content:space-between;margin-bottom:24px}}h1{{font-size:1.55rem;margin:0}}h2{{font-size:1.1rem;margin:24px 0 10px}}a{{color:#1659b7;text-decoration:none}}a:hover{{text-decoration:underline}}.muted{{color:#64748b}}.card{{background:#fff;border:1px solid #dce3eb;border-radius:10px;padding:18px;margin:14px 0}}.table-wrap{{overflow-x:auto}}table{{border-collapse:collapse;width:100%;font-size:.9rem}}th,td{{text-align:left;padding:10px 8px;border-bottom:1px solid #e7edf3;vertical-align:top}}th{{white-space:nowrap;color:#526174}}.badge{{display:inline-block;padding:3px 7px;border-radius:999px;font-size:.72rem;font-weight:700;letter-spacing:.02em}}.NO_CORPUS{{background:#fee2e2;color:#991b1b}}.PARTIAL{{background:#fef3c7;color:#92400e}}.PRIORITY_READY{{background:#dcfce7;color:#166534}}.ok{{background:#dcfce7;color:#166534}}.warn{{background:#fef3c7;color:#92400e}}.bad{{background:#fee2e2;color:#991b1b}}.legacy{{background:#f1f5f9;color:#475569}}button,.button{{font:inherit;background:#1659b7;color:#fff;border:0;border-radius:7px;padding:10px 14px;cursor:pointer;display:inline-block;min-height:44px}}button.secondary,.button.secondary{{background:#e7edf3;color:#172033}}input,select,textarea{{font:inherit;border:1px solid #b9c6d4;border-radius:6px;padding:10px;box-sizing:border-box;max-width:100%}}textarea{{width:100%;min-height:340px;white-space:pre-wrap}}form.inline{{display:flex;gap:8px;align-items:center;flex-wrap:wrap}}.actions{{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0}}.stat-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:10px}}.stat{{background:#f8fafc;border:1px solid #e7edf3;border-radius:7px;padding:10px}}.stat b{{display:block;font-size:1.2rem}}code{{font-size:.85em}}.step{{padding:0;overflow:hidden}}.step>summary{{cursor:pointer;list-style:none;padding:17px;font-size:1.05rem;min-height:24px}}.step>summary::-webkit-details-marker{{display:none}}.step-body{{padding:0 17px 17px}}.step-done{{color:#166534}}.dropzone{{display:block;border:2px dashed #8ba3bd;border-radius:9px;padding:28px 16px;text-align:center;background:#f8fafc;cursor:pointer}}.dropzone input{{display:none}}.error-box{{background:#fee2e2;color:#7f1d1d;padding:12px;border-radius:7px}}.result-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(135px,1fr));gap:8px}}.video-card{{border-left:4px solid #1659b7}}@media(max-width:650px){{main{{padding:18px 12px}}header{{display:block}}.actions{{display:grid}}.actions>*{{width:100%;text-align:center}}}}
</style></head><body><main><header><h1><a href="/admin/research">Kurukin Internal Research</a></h1><span class="muted">INTERNAL RESEARCH BACKOFFICE v1.6 · SCI v1 · STRATEGIST v1 · CREATE v1 · Build {_e(_build_marker())}</span></header>{content}</main></body></html>''')


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


def _status_badge(status: str) -> str:
    return f'<span class="badge {status}">{_e(status)}</span>'


@router.get('/research', response_class=HTMLResponse)
def research_index(q: str = '', page: int = 1,
                   _auth: None = Depends(require_admin), db: Session = Depends(get_db)):
    page = max(1, page)
    ids, total = _channels_page(db, q.strip(), page)
    groups = _corpus_data(db, ids)
    summaries = [_channel_summary(groups[channel_id]) for channel_id in ids if channel_id in groups]
    table_rows = ''.join(
        f'<tr><td><a href="/admin/research/channels/{row["channel"].id}">@{_e(row["channel"].username)}</a></td>'
        f'<td>{_e(row["channel"].nickname)}</td><td>{row["total_videos"]}</td><td>{row["priority_count"]}</td>'
        f'<td>{row["transcripts_total"]}</td><td>{row["priority_transcripts"]}</td><td>{row["missing_priority"]}</td>'
        f'<td>{_when(row["latest_seen"])}</td><td>{_status_badge(row["status"])}</td></tr>'
        for row in summaries
    ) or '<tr><td colspan="9" class="muted">No global TikTok channels found.</td></tr>'
    next_link = '' if page * 50 >= total else f'<a class="button secondary" href="?q={_e(q)}&page={page + 1}">Next page</a>'
    content = f'''<p class="muted">Global corpus status. <code>PRIORITY_READY</code> means every video in the current priority pool has a global transcript; it does not mean every discovered video is transcribed.</p>
<form class="inline" method="get"><label>Search username <input name="q" value="{_e(q)}" placeholder="creator"></label><button>Search</button></form>
<div class="card table-wrap"><table><thead><tr><th>Username</th><th>Nickname</th><th>Global videos</th><th>Priority pool</th><th>Transcripts</th><th>Priority transcripts</th><th>Missing priority</th><th>Latest snapshot / seen</th><th>Corpus status</th></tr></thead><tbody>{table_rows}</tbody></table></div>
<p class="muted">{total} channel(s), page {page}.</p>{next_link}'''
    return _layout('Research corpus', content)


def _channel_or_404(db: Session, channel_id: uuid.UUID) -> dict[str, Any]:
    summary = _summary_for_channel(db, channel_id)
    if summary is None:
        raise HTTPException(404, 'Unknown global channel')
    return summary


@router.get('/research/channels/{channel_id}', response_class=HTMLResponse)
def research_channel(channel_id: uuid.UUID, _auth: None = Depends(require_admin), db: Session = Depends(get_db)):
    data = _channel_or_404(db, channel_id)
    channel = data['channel']
    priority_rows = []
    for video, snapshot, transcript, _reason in sorted(
        data['priority'], key=lambda item: (-data['rates'][item[0].id]['outlier_score'], -item[1].views)
    ):
        status = '<span class="badge ok">RESOLVED</span>' if transcript else '<span class="badge warn">MISSING</span>'
        priority_rows.append(
            f'<tr><td>{_e(video.tiktok_id)}</td><td>{_when(video.published_at)}</td><td>{snapshot.views:,}</td>'
            f'<td>{float(data["rates"][video.id]["engagement_rate"]):.3%}</td>'
            f'<td>{float(data["rates"][video.id]["outlier_score"]):.2f}</td><td>{status}</td>'
            f'<td>{_e(transcript.model if transcript else "—")}</td></tr>'
        )
    priority_table = ''.join(priority_rows) or '<tr><td colspan="7" class="muted">No eligible priority videos under current selection semantics.</td></tr>'
    content = f'''<p><a href="/admin/research">← Global corpus</a></p><h2>@{_e(channel.username)}</h2><p class="muted">{_e(channel.nickname)} · Stable TikTok author ID: <code>{_e(channel.tiktok_user_id or 'not available')}</code> · {_status_badge(data['status'])}</p>
<div class="stat-grid"><div class="stat"><b>{data['total_videos']}</b>total videos</div><div class="stat"><b>{data['priority_count']}</b>priority pool</div><div class="stat"><b>{data['transcripts_total']}</b>transcripts total</div><div class="stat"><b>{data['priority_transcripts']}</b>priority transcripts</div><div class="stat"><b>{data['missing_priority']}</b>missing priority</div><div class="stat"><b>{_when(data['latest_seen'])}</b>latest metrics snapshot</div></div>
<div class="card"><h2>Structured Channel Intelligence</h2><ol><li><b>Preparar investigación</b></li><li><b>Analizar con IA</b></li><li><b>Importar inteligencia</b></li><li><b>Resultados</b></li></ol><div class="actions"><a class="button" href="/admin/research/channels/{channel.id}/intelligence">Structured Channel Intelligence</a></div></div>
<div class="actions"><a class="button secondary" href="/admin/research/channels/{channel.id}/import">Import historical transcripts</a><a class="button secondary" href="/admin/research/channels/{channel.id}/export">Export Research Pack</a><a class="button secondary" href="/admin/research/channels/{channel.id}/prompt">Generador de prompt legacy</a><a class="button secondary" href="/admin/research/historical-import-prompt">Copy historical import prompt</a></div>
<h2>Priority videos</h2><div class="card table-wrap"><table><thead><tr><th>Video ID</th><th>Published</th><th>Views</th><th>Engagement rate</th><th>Outlier score</th><th>Transcript</th><th>Source / model</th></tr></thead><tbody>{priority_table}</tbody></table></div>'''
    return _layout(f'@{channel.username}', content)


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
    schema = json.dumps(CHANNEL_ANALYSIS_JSON_SCHEMA, ensure_ascii=False, indent=2)
    return f'''# Kurukin Structured Channel Intelligence v1

YOUR TASK IS NOT TO WRITE A REPORT IN CHAT.

YOUR TASK IS TO CREATE A FILE.

Required output filename: `kurukin-channel-analysis.json`

The file must be valid JSON matching EXACTLY the JSON Schema contained below. Do NOT write Markdown. Do NOT paste the complete JSON as the normal chat response. Do NOT rename schema fields.

The root structure MUST use exactly: `schema`, `prompt_version`, `processor`, `research_pack`, `videos`, `channel_intelligence`.

Explicitly prohibited aliases: `video_intelligence`, `video_analysis`, `items`, `results`.

Set `schema` to `{CHANNEL_INTELLIGENCE_SCHEMA_VERSION}`, `prompt_version` to `{CHANNEL_INTELLIGENCE_PROMPT_VERSION}`, and `research_pack.hash` exactly to `{pack_hash}`. Set `research_pack.channel_username` and `research_pack.selection_mode` from this pack, and `research_pack.video_count` to {len(records)}.

If the Research Pack contains N videos, `videos[]` MUST contain exactly N video objects. Every exported `video_id` appears exactly once: no missing videos, unknown videos, or duplicates. Every `evidence.video_ids` reference must be an exact `video_id` from this Research Pack.

For a low-information video use `analysis_status = insufficient_content`; do not fabricate semantic conclusions. Canonical metrics belong to Kurukin. Per-video intelligence MUST NOT return or restate `views`, `likes`, `comments`, `shares`, `favorites`, `engagement_rate`, or `outlier_score`. Do NOT create `performance_interpretation` or any field that duplicates metrics.

Every meaningful channel-level insight must reference evidence video IDs. Creator claims are not externally verified facts. Transcripts may contain ASR/manual errors.

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

Create and attach/downloadable file: `kurukin-channel-analysis.json`

Do not paste the complete JSON into the conversation.

If the AI environment truly cannot create a downloadable file, respond exactly:
FILE_GENERATION_UNAVAILABLE

JSON Schema (authoritative):

{schema}
'''


def dry_run_channel_intelligence_import(db: Session, channel_id: uuid.UUID, raw: bytes) -> tuple[dict[str, Any] | None, list[str], str | None, str | None]:
    """Parse and validate an import without writing any intelligence records."""
    if len(raw) > CHANNEL_INTELLIGENCE_IMPORT_MAX_BYTES:
        raise HTTPException(413, 'Channel Intelligence file exceeds 5 MiB')
    try:
        value = json.loads(raw.decode('utf-8', errors='strict'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, ['Upload one valid UTF-8 JSON object.'], None, None
    if not isinstance(value, dict):
        return None, ['The upload must be one JSON object.'], None, None
    data = _channel_or_404(db, channel_id)
    research_pack = value.get('research_pack')
    pack_hash = research_pack.get('hash') if isinstance(research_pack, dict) else None
    resolved = _research_pack_by_hash(data, pack_hash) if isinstance(pack_hash, str) else None
    if resolved is None:
        return value, ['research_pack_hash does not match a current non-empty Research Pack for this channel. Re-export, re-analyze, and upload again.'], None, None
    mode, records = resolved
    if isinstance(research_pack, dict):
        if research_pack.get('channel_username') != data['channel'].username:
            return value, [f"research_pack.channel_username must equal @{data['channel'].username}."], None, None
        if research_pack.get('selection_mode') != mode:
            return value, ['research_pack.selection_mode does not match the Research Pack hash.'], None, None
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
    return value, errors, mode, status


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
    ('winning_patterns', 'Top winning mechanisms'), ('hooks', 'Top hook structures'),
    ('pains', 'Top pain/desire patterns'), ('narratives', 'Top narrative structures'),
    ('ctas', 'Top CTA patterns'), ('repetition_clusters', 'Top repetition strategies'),
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
            value = f'{views:,} views' if isinstance(views, int) else 'Evidence'
            if outlier is not None:
                value += f' · outlier {float(outlier):.1f}'
            badges.append(f'<span class="badge legacy">{_e(value)}</span>')
    return ' '.join(badges[:4]) or '<span class="muted">Evidence cited in imported intelligence.</span>'


def _playbook_items(items: list[dict[str, Any]], records: dict[str, dict[str, Any]]) -> str:
    if not items:
        return '<p class="muted">No imported pattern in this category.</p>'
    return ''.join(
        f'<div class="card"><b>{_e(item.get("name") or "Pattern")}</b><p>{_e(item.get("description") or "")}</p>'
        f'<p>{_evidence_badges(item.get("evidence", []), records)}</p></div>'
        for item in items[:5] if isinstance(item, dict)
    )


def _actionable_playbook(data: dict[str, Any], analysis: ChannelIntelligenceAnalysis) -> str:
    records = {record['video_id']: record for record in _selection(data, analysis.selection_mode)}
    value = analysis.channel_intelligence
    sections = ''.join(
        f'<h3>{label}</h3>{_playbook_items(value.get(key, []), records)}'
        for key, label in _PLAYBOOK_SECTIONS
    )
    winning = value.get('winning_patterns', [])
    formula = ' → '.join(str(item.get('name')) for item in winning[:3] if isinstance(item, dict) and item.get('name')) or 'Use the strongest repeated pattern, then adapt it to your offer.'
    avoid = value.get('opportunities', [])
    avoid_text = '; '.join(str(item.get('name')) for item in avoid[:3] if isinstance(item, dict) and item.get('name')) or 'Avoid unsupported claims and weakly evidenced variations.'
    return f'''<section id="what-works"><h2>WHAT WORKS</h2>{sections}</section>
<section id="why-it-works"><h2>WHY IT WORKS</h2><div class="card"><p>{_e(value.get('summary') or 'Imported evidence identifies repeated mechanisms across this channel.')}</p><p><b>DO MORE OF THIS</b><br>{_e(formula)}</p><p><b>AVOID / LESS USEFUL</b><br>{_e(avoid_text)}</p><p><b>DOMINANT CONTENT FORMULA</b><br>{_e(formula)}</p></div></section>
<section id="what-next"><h2>WHAT YOU SHOULD DO NEXT</h2><p>Adapt these proven mechanisms to your own business, offer and audience—without copying the competitor’s identity, wording, claims or creative expression.</p><div class="actions"><button type="button" id="create-from-patterns">Crear contenido basado en estos patrones</button></div></section>'''


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
                               goal: str = Form(...), tone: str = Form(...), constraints: str = Form(''),
                               _auth: None = Depends(require_admin), db: Session = Depends(get_db)):
    analysis = db.scalar(select(ChannelIntelligenceAnalysis).where(ChannelIntelligenceAnalysis.channel_id == channel_id).order_by(
        ChannelIntelligenceAnalysis.created_at.desc(), ChannelIntelligenceAnalysis.id.desc()))
    if analysis is None: raise HTTPException(409, 'Import Channel Intelligence first')
    playbook = _latest_playbook(db, analysis)
    if playbook is None: raise HTTPException(409, 'Generate the global Playbook first')
    context = _private_context_from_form(business, offer, audience, goal, tone, constraints)
    payload = personal_strategy_input(playbook.payload_json, context)
    digest = canonical_json_sha256(payload)
    existing = db.scalar(select(PrivatePersonalStrategy).where(PrivatePersonalStrategy.payload_sha256 == digest))
    if existing is not None: return JSONResponse({'ok': True, 'reused': True, 'strategy': existing.strategy_json})
    try:
        result = configured_personal_generator(get_settings())(payload)
    except Exception:
        return JSONResponse({'ok': False, 'error': 'No pudimos generar tu estrategia personal en este momento.'}, status_code=503)
    if result.get('schema') != PERSONAL_STRATEGY_SCHEMA_VERSION:
        return JSONResponse({'ok': False, 'error': 'No pudimos generar tu estrategia personal en este momento.'}, status_code=503)
    db.add(PrivatePersonalStrategy(channel_id=channel_id, playbook_id=playbook.id, payload_sha256=digest,
        private_context=context, strategy_json=result)); db.commit()
    return JSONResponse({'ok': True, 'reused': False, 'strategy': result})


def _private_context_from_form(business: str, offer: str, audience: str, goal: str, tone: str, constraints: str) -> dict[str, str]:
    values = {'business': business, 'offer': offer, 'audience': audience, 'goal': goal, 'tone': tone, 'constraints': constraints}
    required = ('business', 'offer', 'audience', 'goal', 'tone')
    errors = [f'{field} is required' for field in required if not values[field].strip()]
    errors.extend(f'{field} is too long' for field, value in values.items() if len(value) > 4000)
    if errors:
        raise HTTPException(422, {'errors': errors})
    return {field: value.strip() for field, value in values.items()}


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


@router.get('/research/channels/{channel_id}/intelligence', response_class=HTMLResponse)
def channel_intelligence_page(channel_id: uuid.UUID, _auth: None = Depends(require_admin), db: Session = Depends(get_db)):
    data = _channel_or_404(db, channel_id)
    analyses = list(db.scalars(select(ChannelIntelligenceAnalysis).where(
        ChannelIntelligenceAnalysis.channel_id == channel_id
    ).order_by(ChannelIntelligenceAnalysis.updated_at.desc(), ChannelIntelligenceAnalysis.id.desc())))
    latest = analyses[0] if analyses else None
    if latest is None:
        content = f'''<p><a href="/admin/research/channels/{channel_id}">← @{_e(data['channel'].username)}</a></p><h2>Structured Channel Intelligence</h2><div class="card"><span class="badge warn">NO INTELLIGENCE</span><p>Este canal todavía no tiene inteligencia estructurada válida.</p><div class="actions"><a class="button" href="/admin/research/channels/{channel_id}/prompt">Preparar análisis estructurado</a></div></div>'''
        return _layout('Channel Intelligence', content)
    state, state_records, delta_ids = _knowledge_state(db, data, latest)
    status_text = {
        'FRESH': '✓ Inteligencia actualizada',
        'PERFORMANCE_CHANGED': 'Las métricas cambiaron; la inteligencia de los videos sigue vigente',
        'SEMANTIC_DELTA': f'{len(delta_ids)} videos nuevos o modificados por analizar',
        'CONTRACT_STALE': 'Nueva versión de análisis disponible',
    }.get(state['state'], 'Inteligencia pendiente')
    status_action = ('<a class="button" href="/admin/research/channels/%s/intelligence/update.zip">Descargar Incremental Intelligence Update Pack</a>' % channel_id
                     if state['state'] == 'SEMANTIC_DELTA' else
                     '<a class="button secondary" href="/admin/research/channels/%s/intelligence/prompt">Forzar nuevo análisis</a>' % channel_id)
    playbook = _latest_playbook(db, latest)
    records_by_id = {record['video_id']: record for record in state_records}
    playbook_block = (_strategic_playbook_html(playbook, records_by_id) if playbook else
        '<section id="strategic-playbook"><h2>EL PLAYBOOK DEL CANAL</h2><div class="card"><p>Genera un Playbook estratégico desde inteligencia estructurada y evidencia pública compacta.</p><button type="button" id="generate-playbook">Generar Playbook</button><p id="playbook-status" class="muted"></p></div></section>')
    packs = list(db.scalars(select(PrivateContentPack).where(PrivateContentPack.channel_id == channel_id).order_by(PrivateContentPack.updated_at.desc())))
    latest_pack = packs[0] if packs else None
    pack_results = _content_pack_results(data, latest, latest_pack) if latest_pack else '<p class="muted">Aún no has importado contenido privado.</p>'
    content = f'''<p><a href="/admin/research/channels/{channel_id}">← @{_e(data['channel'].username)}</a></p><h2>Channel Intelligence</h2><div class="card"><span class="badge {'ok' if state['state'] == 'FRESH' else 'warn'}">{_e(state['state'])}</span><p>{_e(status_text)}</p><div class="actions"><a class="button secondary" href="#strategic-playbook">Ver Playbook</a><a class="button" href="#create-flow">Adaptar a mi negocio</a>{status_action}</div></div>{playbook_block}{_actionable_playbook(data, latest)}
<details class="card"><summary><b>Ver análisis completo</b></summary><div class="step-body">{_channel_intelligence_results(data, latest, db)}</div></details>
<details class="card step" id="create-flow"><summary><b>CREATE v1 · Crea desde estos patrones</b></summary><div class="step-body"><p class="muted">Este contexto y el contenido generado son privados. Nunca se añaden a Channel Intelligence global.</p><form id="private-context"><label>¿Qué vendes?<br><textarea name="business" required maxlength="4000" style="min-height:80px"></textarea></label><label>Offer / price (optional)<br><textarea name="offer" required maxlength="4000" style="min-height:80px"></textarea></label><label>¿A quién?<br><textarea name="audience" required maxlength="4000" style="min-height:80px"></textarea></label><label>¿Qué resultado quieres lograr?<br><textarea name="goal" required maxlength="4000" style="min-height:80px"></textarea></label><label>Tone / style<br><input name="tone" required maxlength="4000"></label><label>Optional constraints<br><textarea name="constraints" maxlength="4000" style="min-height:80px"></textarea></label><div class="actions"><button id="generate-personal-strategy" type="button">Ver mi estrategia primero</button><button id="open-hormozi" type="submit">Crear ideas y guiones con Hormozi</button></div></form><pre id="personal-strategy-result" class="card" hidden></pre><p id="create-status" class="muted"></p><ol><li>Pega las instrucciones en Alex Hormozi — $100M.</li><li>Descarga <code>kurukin-content-pack.json</code>.</li><li>Súbelo abajo para validar y confirmar.</li></ol><form id="content-pack-upload"><label class="dropzone">Sube <b>kurukin-content-pack.json</b><br><span class="button secondary">Seleccionar archivo</span><input id="content-pack-file" type="file" accept=".json,application/json"></label></form><div id="content-pack-dry-run" aria-live="polite"></div></div></details>
<section id="content-plan"><h2>YOUR CONTENT PLAN</h2><div id="content-pack-results">{pack_results}</div></section>
<script>(function(){{const base='/admin/research/channels/{channel_id}/intelligence', form=document.getElementById('private-context'), create=document.getElementById('create-flow'), generate=document.getElementById('generate-playbook'), personal=document.getElementById('generate-personal-strategy');if(generate)generate.onclick=async()=>{{const r=await fetch(base+'/playbook/generate',{{method:'POST'}}), x=await r.json();if(x.ok)location.reload();else document.getElementById('playbook-status').textContent=x.error||'No pudimos generar el Playbook en este momento.'}};document.getElementById('create-from-patterns').onclick=()=>{{create.open=true;create.scrollIntoView({{behavior:'smooth',block:'start'}})}};function contextData(){{return new FormData(form)}}if(personal)personal.onclick=async()=>{{const r=await fetch(base+'/personal-strategy',{{method:'POST',body:contextData()}}),x=await r.json();if(!x.ok){{document.getElementById('create-status').textContent=x.error||'Completa los campos privados requeridos.';return}}const box=document.getElementById('personal-strategy-result');box.hidden=false;box.textContent=JSON.stringify(x.strategy,null,2)}};form.addEventListener('submit',async e=>{{e.preventDefault();const r=await fetch(base+'/content-pack/prompt',{{method:'POST',body:contextData()}});if(!r.ok){{document.getElementById('create-status').textContent='Completa los campos privados requeridos.';return}}await navigator.clipboard.writeText(await r.text());window.open('https://chatgpt.com/g/g-68a6de0c7ec48191876f8297e467fc7c-alex-hormozi-100m','_blank','noopener');document.getElementById('create-status').textContent='Instrucciones copiadas. Alex Hormozi GPT se abrió en una nueva pestaña.'}});async function dryRun(file){{const body=contextData();body.append('file',file,file.name);const r=await fetch(base+'/content-pack/import/dry-run',{{method:'POST',body}});const x=await r.json(), box=document.getElementById('content-pack-dry-run');if(!x.ok){{box.innerHTML='<div class="error-box"><b>No se puede importar.</b><ul>'+x.errors.map(escapeHtml).map(v=>'<li>'+v+'</li>').join('')+'</ul></div>';return}}box.innerHTML='<div class="card"><h3>Dry Run</h3><p>✓ Schema · ✓ source channel · ✓ patterns · ✓ evidence IDs · ✓ required fields · ✓ duplicates</p><button id="confirm-content-pack">Confirmar Content Pack</button></div>';document.getElementById('confirm-content-pack').onclick=async()=>{{const body=new FormData();body.append('token',x.token);const confirmed=await fetch(base+'/content-pack/import/confirm',{{method:'POST',body}});const result=await confirmed.json();if(result.ok)location.reload()}}}}function escapeHtml(v){{const d=document.createElement('div');d.textContent=v;return d.innerHTML}}document.getElementById('content-pack-file').addEventListener('change',e=>{{if(e.target.files[0])dryRun(e.target.files[0])}})}})();</script>'''
    return _layout('Channel Intelligence', content)


@router.get('/research/channels/{channel_id}/intelligence/prompt', response_class=HTMLResponse)
def channel_intelligence_prompt_page(channel_id: uuid.UUID, mode: str = 'all', _auth: None = Depends(require_admin), db: Session = Depends(get_db)):
    data = _channel_or_404(db, channel_id)
    prompt = channel_intelligence_prompt(data, mode)
    content = f'''<p><a href="/admin/research/channels/{channel_id}/intelligence">← Channel Intelligence</a></p><h2>Contractual external AI prompt</h2><p class="muted">Use it with the matching downloaded Research Pack. The hash binds the result to its evidence set.</p><textarea id="channel-prompt" readonly>{_e(prompt)}</textarea><div class="actions"><button type="button" class="secondary" onclick="navigator.clipboard.writeText(document.getElementById('channel-prompt').value)">Copy</button><a class="button" href="/admin/research/channels/{channel_id}/export.zip?mode={_e(mode)}">Download matching Research Pack</a></div>'''
    return _layout('Channel Intelligence prompt', content)


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
    value, errors, mode, status = dry_run_channel_intelligence_import(
        db, channel_id, await file.read(CHANNEL_INTELLIGENCE_IMPORT_MAX_BYTES + 1)
    )
    if value is None or errors:
        return JSONResponse({'ok': False, 'errors': errors or ['Upload one valid JSON object.']}, status_code=422)
    assert mode is not None and status is not None
    token = _store_pending_channel_intelligence(PendingChannelIntelligenceImport(
        channel_id, value, mode, canonical_json_sha256(value), time.time()))
    return JSONResponse({'ok': True, 'token': token, 'status': status, 'summary': _dry_run_summary(value, data, _selection(data, mode))})


@router.post('/research/channels/{channel_id}/intelligence/import/confirm')
def channel_intelligence_import_confirm(channel_id: uuid.UUID, token: str = Form(...), _auth: None = Depends(require_admin), db: Session = Depends(get_db)):
    pending = _take_pending_channel_intelligence(token, channel_id)
    data = _channel_or_404(db, channel_id)
    # Re-validate the live corpus: a transcript/import change between preview
    # and confirmation must not silently attach analysis to different evidence.
    _value, errors, mode, _status = dry_run_channel_intelligence_import(
        db, channel_id, json.dumps(pending.payload, ensure_ascii=False).encode('utf-8'))
    if errors or mode != pending.selection_mode:
        raise HTTPException(409, 'Research Pack changed since dry run; export and analyze it again')
    pack_hash = pending.payload['research_pack']['hash']
    analysis = db.scalar(select(ChannelIntelligenceAnalysis).where(
        ChannelIntelligenceAnalysis.channel_id == channel_id,
        ChannelIntelligenceAnalysis.research_pack_hash == pack_hash,
        ChannelIntelligenceAnalysis.schema_version == CHANNEL_INTELLIGENCE_SCHEMA_VERSION,
    ).with_for_update())
    if analysis is not None and analysis.payload_sha256 == pending.payload_sha256:
        return JSONResponse({'ok': True, 'already_imported': True, 'analysis_id': str(analysis.id)})
    changed = analysis is not None
    if analysis is None:
        analysis = ChannelIntelligenceAnalysis(channel_id=channel_id, research_pack_hash=pack_hash,
            schema_version=CHANNEL_INTELLIGENCE_SCHEMA_VERSION, selection_mode=mode,
            payload_sha256=pending.payload_sha256, channel_intelligence=pending.payload['channel_intelligence'],
            semantic_corpus_hash=semantic_corpus_hash(_selection(data, mode)),
            performance_state_hash=performance_state_hash(_selection(data, mode)),
            analysis_contract_version=ANALYSIS_CONTRACT_VERSION)
        db.add(analysis); db.flush()
    else:
        raise HTTPException(409, 'Historical Channel Intelligence snapshots are immutable')
    ids = [item['video_id'] for item in pending.payload['videos']]
    videos = {video.tiktok_id: video for video in db.scalars(select(Video).where(
        Video.channel_id == channel_id, Video.tiktok_id.in_(ids)
    ))}
    if set(videos) != set(ids):
        raise HTTPException(409, 'Research Pack videos changed since dry run; export and analyze it again')
    for item in pending.payload['videos']:
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
                             goal: str = Form(...), tone: str = Form(...), constraints: str = Form(''),
                             _auth: None = Depends(require_admin), db: Session = Depends(get_db)):
    data = _channel_or_404(db, channel_id)
    analysis = db.scalar(select(ChannelIntelligenceAnalysis).where(ChannelIntelligenceAnalysis.channel_id == channel_id).order_by(
        ChannelIntelligenceAnalysis.updated_at.desc(), ChannelIntelligenceAnalysis.id.desc()))
    if analysis is None:
        raise HTTPException(409, 'Import Channel Intelligence before creating content')
    # This request only composes a prompt. It intentionally has no database write.
    context = _private_context_from_form(business, offer, audience, goal, tone, constraints)
    saved = db.scalar(select(PrivatePersonalStrategy).where(PrivatePersonalStrategy.channel_id == channel_id,
        PrivatePersonalStrategy.private_context == context).order_by(PrivatePersonalStrategy.created_at.desc()))
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
                                      constraints: str = Form(''), _auth: None = Depends(require_admin), db: Session = Depends(get_db)):
    data = _channel_or_404(db, channel_id)
    analysis = _content_pack_analysis_or_404(db, channel_id)
    context = _private_context_from_form(business, offer, audience, goal, tone, constraints)
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
        cards.append(f'<div class="card"><b>{_e(name)}</b><p>{_e(item.get("description") or "")}</p><p><b>Why it worked</b><br>{_e(item.get("description") or "")}</p><ul>{"".join(videos) or "<li class=\"muted\">No video evidence.</li>"}</ul></div>')
    return ''.join(cards) or '<p class="muted">No matching source pattern.</p>'


def _content_pack_results(data: dict[str, Any], analysis: ChannelIntelligenceAnalysis, pack: PrivateContentPack) -> str:
    value = pack.content_pack
    strategy = value['strategy']
    def source_button(item: dict[str, Any], token: str) -> str:
        names = item.get('source_patterns', [])
        return f'<button type="button" class="secondary" onclick="document.getElementById(\'{token}\').open=true;document.getElementById(\'{token}\').scrollIntoView({{behavior:\'smooth\'}})">Ver patrón de origen</button><details id="{token}" class="card"><summary>Patrón de origen</summary>{_source_pattern_html(data, analysis, names)}</details>'
    ideas = ''.join(f'<div class="card"><h3>{_e(item["title"])}</h3><p><b>Hook</b><br>{_e(item["hook"])}</p><p><b>Angle</b><br>{_e(item["angle"])}</p><p><b>Objective</b><br>{_e(item["objective"])}</p><p><b>CTA</b><br>{_e(item["cta"])}</p><div class="actions"><button type="button" class="secondary" onclick="navigator.clipboard.writeText({json.dumps(item["hook"] + "\\n\\n" + item["angle"] + "\\n\\nCTA: " + item["cta"])})">Copiar</button>{source_button(item, 'idea-source-' + str(index))}</div></div>' for index, item in enumerate(value['content_ideas']))
    scripts = ''.join(f'<div class="card"><h3>{_e(item["title"])}</h3><p><b>Hook</b><br>{_e(item["hook"])}</p><p><b>Body</b><br>{_e(item["body"])}</p><p><b>CTA</b><br>{_e(item["cta"])}</p><p class="muted">Duration target: {_e(item["duration_target"])}</p><div class="actions"><button type="button" class="secondary" onclick="navigator.clipboard.writeText({json.dumps(item["hook"] + "\\n\\n" + item["body"] + "\\n\\n" + item["cta"])})">Copiar</button>{source_button(item, 'script-source-' + str(index))}</div></div>' for index, item in enumerate(value['scripts']))
    return f'<div class="card"><h3>Strategy</h3><p><b>Positioning</b><br>{_e(strategy["recommended_positioning"])}</p><p><b>Content formula</b><br>{_e(strategy["content_formula"])}</p><p><b>CTA strategy</b><br>{_e(strategy["recommended_cta_strategy"])}</p><p><b>Content mix</b><br>{_e(strategy["recommended_content_mix"])}</p></div><h3>Content ideas</h3>{ideas}<h3>Scripts</h3>{scripts}'


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
    return _layout('Channel Intelligence detail', f'''<p><a href="/admin/research/channels/{channel_id}/intelligence">← Channel Intelligence</a></p><h2>Channel Intelligence</h2>{_channel_intelligence_results(data, analysis, db)}''')


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
