import hashlib
from math import ceil
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import exists, func, or_, select, text, update

from .config import MIN_DISCOVERED_FOR_ADAPTIVE_INCREMENTAL, get_settings
from .models import (Analysis, AnalysisAcquisition, AudioAssessment, Channel, Transcript,
                     Video, VideoSnapshot, TranscriptionJob, now)
from .ranking import rank_snapshots, rank_videos


ACQUISITION_VIDEO_NOT_AVAILABLE_COOLDOWN = timedelta(hours=24)


def lock_ingest(db, username, tiktok_user_id=None):
    if db.bind.dialect.name == 'postgresql':
        # Take both known identity locks in a deterministic order.  This closes
        # the legacy-handle/stable-id race while old collectors still omit ids.
        values = sorted({f'username:{username}', *( [f'tiktok_user_id:{tiktok_user_id}'] if tiktok_user_id else [])})
        for value in values:
            key = int.from_bytes(hashlib.sha256(value.encode()).digest()[:8], signed=True)
            db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': key})


def lock_audio(db):
    if db.bind.dialect.name == 'postgresql':
        if not db.scalar(text('SELECT pg_try_advisory_xact_lock(742819501)')):
            raise HTTPException(429, 'Whisper busy; retry later', headers={'Retry-After': '5'})


def eligibility(duration):
    config = get_settings()
    if duration is None:
        return False, 'unknown_duration'
    if duration < config.min_transcribe_duration_seconds:
        return False, 'too_short'
    if duration > min(config.auto_transcribe_max_duration_seconds, config.hard_transcribe_max_duration_seconds):
        return False, 'long_form'
    return True, None


def analysis_rows(db, analysis):
    return db.execute(select(VideoSnapshot, Video, Transcript)
                      .join(Video, Video.id == VideoSnapshot.video_id)
                      .outerjoin(Transcript, Transcript.video_id == Video.id)
                      .where(VideoSnapshot.analysis_id == analysis.id)
                      .order_by(VideoSnapshot.overall_rank)).all()


def acquisition_rows(db, analysis_id):
    return db.execute(select(AnalysisAcquisition, Video, Transcript)
                      .join(Video, Video.id == AnalysisAcquisition.video_id)
                      .outerjoin(Transcript, Transcript.video_id == Video.id)
                      .where(AnalysisAcquisition.analysis_id == analysis_id)
                      .order_by(AnalysisAcquisition.transcription_rank)).all()


def latest_channel_rows(db, channel_id):
    """Return each global video with exactly its latest public observation."""
    latest = select(VideoSnapshot.id, VideoSnapshot.video_id, func.row_number().over(
        partition_by=VideoSnapshot.video_id,
        order_by=(VideoSnapshot.created_at.desc(), VideoSnapshot.id.desc())).label('position')).subquery()
    return db.execute(select(Video, VideoSnapshot, Transcript)
        .join(latest, (latest.c.position == 1) & (latest.c.video_id == Video.id))
        .join(VideoSnapshot, VideoSnapshot.id == latest.c.id)
        .outerjoin(Transcript, Transcript.video_id == Video.id)
        .where(Video.channel_id == channel_id)).all()


def global_ranked_videos(db, channel_id):
    rows = latest_channel_rows(db, channel_id)
    median_views, ranked = rank_snapshots((video, snapshot) for video, snapshot, _ in rows)
    transcripts = {video.id: transcript for video, _, transcript in rows}
    return median_views, [(video, snapshot, rates, transcripts[video.id])
                          for video, snapshot, rates in ranked]


def analysis_ranked_videos(db, analysis):
    """Global ranking, restricted to videos the browser saw in this analysis.

    VideoSnapshot is the durable analysis -> scanned-video membership record.
    Ranking still uses each video's latest channel-wide observation; only the
    set that may be reserved for browser acquisition is narrowed here.
    """
    scanned_video_ids = set(db.scalars(select(VideoSnapshot.video_id).where(
        VideoSnapshot.analysis_id == analysis.id)))
    _, ranked = global_ranked_videos(db, analysis.channel_id)
    return [(video, snapshot, rates, transcript) for video, snapshot, rates, transcript in ranked
            if video.id in scanned_video_ids]


COMMITTED_ENRICHMENT_STATUSES = ('reserved', 'audio_received', 'queued', 'processing',
                                 'completed', 'failed', 'skipped')


def new_enrichment_budget(discovered, config=None, eligible_missing_count=None):
    """Return this scan's bounded allowance for genuinely new transcript work.

    Resolved videos are intentionally outside this allowance.  Tiny completed
    channels are additionally capped by the missing feasible candidates so a
    policy minimum cannot manufacture work that does not exist.
    """
    config = config or get_settings()
    discovered = max(0, int(discovered))
    budget = min(config.enrichment_max_new, max(
        config.enrichment_min_new, ceil(discovered * config.enrichment_target_ratio)))
    if discovered < config.enrichment_min_new:
        budget = min(budget, discovered)
        if eligible_missing_count is not None:
            budget = min(budget, max(0, int(eligible_missing_count)))
    return budget


def new_enrichment_used_ids(db, analysis):
    """Unique work still committed to this analysis; released reservations free a slot."""
    rows = db.execute(select(AnalysisAcquisition, Video, TranscriptionJob)
        .join(Video, Video.id == AnalysisAcquisition.video_id)
        .join(TranscriptionJob, TranscriptionJob.video_id == Video.id)
        .where(AnalysisAcquisition.analysis_id == analysis.id,
               TranscriptionJob.status.in_(COMMITTED_ENRICHMENT_STATUSES))).all()
    return {acquisition.video_id for acquisition, video, job in rows
            if video.enrichment_analysis_id == analysis.id and
            (job.status != 'reserved' or lease_active(video))}


def selection_rank(row):
    video, snapshot, rates, _transcript = row
    return rates['outlier_score'], snapshot.views, rates['engagement_rate']


def enrichment_eligibility(db, analysis, discovery_complete=False, has_more=False):
    """Return the current-scan candidates and safe selection diagnostics.

    The ranking input is the full channel corpus, so an outlier score is
    always relative to the channel's currently available median.  Selection
    itself remains limited to this logical scan; discovery never turns into a
    global transcription backfill.
    """
    config = get_settings()
    rows = analysis_ranked_videos(db, analysis)
    video_ids = [video.id for video, _, _, _ in rows]
    jobs = {job.video_id: job for job in db.scalars(select(TranscriptionJob).where(
        TranscriptionJob.video_id.in_(video_ids)))} if video_ids else {}
    incremental = []
    final = []
    incremental_by_views = final_by_views = final_by_outlier = final_total = 0
    effective_min_views = max(Decimal(config.enrichment_min_views),
                              Decimal(analysis.median_views) * Decimal(str(config.incremental_median_multiplier)))
    for video, snapshot, rates, transcript in rows:
        duration_ok = eligibility(video.duration)[0]
        views_ok = snapshot.views >= config.enrichment_min_views
        outlier_ok = rates['outlier_score'] >= Decimal(str(config.enrichment_min_outlier_score))
        if duration_ok and Decimal(snapshot.views) >= effective_min_views:
            incremental_by_views += 1
            incremental.append((video, snapshot, rates, transcript))
        if not discovery_complete or not duration_ok:
            continue
        if views_ok:
            final_by_views += 1
        if outlier_ok:
            final_by_outlier += 1
        if views_ok or outlier_ok:
            final_total += 1
            final.append((video, snapshot, rates, transcript))
    # Partial medians settle materially only after a useful corpus.  Checkpoint
    # persistence remains independent from this no-op selection result.
    selected = final if discovery_complete else ([] if analysis.video_count < MIN_DISCOVERED_FOR_ADAPTIVE_INCREMENTAL and has_more else incremental)
    selected.sort(key=selection_rank, reverse=True)
    candidates, already_resolved = [], 0
    for video, snapshot, rates, transcript in selected:
        job = jobs.get(video.id)
        if transcript is not None or job is not None and job.status in ('completed', 'skipped'):
            already_resolved += 1
        else:
            candidates.append((video, snapshot, rates, transcript))
    used_ids = new_enrichment_used_ids(db, analysis)
    available = [row for row in candidates if row[0].id not in used_ids]
    used = len(used_ids)
    budget = new_enrichment_budget(analysis.video_count, config,
                                   len(available) + used if analysis.video_count < config.enrichment_min_new else None)
    remaining = max(0, budget - used)
    authorized = available[:remaining]
    return candidates, dict(
        median_views_partial=float(analysis.median_views),
        median_views_final=float(analysis.median_views) if discovery_complete else None,
        min_views=config.enrichment_min_views,
        min_outlier=config.enrichment_min_outlier_score,
        incremental_effective_min_views=float(effective_min_views),
        final_candidate_count=final_total if discovery_complete else None,
        globally_resolved_eligible=already_resolved,
        new_enrichment_budget=budget,
        new_enrichment_used=used,
        new_enrichment_remaining=remaining,
        excluded_by_budget=max(0, len(available) - remaining),
        eligible_incremental_by_views=incremental_by_views,
        eligible_final_by_views=final_by_views if discovery_complete else None,
        eligible_final_by_outlier=final_by_outlier if discovery_complete else None,
        eligible_final_total=final_total if discovery_complete else None,
        eligible_total=final_total if discovery_complete else incremental_by_views,
        already_resolved_global=already_resolved,
    ), authorized


def lease_active(video):
    expiry = video.enrichment_lease_until
    if expiry is not None and expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    return expiry is not None and expiry > now()


def refresh_status(db, analysis):
    rows = analysis_rows(db, analysis)
    selected = acquisition_rows(db, analysis.id)
    analysis.completed_transcripts = sum(t is not None for _, _, t in selected)
    if analysis.completed_transcripts == analysis.requested_transcripts:
        analysis.status = 'transcribed'
        analysis.completed_at = analysis.completed_at or now()
    elif any(t is None and v.enrichment_status == 'failed' for _, v, t in selected):
        analysis.status = 'failed'
    elif any(t is None and v.enrichment_status in ('audio_received', 'queued', 'processing') for _, v, t in selected):
        analysis.status = 'processing'
    elif any(t is None and (v.enrichment_analysis_id != analysis.id or not lease_active(v)) for _, v, t in selected):
        analysis.status = 'expired'
    else:
        analysis.status = 'awaiting_audio'
    return rows


def coverage(db, channel_id):
    rows = [(video, transcript, snapshot) for video, snapshot, transcript in latest_channel_rows(db, channel_id)]
    known = len(rows)
    available = sum(t is not None for _, t, _ in rows)
    threshold = Decimal(str(get_settings().high_value_outlier_threshold))
    high = [(v, t, s) for v, t, s in rows if eligibility(v.duration)[0] and s is not None and s.outlier_score >= threshold]
    high_done = sum(t is not None for _, t, _ in high)
    skipped = {job.video_id for job in db.scalars(select(TranscriptionJob).where(
        TranscriptionJob.status == 'skipped', TranscriptionJob.skip_reason == 'music'))}
    resolved = sum(t is not None or v.id in skipped for v, t, _ in rows)
    assessments_total = db.scalar(select(func.count()).select_from(AudioAssessment)
                                   .join(Video, Video.id == AudioAssessment.video_id)
                                   .where(Video.channel_id == channel_id))
    return dict(videos_known=known, transcripts_available=available, transcripts_total=available,
                transcripts_missing=known - available, audio_assessments_total=assessments_total,
                transcript_coverage=available / known if known else 0,
                high_value_candidates=len(high), high_value_transcribed=high_done,
                high_value_coverage=high_done / len(high) if high else 0,
                resolved_enrichment=resolved, resolved_enrichment_coverage=resolved / known if known else 0)


def analysis_response(db, analysis, discovery_complete=True, has_more=False):
    rows = refresh_status(db, analysis)
    videos, requests = [], []
    acquisitions = acquisition_rows(db, analysis.id)
    acquisition_by_video = {acquisition.video_id: acquisition for acquisition, _, _ in acquisitions}
    all_video_ids = {v.id for _, v, _ in rows} | set(acquisition_by_video)
    jobs = {job.video_id: job for job in db.scalars(select(TranscriptionJob).where(
        TranscriptionJob.video_id.in_(all_video_ids)))} if all_video_ids else {}
    for s, v, t in rows:
        job = jobs.get(v.id)
        acquisition = acquisition_by_video.get(v.id)
        transcription_rank = acquisition.transcription_rank if acquisition else s.transcription_rank
        needs_audio = (t is None and acquisition is not None and
                       v.enrichment_analysis_id == analysis.id and lease_active(v)
                       and eligibility(v.duration)[0] and job is not None and job.status == 'reserved')
        item = dict(overall_rank=s.overall_rank, transcription_rank=transcription_rank,
                    tiktok_id=v.tiktok_id, url=v.url, views=s.views, likes=s.likes,
                    outlier_score=float(s.outlier_score), engagement_rate=float(s.engagement_rate),
                    transcript_eligible=s.transcript_eligible, transcript_skip_reason=s.transcript_skip_reason,
                    needs_audio=needs_audio, transcript_status='completed' if t else job.status if job else v.enrichment_status,
                    transcript=t.text if t else None, language=t.language if t else None,
                    duration=v.duration)
        videos.append(item)
        if needs_audio:
            requests.append(dict(tiktok_id=v.tiktok_id, url=v.url, job_id=str(job.id),
                transcription_rank=transcription_rank,
                priority=dict(outlier_score=float(s.outlier_score), engagement_rate=float(s.engagement_rate)),
                needs_audio=True))
    channel = db.get(Channel, analysis.channel_id)
    channel_coverage = dict(username=channel.username, tiktok_user_id=channel.tiktok_user_id,
                            **coverage(db, channel.id))
    _candidates, enrichment, _authorized = enrichment_eligibility(db, analysis, discovery_complete, has_more)
    scan = dict(videos_seen_this_scan=analysis.video_count, videos_new=analysis.videos_new,
                videos_refreshed=analysis.videos_refreshed,
                acquisition_requested=analysis.requested_transcripts)
    return dict(analysis_id=str(analysis.id), status=analysis.status,
                channel=channel_coverage, scan=scan,
                coverage=dict(channel=channel_coverage, scan=scan),
                videos_received=analysis.video_count, median_views=float(analysis.median_views),
                requested_transcripts=analysis.requested_transcripts,
                completed_transcripts=analysis.completed_transcripts, top_videos=videos,
                enrichment=enrichment,
                enrichment_requests=sorted(requests, key=lambda r: r['transcription_rank']))


def claim_video(db, video_id, analysis_id):
    # Atomic compare-and-set, plus UNIQUE transcripts.video_id, across all analyses.
    current = now()
    active_count = db.scalar(select(func.count()).select_from(TranscriptionJob).where(
        TranscriptionJob.status.in_(('reserved', 'audio_received', 'queued', 'processing'))))
    if active_count >= get_settings().audio_queue_max_jobs:
        return False
    result = db.execute(update(Video).where(Video.id == video_id,
        or_(Video.enrichment_lease_until.is_(None), Video.enrichment_lease_until <= current),
        ~exists(select(Transcript.id).where(Transcript.video_id == Video.id)),
        # A browser-confirmed unavailable MP4 is likely transient, but retrying
        # it immediately creates a reservation/failure loop.  updated_at is
        # explicitly recorded by release_reserved_acquisition on that exact
        # reserved -> expired transition, including rows created before this
        # cooldown was introduced.
        ~exists(select(TranscriptionJob.id).where(
            TranscriptionJob.video_id == Video.id,
            TranscriptionJob.status == 'expired',
            TranscriptionJob.last_error_code == 'FETCH_MP4_VIDEO_NOT_AVAILABLE',
            TranscriptionJob.updated_at > current - ACQUISITION_VIDEO_NOT_AVAILABLE_COOLDOWN)),
        ~exists(select(TranscriptionJob.id).where(TranscriptionJob.video_id == Video.id,
            TranscriptionJob.status.in_(('audio_received', 'queued', 'processing', 'completed', 'failed', 'skipped')))))
        .values(enrichment_status='requested', enrichment_analysis_id=analysis_id,
                enrichment_lease_until=current + timedelta(seconds=get_settings().enrichment_lease_seconds)),
        execution_options={'synchronize_session': False})
    if result.rowcount != 1:
        return False
    job = db.scalar(select(TranscriptionJob).where(TranscriptionJob.video_id == video_id).with_for_update())
    if job is None:
        job = TranscriptionJob(video_id=video_id)
        db.add(job)
    job.status = 'reserved'
    job.reserved_at = current
    job.last_error_code = None
    db.flush()
    return True


def reserve_for_analysis(db, video, analysis, snapshot=None):
    """Claim a global video once and attach only a lightweight session reference."""
    acquisition = db.scalar(select(AnalysisAcquisition).where(
            AnalysisAcquisition.analysis_id == analysis.id,
            AnalysisAcquisition.video_id == video.id))
    if acquisition is not None:
        # A browser-reported pre-audio failure leaves this lightweight
        # association in place.  Reclaim its expired job without duplicating
        # the association or inflating the analysis request count.
        job = db.scalar(select(TranscriptionJob).where(TranscriptionJob.video_id == video.id))
        if job is None or job.status != 'expired' or not claim_video(db, video.id, analysis.id):
            return False
        db.expire(video)
        return True
    if not claim_video(db, video.id, analysis.id):
        return False
    rank = analysis.requested_transcripts + 1
    db.add(AnalysisAcquisition(analysis_id=analysis.id, video_id=video.id,
                               transcription_rank=rank))
    # Retain the legacy field for old readers of an analysis snapshot.  The
    # normalized association above is the source of truth for global selection.
    if snapshot is not None:
        snapshot.transcription_rank = rank
    analysis.requested_transcripts = rank
    db.expire(video)
    return True


def resolve_channel(db, profile):
    username = profile.username.lower()
    stable_id = profile.author_id
    lock_ingest(db, username, stable_id)
    by_stable_id = (db.scalar(select(Channel).where(Channel.platform == 'tiktok',
        Channel.tiktok_user_id == stable_id).with_for_update()) if stable_id else None)
    by_username = db.scalars(select(Channel).where(Channel.platform == 'tiktok',
        Channel.username == username).order_by(Channel.updated_at.desc(), Channel.id.desc()).with_for_update()).first()

    if by_stable_id is not None:
        channel = by_stable_id
        # An older handle-only row is known to be the same account once the
        # stable ID has resolved it.  Preserve all corpus rows when consolidating.
        if by_username is not None and by_username.id != channel.id and by_username.tiktok_user_id is None:
            db.execute(update(Video).where(Video.channel_id == by_username.id).values(channel_id=channel.id))
            db.execute(update(Analysis).where(Analysis.channel_id == by_username.id).values(channel_id=channel.id))
            db.delete(by_username)
            db.flush()
        # A handle can be recycled.  With no username uniqueness constraint the
        # stable account remains authoritative and can expose its current handle.
        channel.username = username
    elif by_username is not None:
        channel = by_username
        if stable_id:
            channel.tiktok_user_id = stable_id
    else:
        channel = Channel(platform='tiktok', username=username, nickname=profile.nickname,
                          tiktok_user_id=stable_id)
        db.add(channel)
        db.flush()
    if profile.nickname is not None:
        channel.nickname = profile.nickname
    channel.updated_at = now()
    return channel


def create_analysis(db, payload, analysis_id=None, reserve=True):
    """Create or append a browser checkpoint to one logical analysis.

    ``analysis_id`` is supplied only by the resumable browser endpoint.  The
    unique snapshot pair makes replaying a checkpoint harmless; there is no
    server-side TikTok pagination or scan state.
    """
    channel = resolve_channel(db, payload.profile)
    # Preserve the old response field immediately; it is overwritten with the
    # channel-global median after this scan's observations have been recorded.
    _, ranked = rank_videos(payload.videos)
    analysis = db.get(Analysis, analysis_id) if analysis_id is not None else None
    if analysis is not None:
        if analysis.channel_id != channel.id:
            raise HTTPException(409, 'Analysis belongs to another channel')
    else:
        analysis = Analysis(id=analysis_id, channel_id=channel.id, video_count=0, median_views=Decimal(0),
                            requested_transcripts=0, videos_new=0, videos_refreshed=0)
        db.add(analysis)
        db.flush()
    existing = {v.tiktok_id: v for v in db.scalars(select(Video).where(Video.tiktok_id.in_(
        [v.id for v in payload.videos])).order_by(Video.id).with_for_update())}
    current_snapshots = {snapshot.video_id: snapshot for snapshot in db.scalars(
        select(VideoSnapshot).where(VideoSnapshot.analysis_id == analysis.id))}
    for rank, (item, rates) in enumerate(ranked, 1):
        video = existing.get(item.id)
        snapshot = current_snapshots.get(video.id) if video is not None else None
        video_is_new = video is None
        if video is None:
            video = Video(channel_id=channel.id, tiktok_id=item.id, first_seen_at=now(), last_seen_at=now())
            db.add(video)
            is_new_snapshot = True
        elif video.channel_id != channel.id:
            raise HTTPException(409, 'Video belongs to another channel')
        else:
            is_new_snapshot = snapshot is None
        if is_new_snapshot:
            if video_is_new:
                analysis.videos_new += 1
            else:
                analysis.videos_refreshed += 1
        for key in ('author', 'nickname', 'caption', 'duration', 'url'):
            value = getattr(item, key)
            if value is not None:
                setattr(video, key, value)
        # Sound metadata is a global video asset.  A partial later scan must not
        # erase an already-known value; False is an explicit, valid value.
        for key in ('music_id', 'music_title', 'music_author', 'music_original'):
            value = getattr(item, key)
            if value is not None:
                setattr(video, key, value)
        video.published_at = datetime.fromtimestamp(item.created_at, timezone.utc)
        video.last_seen_at = now()
        video.updated_at = now()
        db.flush()
        eligible, reason = eligibility(item.duration)
        if snapshot is None:
            snapshot = VideoSnapshot(analysis_id=analysis.id, video_id=video.id, overall_rank=rank,
                transcript_eligible=eligible, transcript_skip_reason=reason, **rates,
                **{k: getattr(item, k) for k in ('views', 'likes', 'comments', 'shares', 'favorites')})
            db.add(snapshot)
            db.flush()
            current_snapshots[video.id] = snapshot
        else:
            snapshot.overall_rank = rank
            snapshot.transcript_eligible = eligible
            snapshot.transcript_skip_reason = reason
            for key, value in {**rates, **{k: getattr(item, k) for k in ('views', 'likes', 'comments', 'shares', 'favorites')}}.items():
                setattr(snapshot, key, value)
    db.flush()
    analysis.video_count = len(current_snapshots)

    # Re-rank from the latest observation of every global video in this channel,
    # not just the incoming payload.  Historical snapshot ranks remain intact.
    median_views, global_ranked = global_ranked_videos(db, channel.id)
    analysis.median_views = median_views
    for rank, (video, _latest, rates, _transcript) in enumerate(global_ranked, 1):
        snapshot = current_snapshots.get(video.id)
        if snapshot is not None:
            snapshot.overall_rank = rank
            for key, value in rates.items():
                setattr(snapshot, key, value)
    # A resumable checkpoint is only durable discovery.  Its caller reserves
    # exactly one normal acquisition batch afterwards, so discovery can wait
    # for the browser's HTTP 202s without coupling it to Whisper completion.
    # The legacy one-shot ingest endpoint retains its original initial reserve.
    if reserve:
        for video, latest, _rates, transcript in enrichment_eligibility(db, analysis, True)[2]:
            if analysis.requested_transcripts >= get_settings().acquisition_batch_size:
                break
            reserve_for_analysis(db, video, analysis, current_snapshots.get(video.id))
    db.flush()
    return analysis


def acquisition_batch(db, analysis_id, discovery_complete=False, has_more=False):
    from .jobs import capacity, inbox_lock
    with inbox_lock() as root:
        capacity(db, root)
        analysis = db.scalar(select(Analysis).where(Analysis.id == analysis_id).with_for_update())
        if analysis is None:
            raise HTTPException(404, 'Unknown analysis')
        existing = analysis_response(db, analysis, discovery_complete, has_more)['enrichment_requests']
        remaining = get_settings().acquisition_batch_size - len(existing)
        snapshots = {snapshot.video_id: snapshot for snapshot in db.scalars(select(VideoSnapshot).where(
            VideoSnapshot.analysis_id == analysis_id))}
        rows = enrichment_eligibility(db, analysis, discovery_complete, has_more)[2]
        for video, _snapshot, _rates, transcript in rows:
            if remaining <= 0:
                break
            capacity(db, root, new_job=True)
            if reserve_for_analysis(db, video, analysis, snapshots.get(video.id)):
                remaining -= 1
        db.flush()
        result = analysis_response(db, analysis, discovery_complete, has_more)
        db.commit()
        return result
