"""Deterministic local-curator ingestion; never creates scan observations.

Metric-bearing bundles remain preview-only pending a dedicated observation schema.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import func, or_, select, text as sql_text
from sqlalchemy.orm import Session

from .models import Channel, Transcript, Video

MAX_BYTES = 25 * 1024 * 1024
MAX_RECORDS = 10_000
MAX_TEXT = 100_000
ID = re.compile(r'[1-9][0-9]{14,29}\Z')
HANDLE = re.compile(r'[A-Za-z0-9_.]{1,24}\Z')
ID_LINE = re.compile(r'^-\s+ID:\s*`([1-9][0-9]{14,29})`\s*$', re.M)


@dataclass(frozen=True)
class Bundle:
    rows: list[dict]
    transcripts: dict[str, str]
    digest: str
    warnings: list[str]


def invalid(code, detail):
    raise HTTPException(422, f'{code}: {detail}')


def decode(raw, code='INVALID_METADATA'):
    if len(raw) > MAX_BYTES:
        raise HTTPException(413, 'Cada archivo admite hasta 25 MiB')
    try:
        return raw.decode('utf-8', errors='strict')
    except UnicodeDecodeError:
        invalid(code, 'UTF-8 requerido')


def parse_bundle(metadata: bytes, markdown: bytes) -> Bundle:
    try:
        rows = json.loads(decode(metadata))
    except ValueError:
        invalid('INVALID_METADATA', 'JSON inválido')
    if not isinstance(rows, list) or not rows or len(rows) > MAX_RECORDS:
        invalid('INVALID_METADATA', 'Se requiere un array de 1 a 10.000 registros')
    normalized, seen = [], set()
    for item in rows:
        if not isinstance(item, dict) or not isinstance(item.get('id'), str) or not ID.fullmatch(item['id']):
            invalid('INVALID_METADATA', 'ID TikTok inválido')
        if item['id'] in seen:
            invalid('DUPLICATE_METADATA_ID', item['id'])
        seen.add(item['id'])
        row = {'id': item['id']}
        for key, limit in [('author', 24), ('author_id', 64), ('nickname', 256), ('caption', MAX_TEXT)]:
            value = item.get(key)
            if value is not None and (not isinstance(value, str) or len(value) > limit):
                invalid('INVALID_METADATA', key)
            row[key] = value
        if not row['author'] or not HANDLE.fullmatch(row['author']):
            invalid('INVALID_METADATA', 'author requerido / inválido')
        if row['author_id'] is not None and not re.fullmatch(r'[1-9][0-9]{0,63}', row['author_id']):
            invalid('INVALID_METADATA', 'author_id inválido')
        for key in ('views', 'likes'):
            value = item.get(key)
            if value is not None and (type(value) is not int or not 0 <= value <= 2**63 - 1):
                invalid('INVALID_METADATA', key)
            row[key] = value
        value = item.get('created_at')
        try:
            date = datetime.fromisoformat(value.replace('Z', '+00:00')) if value is not None else None
            if date is not None:
                date = date.replace(tzinfo=date.tzinfo or timezone.utc).astimezone(timezone.utc)
        except (ValueError, TypeError, AttributeError, OverflowError):
            invalid('INVALID_METADATA', 'created_at inválido')
        row['created_at'] = date.isoformat() if date else None
        normalized.append(row)
    stable = {r['author_id'] for r in normalized if r['author_id']}
    handles = {r['author'].lower() for r in normalized}
    names = {r['nickname'] for r in normalized if r['nickname']}
    if len(stable) > 1 or (not stable and len(handles) > 1):
        invalid('CHANNEL_CONFLICT', 'El bundle contiene varios canales')
    # Rows without stable identity cannot introduce an unrelated handle.
    if stable and len(handles) > 1 and any(not r['author_id'] for r in normalized):
        invalid('CHANNEL_CONFLICT', 'Username sin identidad estable compatible')
    warnings = []
    if len(handles) > 1 or len(names) > 1:
        warnings.append('Identidad estable única con variantes de username/nickname; se conserva la identidad canónica existente.')
    text = decode(markdown, 'INVALID_TRANSCRIPT_SECTION')
    matches = list(ID_LINE.finditer(text))
    if len(matches) > MAX_RECORDS:
        invalid('INVALID_TRANSCRIPT_SECTION', 'Demasiadas secciones')
    # Reject malformed ID lines rather than silently ignoring them.
    if len(re.findall(r'^-\s+ID:', text, re.M)) != len(matches):
        invalid('INVALID_TRANSCRIPT_SECTION', 'ID inválido')
    if text.strip() and not matches:
        invalid('INVALID_TRANSCRIPT_SECTION', 'No hay IDs exactos')
    transcripts = {}
    for i, match in enumerate(matches):
        video_id = match[1]
        if video_id in transcripts:
            invalid('DUPLICATE_TRANSCRIPT_ID', video_id)
        section = text[match.end():matches[i + 1].start() if i + 1 < len(matches) else len(text)]
        headings = list(re.finditer(r'^### Transcripción:[ \t]*\r?\n', section, re.M))
        if len(headings) != 1:
            invalid('INVALID_TRANSCRIPT_SECTION', video_id)
        body = section[headings[0].end():]
        # Remove trailing native record framing, preserving Markdown inside the text.
        body = re.sub(r'(?:\r?\n){2,}---[ \t]*(?:\r?\n(?:#{1,2} [^\r\n]*|[ \t]*))*\Z', '', body)
        if i + 1 < len(matches):
            body = re.sub(r'(?:\r?\n){2,}#{1,2} [^\r\n]*\r?\n[ \t]*\Z', '', body)
        body = body.strip('\r\n')
        if not body.strip() or len(body) > MAX_TEXT:
            invalid('INVALID_TRANSCRIPT_SECTION', video_id)
        transcripts[video_id] = body
    payload = {'metadata': sorted(normalized, key=lambda r: r['id']), 'transcripts': transcripts}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    return Bundle(sorted(normalized, key=lambda r: r['id']), transcripts, digest, warnings)


def preview(db: Session, bundle: Bundle):
    rows = bundle.rows
    stable = next((r['author_id'] for r in rows if r['author_id']), None)
    author = rows[0]['author']
    warnings, errors = list(bundle.warnings), []
    handles = {r['author'].lower() for r in rows}
    identity = func.lower(Channel.username).in_(handles)
    if stable:
        identity = or_(identity, Channel.tiktok_user_id == stable)
    channels = list(db.scalars(select(Channel).where(Channel.platform == 'tiktok', identity)))
    by_id = next((c for c in channels if stable and c.tiktok_user_id == stable), None)
    by_name = [c for c in channels if c.username.lower() in {r['author'].lower() for r in rows}]
    if any(stable and c.tiktok_user_id and c.tiktok_user_id != stable for c in by_name):
        errors.append('CHANNEL_CONFLICT: username vinculado a otro author_id')
    if len(by_name) > 1 or (by_id and any(c.id != by_id.id for c in by_name)):
        errors.append('CHANNEL_CONFLICT: identidad ambigua')
    channel = by_id or (by_name[0] if len(by_name) == 1 else None)
    if by_id and by_id.username.lower() != author.lower():
        warnings.append('author_id coincide; se conserva el username canónico existente.')
    existing = {v.tiktok_id: (v, t) for v, t in db.execute(select(Video, Transcript)
        .outerjoin(Transcript, Transcript.video_id == Video.id).where(Video.tiktok_id.in_([r['id'] for r in rows])))}
    counts = {k: 0 for k in ('VIDEO_NEW', 'VIDEO_EXISTING', 'TRANSCRIPT_NEW', 'TRANSCRIPT_EXISTS',
        'TRANSCRIPT_EXISTS_DIFFERENT', 'TRANSCRIPT_MISSING_IN_FILE', 'METADATA_ONLY', 'METRIC_NEW_OBSERVATION', 'METRIC_UNCHANGED')}
    classifications = []
    for row in rows:
        found = existing.get(row['id'])
        status = ['VIDEO_EXISTING' if found else 'VIDEO_NEW']
        if found and (not channel or found[0].channel_id != channel.id):
            errors.append(f'VIDEO_CHANNEL_CONFLICT: {row["id"]}')
        if not found and not row['created_at']:
            errors.append(f'INVALID_METADATA: {row["id"]}: published_at obligatorio en el esquema actual; requiere migración para admitir fecha desconocida')
        transcript = bundle.transcripts.get(row['id'])
        if transcript is None:
            status.extend(['TRANSCRIPT_MISSING_IN_FILE', 'METADATA_ONLY'])
        elif found and found[1]:
            status.append('TRANSCRIPT_EXISTS' if found[1].text == transcript else 'TRANSCRIPT_EXISTS_DIFFERENT')
            if found[1].text != transcript:
                warnings.append(f'Transcripción diferente preservada: {row["id"]}')
        else:
            status.append('TRANSCRIPT_NEW')
        for key in status:
            counts[key] += 1
        classifications.append((row['id'], status))
    for video_id in bundle.transcripts.keys() - {r['id'] for r in rows}:
        warnings.append(f'TRANSCRIPT_WITHOUT_METADATA: {video_id}; no se importará')
    metrics = sum(r['views'] is not None or r['likes'] is not None for r in rows)
    if metrics:
        errors.append(f'METRIC_SCHEMA_REQUIRED: {metrics} observaciones parciales pendientes de un esquema seguro. Importación bloqueada; no se descartarán métricas.')
    return dict(channel=channel, author=author, author_id=stable, nickname=rows[0]['nickname'],
        counts=counts, warnings=warnings, errors=errors, classifications=classifications,
        metrics_pending=metrics, channel_status='CHANNEL_CONFLICT' if any(e.startswith('CHANNEL_CONFLICT') for e in errors) else 'CHANNEL_EXISTING' if channel else 'CHANNEL_NEW',
        matched=len(bundle.transcripts.keys() & {r['id'] for r in rows}))


def persist(db: Session, bundle: Bundle):
    """Revalidate at confirmation, then atomically import assets without scans."""
    if db.get_bind().dialect.name == 'postgresql':
        # Serialize native confirmations, including username-only channel creation.
        db.execute(sql_text('SELECT pg_advisory_xact_lock(740183279124)'))
    result = preview(db, bundle)
    if result['errors']:
        raise HTTPException(409, result['errors'])
    channel = result['channel']
    if channel is None:
        channel = Channel(platform='tiktok', username=result['author'], nickname=result['nickname'] or '', tiktok_user_id=result['author_id'])
        db.add(channel)
        db.flush()
    else:
        channel = db.scalar(select(Channel).where(Channel.id == channel.id).with_for_update().execution_options(populate_existing=True))
        if result['author_id'] and channel.tiktok_user_id not in (None, '', result['author_id']):
            raise HTTPException(409, 'CHANNEL_CONFLICT: identidad modificada')
        if not channel.tiktok_user_id and result['author_id']:
            channel.tiktok_user_id = result['author_id']
    for row in bundle.rows:
        video = db.scalar(select(Video).where(Video.tiktok_id == row['id']).with_for_update())
        values = dict(author=row['author'], nickname=row['nickname'] or '', caption=row['caption'] or '',
            url=f'https://www.tiktok.com/@{row["author"]}/video/{row["id"]}')
        if row['created_at']:
            values['published_at'] = datetime.fromisoformat(row['created_at'])
        if video is None:
            video = Video(channel_id=channel.id, tiktok_id=row['id'], **values)
            db.add(video)
            db.flush()
        else:
            if video.channel_id != channel.id:
                raise HTTPException(409, 'VIDEO_CHANNEL_CONFLICT')
            for key, value in values.items():
                if not getattr(video, key):
                    setattr(video, key, value)
        text = bundle.transcripts.get(row['id'])
        if text is not None and db.scalar(select(Transcript.id).where(Transcript.video_id == video.id)) is None:
            db.add(Transcript(video_id=video.id, text=text, model='local-curator-import-v1', language=None, duration=video.duration))
    db.flush()
    return result
