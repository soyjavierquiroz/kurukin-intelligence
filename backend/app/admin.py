"""Small, server-rendered internal research backoffice.

This module intentionally owns no acquisition, worker, object-storage, or
Semantic DNA behaviour.  It reads the existing global corpus and writes only
new global ``Transcript`` records after an explicit import confirmation.
"""
from __future__ import annotations

import html
import io
import json
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
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .config import get_settings
from .db import get_db
from .channel_intelligence_contract import (
    CHANNEL_ANALYSIS_JSON_SCHEMA, CHANNEL_INTELLIGENCE_SCHEMA_VERSION,
    canonical_json_sha256, validate_channel_analysis,
)
from .models import (
    Channel, ChannelIntelligenceAnalysis, ChannelVideoIntelligence, Transcript,
    Video, VideoSnapshot,
)
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


def _layout(title: str, content: str) -> HTMLResponse:
    return HTMLResponse(f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(title)} · Kurukin</title><style>
:root{{color-scheme:light;font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#172033;background:#f5f7fa}}
body{{margin:0}}main{{max-width:1280px;margin:auto;padding:28px 20px 48px}}header{{display:flex;gap:18px;align-items:baseline;justify-content:space-between;margin-bottom:24px}}h1{{font-size:1.55rem;margin:0}}h2{{font-size:1.1rem;margin:24px 0 10px}}a{{color:#1659b7;text-decoration:none}}a:hover{{text-decoration:underline}}.muted{{color:#64748b}}.card{{background:#fff;border:1px solid #dce3eb;border-radius:10px;padding:18px;margin:14px 0}}.table-wrap{{overflow-x:auto}}table{{border-collapse:collapse;width:100%;font-size:.9rem}}th,td{{text-align:left;padding:10px 8px;border-bottom:1px solid #e7edf3;vertical-align:top}}th{{white-space:nowrap;color:#526174}}.badge{{display:inline-block;padding:3px 7px;border-radius:999px;font-size:.72rem;font-weight:700;letter-spacing:.02em}}.NO_CORPUS{{background:#fee2e2;color:#991b1b}}.PARTIAL{{background:#fef3c7;color:#92400e}}.PRIORITY_READY{{background:#dcfce7;color:#166534}}.ok{{background:#dcfce7;color:#166534}}.warn{{background:#fef3c7;color:#92400e}}.bad{{background:#fee2e2;color:#991b1b}}button,.button{{font:inherit;background:#1659b7;color:#fff;border:0;border-radius:7px;padding:8px 12px;cursor:pointer;display:inline-block}}button.secondary,.button.secondary{{background:#e7edf3;color:#172033}}input,select,textarea{{font:inherit;border:1px solid #b9c6d4;border-radius:6px;padding:8px;box-sizing:border-box;max-width:100%}}textarea{{width:100%;min-height:340px;white-space:pre-wrap}}form.inline{{display:flex;gap:8px;align-items:center;flex-wrap:wrap}}.actions{{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0}}.stat-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:10px}}.stat{{background:#f8fafc;border:1px solid #e7edf3;border-radius:7px;padding:10px}}.stat b{{display:block;font-size:1.2rem}}code{{font-size:.85em}}@media(max-width:650px){{main{{padding:18px 12px}}header{{display:block}}}}
</style></head><body><main><header><h1><a href="/admin/research">Kurukin Internal Research</a></h1><span class="muted">INTERNAL RESEARCH BACKOFFICE v1</span></header>{content}</main></body></html>''')


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
<div class="actions"><a class="button" href="/admin/research/channels/{channel.id}/import">Import historical transcripts</a><a class="button" href="/admin/research/channels/{channel.id}/export">Export for AI</a><a class="button" href="/admin/research/channels/{channel.id}/prompt">Generate prompt</a><a class="button" href="/admin/research/channels/{channel.id}/intelligence">Channel Intelligence</a><a class="button secondary" href="/admin/research/historical-import-prompt">Copy historical import prompt</a></div>
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


def channel_intelligence_prompt(data: dict[str, Any], mode: str = 'all') -> str:
    """Contractual prompt used beside the downloaded immutable evidence pack."""
    records = _selection(data, mode)
    if not records:
        raise HTTPException(422, 'The selected Research Pack has no resolved transcript videos')
    pack_hash = research_pack_hash(data, mode)
    schema = json.dumps(CHANNEL_ANALYSIS_JSON_SCHEMA, ensure_ascii=False, indent=2)
    return f'''# Kurukin Structured Channel Intelligence v1

Analyze the supplied Kurukin Research Pack only. Return exactly one JSON object, with no Markdown fence, commentary, or omitted fields.

This is a contractual import format. Set `schema` to `{CHANNEL_INTELLIGENCE_SCHEMA_VERSION}` and set `research_pack_hash` exactly to `{pack_hash}`. Produce one `video_intelligence` object for **every one of the {len(records)} Research Pack videos**, exactly once. Every `evidence.video_ids` value must be an exact `video_id` from that same Research Pack. Do not cite a video outside it.

Evidence rules:
- Treat captions, transcripts, and creator claims as evidence with possible errors; do not turn them into verified facts.
- Interpret views, engagement, and outlier score in context; outlier score is relative performance, not viral probability.
- Do not invent metrics, transcripts, intent, examples, or cross-video evidence.
- Use clear, concise claims and tie patterns/opportunities to the supplied videos.

Return JSON conforming exactly to this schema:

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
    pack_hash = value.get('research_pack_hash')
    resolved = _research_pack_by_hash(data, pack_hash) if isinstance(pack_hash, str) else None
    if resolved is None:
        return value, ['research_pack_hash does not match a current non-empty Research Pack for this channel. Re-export, re-analyze, and upload again.'], None, None
    mode, records = resolved
    errors = validate_channel_analysis(value, {record['video_id'] for record in records})
    payload_sha256 = canonical_json_sha256(value)
    existing = db.scalar(select(ChannelIntelligenceAnalysis).where(
        ChannelIntelligenceAnalysis.channel_id == channel_id,
        ChannelIntelligenceAnalysis.research_pack_hash == pack_hash,
        ChannelIntelligenceAnalysis.schema_version == CHANNEL_INTELLIGENCE_SCHEMA_VERSION,
    ))
    status = ('ALREADY_IMPORTED' if existing is not None and existing.payload_sha256 == payload_sha256 else
              'UPDATE_EXISTING' if existing is not None else 'NEW')
    return value, errors, mode, status


def _video_label(record: dict[str, Any]) -> str:
    published = record.get('published_at') or 'unknown date'
    return f"Video {record['video_id']} · {published} · {record.get('views') if record.get('views') is not None else '—'} views"


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
    for section in ('content_pillars', 'winning_patterns', 'performance_insights', 'opportunities'):
        for item in value.get(section, []):
            if isinstance(item, dict):
                result.append((section.replace('_', ' ').title(), item))
    return result


@router.get('/research/channels/{channel_id}/intelligence', response_class=HTMLResponse)
def channel_intelligence_page(channel_id: uuid.UUID, _auth: None = Depends(require_admin), db: Session = Depends(get_db)):
    data = _channel_or_404(db, channel_id)
    analyses = list(db.scalars(select(ChannelIntelligenceAnalysis).where(
        ChannelIntelligenceAnalysis.channel_id == channel_id
    ).order_by(ChannelIntelligenceAnalysis.updated_at.desc(), ChannelIntelligenceAnalysis.id.desc())))
    cards = ''.join(
        f'<tr><td><code>{_e(item.research_pack_hash)}</code></td><td>{_e(item.selection_mode)}</td>'
        f'<td>{_when(item.updated_at)}</td><td><a href="/admin/research/channels/{channel_id}/intelligence/{item.id}">View</a></td></tr>'
        for item in analyses
    ) or '<tr><td colspan="4" class="muted">No imported Channel Intelligence yet.</td></tr>'
    content = f'''<p><a href="/admin/research/channels/{channel_id}">← @{_e(data['channel'].username)}</a></p><h2>Channel Intelligence</h2>
<p class="muted">Structured, externally generated analysis is accepted only when it names the exact current Research Pack hash and includes every video in that pack.</p>
<div class="actions"><a class="button" href="/admin/research/channels/{channel_id}/intelligence/prompt">Generate contractual prompt</a><a class="button secondary" href="/admin/research/channels/{channel_id}/export?mode=all">Export Research Pack</a></div>
<div class="card"><h2>Import analysis</h2><form method="post" action="/admin/research/channels/{channel_id}/intelligence/import/dry-run" enctype="multipart/form-data"><label>kurukin-channel-analysis-v1.json<br><input required type="file" name="file" accept=".json,application/json"></label><div class="actions"><button>Upload and dry run</button></div></form><p class="muted">The dry run validates the frozen schema, Research Pack hash, every per-video record, and evidence references. Nothing is stored until confirmation.</p></div>
<h2>Imported analyses</h2><div class="card table-wrap"><table><thead><tr><th>Research Pack hash</th><th>Selection</th><th>Updated</th><th></th></tr></thead><tbody>{cards}</tbody></table></div>'''
    return _layout('Channel Intelligence', content)


@router.get('/research/channels/{channel_id}/intelligence/prompt', response_class=HTMLResponse)
def channel_intelligence_prompt_page(channel_id: uuid.UUID, mode: str = 'all', _auth: None = Depends(require_admin), db: Session = Depends(get_db)):
    data = _channel_or_404(db, channel_id)
    prompt = channel_intelligence_prompt(data, mode)
    content = f'''<p><a href="/admin/research/channels/{channel_id}/intelligence">← Channel Intelligence</a></p><h2>Contractual external AI prompt</h2><p class="muted">Use it with the matching downloaded Research Pack. The hash binds the result to its evidence set.</p><textarea id="channel-prompt" readonly>{_e(prompt)}</textarea><div class="actions"><button type="button" class="secondary" onclick="navigator.clipboard.writeText(document.getElementById('channel-prompt').value)">Copy</button><a class="button" href="/admin/research/channels/{channel_id}/export.zip?mode={_e(mode)}">Download matching Research Pack</a></div>'''
    return _layout('Channel Intelligence prompt', content)


@router.post('/research/channels/{channel_id}/intelligence/import/dry-run', response_class=HTMLResponse)
async def channel_intelligence_import_dry_run(channel_id: uuid.UUID, file: UploadFile = File(...), _auth: None = Depends(require_admin), db: Session = Depends(get_db)):
    _channel_or_404(db, channel_id)
    value, errors, mode, status = dry_run_channel_intelligence_import(
        db, channel_id, await file.read(CHANNEL_INTELLIGENCE_IMPORT_MAX_BYTES + 1)
    )
    if value is None or errors:
        error_rows = ''.join(f'<li>{_e(error)}</li>' for error in errors)
        return _layout('Channel Intelligence dry run', f'''<p><a href="/admin/research/channels/{channel_id}/intelligence">← Channel Intelligence</a></p><h2>Dry run rejected</h2><div class="card bad"><ul>{error_rows}</ul><p>No data was stored.</p></div>''')
    assert mode is not None and status is not None
    token = _store_pending_channel_intelligence(PendingChannelIntelligenceImport(
        channel_id, value, mode, canonical_json_sha256(value), time.time()))
    word = 'already imported (confirmation is a no-op)' if status == 'ALREADY_IMPORTED' else ('will update the existing analysis' if status == 'UPDATE_EXISTING' else 'will create a new analysis')
    content = f'''<p><a href="/admin/research/channels/{channel_id}/intelligence">← Channel Intelligence</a></p><h2>Channel Intelligence dry run</h2><div class="card"><p><b>{len(value['video_intelligence'])}</b> per-video records validated against the <b>{_e(mode)}</b> Research Pack.</p><p>Hash: <code>{_e(value['research_pack_hash'])}</code></p><p class="muted">This import {word}. No data has been stored yet.</p><form method="post" action="/admin/research/channels/{channel_id}/intelligence/import/confirm"><input type="hidden" name="token" value="{_e(token)}"><button>Confirm import</button></form></div>'''
    return _layout('Channel Intelligence dry run', content)


@router.post('/research/channels/{channel_id}/intelligence/import/confirm', response_class=HTMLResponse)
def channel_intelligence_import_confirm(channel_id: uuid.UUID, token: str = Form(...), _auth: None = Depends(require_admin), db: Session = Depends(get_db)):
    pending = _take_pending_channel_intelligence(token, channel_id)
    # Re-validate the live corpus: a transcript/import change between preview
    # and confirmation must not silently attach analysis to different evidence.
    _value, errors, mode, _status = dry_run_channel_intelligence_import(
        db, channel_id, json.dumps(pending.payload, ensure_ascii=False).encode('utf-8'))
    if errors or mode != pending.selection_mode:
        raise HTTPException(409, 'Research Pack changed since dry run; export and analyze it again')
    pack_hash = pending.payload['research_pack_hash']
    analysis = db.scalar(select(ChannelIntelligenceAnalysis).where(
        ChannelIntelligenceAnalysis.channel_id == channel_id,
        ChannelIntelligenceAnalysis.research_pack_hash == pack_hash,
        ChannelIntelligenceAnalysis.schema_version == CHANNEL_INTELLIGENCE_SCHEMA_VERSION,
    ).with_for_update())
    if analysis is not None and analysis.payload_sha256 == pending.payload_sha256:
        return _layout('Channel Intelligence imported', f'''<p><a href="/admin/research/channels/{channel_id}/intelligence">← Channel Intelligence</a></p><h2>Already imported</h2><div class="card"><p>This exact analysis was already persisted. No records changed.</p></div>''')
    changed = analysis is not None
    if analysis is None:
        analysis = ChannelIntelligenceAnalysis(channel_id=channel_id, research_pack_hash=pack_hash,
            schema_version=CHANNEL_INTELLIGENCE_SCHEMA_VERSION, selection_mode=mode,
            payload_sha256=pending.payload_sha256, channel_intelligence=pending.payload['channel_intelligence'])
        db.add(analysis); db.flush()
    else:
        analysis.selection_mode = mode
        analysis.payload_sha256 = pending.payload_sha256
        analysis.channel_intelligence = pending.payload['channel_intelligence']
        db.execute(delete(ChannelVideoIntelligence).where(ChannelVideoIntelligence.analysis_id == analysis.id))
        db.flush()
    ids = [item['video_id'] for item in pending.payload['video_intelligence']]
    videos = {video.tiktok_id: video for video in db.scalars(select(Video).where(
        Video.channel_id == channel_id, Video.tiktok_id.in_(ids)
    ))}
    if set(videos) != set(ids):
        raise HTTPException(409, 'Research Pack videos changed since dry run; export and analyze it again')
    for item in pending.payload['video_intelligence']:
        db.add(ChannelVideoIntelligence(analysis_id=analysis.id, video_id=videos[item['video_id']].id,
                                        intelligence=item))
    db.commit()
    action = 'updated' if changed else 'imported'
    return _layout('Channel Intelligence imported', f'''<p><a href="/admin/research/channels/{channel_id}/intelligence/{analysis.id}">View Channel Intelligence</a></p><h2>Channel Intelligence {action}</h2><div class="card"><p><b>{len(ids)}</b> per-video records and one channel-level record were persisted.</p><p class="muted">The Research Pack hash remains the evidence identity: <code>{_e(pack_hash)}</code></p></div>''')


@router.get('/research/channels/{channel_id}/intelligence/{analysis_id}', response_class=HTMLResponse)
def channel_intelligence_detail(channel_id: uuid.UUID, analysis_id: uuid.UUID, _auth: None = Depends(require_admin), db: Session = Depends(get_db)):
    analysis = db.scalar(select(ChannelIntelligenceAnalysis).where(
        ChannelIntelligenceAnalysis.id == analysis_id, ChannelIntelligenceAnalysis.channel_id == channel_id
    ))
    if analysis is None:
        raise HTTPException(404, 'Unknown Channel Intelligence analysis')
    data = _channel_or_404(db, channel_id)
    records = _selection(data, analysis.selection_mode)
    by_tiktok_id = {record['video_id']: record for record in records}
    channel_value = analysis.channel_intelligence
    sections = ''.join(
        f'<h2>{_e(section)}</h2><div class="card"><h3>{_e(item.get("name") or item.get("opportunity") or "Insight")}</h3><p>{_e(item.get("description") or item.get("rationale") or "")}</p>{_evidence_html(item.get("evidence", []), by_tiktok_id)}</div>'
        for section, item in _channel_analysis_items(channel_value)
    ) or '<p class="muted">No channel-level patterns were supplied.</p>'
    video_rows = []
    children = db.execute(select(ChannelVideoIntelligence, Video).join(Video, Video.id == ChannelVideoIntelligence.video_id).where(
        ChannelVideoIntelligence.analysis_id == analysis.id
    ).order_by(Video.published_at.desc(), Video.tiktok_id)).all()
    # A later transcript import can change a live selection.  Imported
    # evidence remains resolvable because the child rows retain the global
    # video identity even when the old pack is no longer reproducible.
    for _child, video in children:
        by_tiktok_id.setdefault(video.tiktok_id, {
            'video_id': video.tiktok_id, 'url': video.url,
            'published_at': _iso(video.published_at), 'views': None,
        })
    for child, video in children:
        item = child.intelligence
        video_rows.append(f'<details class="card"><summary><b>{_e(video.tiktok_id)}</b> — {_e(item["topic"])} · {_e(item["content_format"])}</summary><p><b>Summary:</b> {_e(item["summary"])}</p><p><b>Hook:</b> {_e(item["hook"])}</p><p><b>Angle:</b> {_e(item["angle"])}</p><p><b>Audience:</b> {_e(item["target_audience"])}</p><p><b>Narrative:</b> {_e(item["narrative_structure"])}</p><p><b>CTA:</b> {_e(item["cta"])}</p><p><b>Performance interpretation:</b> {_e(item["performance_interpretation"])}</p>{_evidence_html(item["evidence"], by_tiktok_id)}</details>')
    caveats = ''.join(f'<li>{_e(item)}</li>' for item in channel_value.get('caveats', [])) or '<li class="muted">None supplied.</li>'
    content = f'''<p><a href="/admin/research/channels/{channel_id}/intelligence">← Channel Intelligence</a></p><h2>Channel Intelligence</h2><p class="muted">Research Pack <code>{_e(analysis.research_pack_hash)}</code> · {len(children)} videos · { _when(analysis.updated_at) }</p><div class="card"><h2>Channel summary</h2><p>{_e(channel_value['channel_summary'])}</p><h3>Audience profile</h3><p>{_e(channel_value['audience_profile'])}</p><h3>Caveats</h3><ul>{caveats}</ul></div>{sections}<h2>Per-video intelligence</h2>{''.join(video_rows)}'''
    return _layout('Channel Intelligence detail', content)


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
    content = f'''<p><a href="/admin/research/channels/{channel_id}">← @{_e(channel.username)}</a></p><h2>Generate external AI prompt</h2><div class="card"><form method="post" action="/admin/research/channels/{channel_id}/prompt"><label>Preset <select name="preset">{options}</select></label><p><label>Business/context<br><input name="business" maxlength="4000"></label></p><p><label>Goal<br><input name="goal" maxlength="4000"></label></p><p><label>Additional instructions<br><textarea name="instructions" style="min-height:120px" maxlength="8000"></textarea></label></p><button>Generate prompt</button></form></div>'''
    return _layout('Generate prompt', content)


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
