from datetime import timedelta
from uuid import uuid4
from types import SimpleNamespace
import io
import wave
import pytest
from fastapi import HTTPException
from sqlalchemy import select, func
from app.models import Analysis, AnalysisAcquisition, Channel, TranscriptionJob, Video, VideoSnapshot, Transcript, now
from app.schemas import AnalysisInput, AnalysisCheckpointInput, SENSITIVE
from app.services import acquisition_batch, create_analysis, analysis_response, eligibility, coverage, claim_video
from app.main import process_audio
from app.ranking import rank_videos
from app import audio


def payload(durations=(39., 23., 50., 60., 70.), views=None):
    views = views or [1000, 400, 200, 100, 50][:len(durations)]
    return AnalysisInput.model_validate(dict(profile=dict(username='creator', nickname='Creator'), videos=[
        dict(id=str(10000+i), author='creator', nickname='Creator', caption='Public caption',
             created_at=1700000000, views=v, likes=10, comments=2, shares=1, favorites=1,
             duration=float(d) if d is not None else None, url=f'https://www.tiktok.com/@creator/video/{10000+i}')
        for i, (d, v) in enumerate(zip(durations, views))]))


def payload_with_music(**music):
    data = payload((39.,), [100]).model_dump()
    data['videos'][0].update(music)
    return AnalysisInput.model_validate(data)


def complete(db, video):
    db.add(Transcript(video_id=video.id, text='Valid global transcript', language='es', duration=39., model='small'))
    video.enrichment_status = 'completed'
    video.enrichment_lease_until = None
    db.flush()


def wav(seconds=8):
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as f:
        f.setnchannels(1); f.setsampwidth(2); f.setframerate(16000)
        f.writeframes(b'\0\0' * int(seconds * 16000))
    return buf.getvalue()


def scoped_payload(ids, durations=None, views=None):
    durations = durations or [39.] * len(ids)
    views = views or [100] * len(ids)
    data = payload(tuple(durations), views).model_dump()
    for item, tiktok_id in zip(data['videos'], ids):
        item['id'] = tiktok_id
        item['url'] = f'https://www.tiktok.com/@creator/video/{tiktok_id}'
    return AnalysisInput.model_validate(data)


def expire_reservations(db):
    for video in db.scalars(select(Video)):
        video.enrichment_lease_until = now() - timedelta(seconds=1)
    for job in db.scalars(select(TranscriptionJob)):
        job.status = 'expired'
        job.last_error_code = None
    db.flush()


@pytest.mark.parametrize('duration,expected', [(7,(False,'too_short')), (8,(True,None)),
    (180,(True,None)), (181,(False,'long_form')), (300,(False,'long_form')),
    (301,(False,'long_form')), (None,(False,'unknown_duration'))])
def test_duration(duration, expected):
    assert eligibility(duration) == expected


def test_global_reuse_and_budget(db, monkeypatch):
    monkeypatch.setenv("ACQUISITION_BATCH_SIZE", "3")
    first = create_analysis(db, payload())
    videos = db.scalars(select(Video).order_by(Video.tiktok_id)).all()
    complete(db, videos[0]); complete(db, videos[1]); complete(db, videos[2])
    db.commit()
    second = create_analysis(db, payload())
    response = analysis_response(db, second)
    assert db.scalar(select(func.count()).select_from(Video)) == 5
    assert [r['tiktok_id'] for r in response['enrichment_requests']] == ['10003', '10004']
    assert not response['top_videos'][0]['needs_audio']
    assert response['top_videos'][0]['transcript'] == 'Valid global transcript'
    assert response['channel']['transcripts_available'] == 3
    assert response['channel']['transcript_coverage'] == 3/5
    assert first.id != second.id


def test_acquisition_batch_never_falls_back_to_higher_ranked_global_video(db):
    # B is the global outlier, but this browser scan only supplied A.
    create_analysis(db, scoped_payload(['10000', '10001', '10002'], views=[10, 1_000_000, 100]))
    expire_reservations(db)
    current = create_analysis(db, scoped_payload(['10000'], views=[10]))

    response = acquisition_batch(db, current.id)

    assert [request['tiktok_id'] for request in response['enrichment_requests']] == ['10000']
    assert db.scalar(select(AnalysisAcquisition).where(
        AnalysisAcquisition.analysis_id == current.id,
        AnalysisAcquisition.video_id == db.scalar(select(Video.id).where(Video.tiktok_id == '10001')))) is None


def test_acquisition_batch_scopes_to_scan_then_applies_global_rules(db):
    # C and D remain in the channel corpus but are not candidates for this scan.
    create_analysis(db, scoped_payload(['10002', '10003'], views=[1_000_000, 500_000]))
    expire_reservations(db)
    current = create_analysis(db, scoped_payload(['10000', '10001'], durations=[7., 39.], views=[1, 2]))

    response = acquisition_batch(db, current.id)

    assert [request['tiktok_id'] for request in response['enrichment_requests']] == ['10001']


def test_acquisition_batch_returns_empty_when_scan_has_no_acquirable_video(db):
    create_analysis(db, scoped_payload(['10001', '10002'], views=[1_000_000, 500_000]))
    expire_reservations(db)
    current = create_analysis(db, scoped_payload(['10000'], durations=[7.], views=[1]))

    response = acquisition_batch(db, current.id)

    assert response['enrichment_requests'] == []
    assert db.scalars(select(AnalysisAcquisition).where(
        AnalysisAcquisition.analysis_id == current.id)).all() == []


def test_scanned_video_with_global_transcript_is_not_reserved_and_assets_are_shared(db):
    first = create_analysis(db, scoped_payload(['10000']))
    video = db.scalar(select(Video).where(Video.tiktok_id == '10000'))
    job = db.scalar(select(TranscriptionJob).where(TranscriptionJob.video_id == video.id))
    complete(db, video)
    expire_reservations(db)

    second = create_analysis(db, scoped_payload(['10000']))
    response = acquisition_batch(db, second.id)

    assert response['enrichment_requests'] == []
    assert db.scalar(select(func.count()).select_from(Video)) == 1
    assert db.scalar(select(func.count()).select_from(TranscriptionJob)) == 1
    assert db.get(TranscriptionJob, job.id).video_id == video.id
    assert db.scalars(select(AnalysisAcquisition).where(
        AnalysisAcquisition.analysis_id == second.id)).all() == []
    assert first.id != second.id


def test_global_music_metadata_preserves_partial_scans_and_updates_explicit_values(db):
    create_analysis(db, payload_with_music(music_id='7678773331130125582',
        music_title='original sound', music_author='Marta Marcilla', music_original=True))
    video = db.scalar(select(Video))
    assert (video.music_id, video.music_title, video.music_author, video.music_original) == (
        '7678773331130125582', 'original sound', 'Marta Marcilla', True)

    create_analysis(db, payload_with_music(music_id=None, music_title=None,
        music_author=None, music_original=None))
    assert (video.music_id, video.music_title, video.music_author, video.music_original) == (
        '7678773331130125582', 'original sound', 'Marta Marcilla', True)

    create_analysis(db, payload_with_music(music_title='updated sound', music_original=False))
    assert video.music_title == 'updated sound'
    assert video.music_original is False


def test_music_metadata_validation_is_strict_and_rejects_urls():
    data = payload((39.,), [100]).model_dump()
    video = data['videos'][0]
    video['music_id'] = 7678773331130125582
    with pytest.raises(ValueError):
        AnalysisInput.model_validate(data)
    video['music_id'] = 'https://cdn.example.test/audio.mp3'
    with pytest.raises(ValueError):
        AnalysisInput.model_validate(data)
    video['music_id'] = '7678773331130125582'
    video['music_title'] = 'https://cdn.example.test/audio.mp3'
    with pytest.raises(ValueError):
        AnalysisInput.model_validate(data)
    video['music_title'] = 'original sound'
    video['music_original'] = 'false'
    with pytest.raises(ValueError):
        AnalysisInput.model_validate(data)


def test_analysis_endpoint_accepts_extension_music_fixture_and_persists_it(db):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.db import get_db
    data = payload((39.,), [100]).model_dump()
    data['videos'][0].update(music_id='7678773331130125582', music_title='original sound',
        music_author='Marta Marcilla', music_original=True)
    def session():
        yield db
    app.dependency_overrides[get_db] = session
    try:
        response = TestClient(app).post('/api/v1/analyses', json=data)
        assert response.status_code == 201
        video = db.scalar(select(Video))
        assert (video.music_id, video.music_title, video.music_author, video.music_original) == (
            '7678773331130125582', 'original sound', 'Marta Marcilla', True)
    finally:
        app.dependency_overrides.clear()


def test_analysis_endpoint_persists_false_accepts_null_and_forbids_unknown_fields(db):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.db import get_db
    first = payload((39.,), [100]).model_dump()
    first['videos'][0].update(music_id='7678773331130125582', music_title='original sound',
        music_author='Marta Marcilla', music_original=False)
    second = payload((39.,), [100]).model_dump()
    second['videos'][0]['id'] = '10001'
    second['videos'][0]['url'] = 'https://www.tiktok.com/@creator/video/10001'
    second['videos'][0].update(music_id=None, music_title=None, music_author=None, music_original=None)
    unknown = payload((39.,), [100]).model_dump()
    unknown['videos'][0]['unexpected_extension_field'] = 'still forbidden'
    def session():
        yield db
    app.dependency_overrides[get_db] = session
    try:
        client = TestClient(app)
        assert client.post('/api/v1/analyses', json=first).status_code == 201
        assert db.scalar(select(Video).where(Video.tiktok_id == '10000')).music_original is False
        assert client.post('/api/v1/analyses', json=second).status_code == 201
        null_video = db.scalar(select(Video).where(Video.tiktok_id == '10001'))
        assert (null_video.music_id, null_video.music_title, null_video.music_author, null_video.music_original) == (None, None, None, None)
        assert client.post('/api/v1/analyses', json=unknown).status_code == 422
        schema = client.get('/openapi.json').json()['components']['schemas']['VideoInput']
        assert schema['additionalProperties'] is False
        assert {'music_id', 'music_title', 'music_author', 'music_original'} <= set(schema['properties'])
    finally:
        app.dependency_overrides.clear()


def test_existing_transcripts_do_not_consume_budget(db):
    first = create_analysis(db, payload())
    videos = db.scalars(select(Video).order_by(Video.tiktok_id)).all()
    for v in videos:
        v.enrichment_lease_until = now() - timedelta(seconds=1)
    complete(db, videos[0]); complete(db, videos[3]); db.commit()
    response = analysis_response(db, create_analysis(db, payload()))
    assert [r['tiktok_id'] for r in response['enrichment_requests']] == ['10001','10002','10004']


def test_duplicate_requests_and_atomic_claim(db):
    first = create_analysis(db, payload(durations=(39.,23.), views=[100,50]))
    db.commit()
    second = create_analysis(db, payload(durations=(39.,23.), views=[100,50]))
    assert analysis_response(db, second)['enrichment_requests'] == []
    v = db.scalar(select(Video))
    assert not claim_video(db, v.id, second.id)
    assert len(analysis_response(db, first)['enrichment_requests']) == 2


def test_expired_reservation_reclaim(db):
    first = create_analysis(db, payload())
    for v in db.scalars(select(Video)):
        v.enrichment_lease_until = now() - timedelta(seconds=1)
    db.commit()
    second = create_analysis(db, payload())
    assert len(analysis_response(db, second)['enrichment_requests']) == 5
    assert analysis_response(db, first)['enrichment_requests'] == []
    assert analysis_response(db, first)['status'] == 'expired'


def test_long_overall_first_and_separate_transcription_rank(db):
    response = analysis_response(db, create_analysis(db, payload((900.,39.,23.), [10000,100,50])))
    first = response['top_videos'][0]
    assert first['overall_rank'] == 1 and first['transcription_rank'] is None
    assert first['transcript_skip_reason'] == 'long_form'
    assert response['top_videos'][1]['transcription_rank'] == 1
    assert response['enrichment_requests'][0]['tiktok_id'] == '10001'
    assert first['url'].endswith('/10000')


def test_coverage_latest_snapshot_and_unique_video(db):
    first = create_analysis(db, payload())
    complete(db, db.scalar(select(Video).where(Video.tiktok_id=='10000')))
    db.commit()
    create_analysis(db, payload())
    result = coverage(db, first.channel_id)
    assert result == dict(videos_known=5, transcripts_available=1, transcripts_total=1,
                         transcripts_missing=4, audio_assessments_total=0, transcript_coverage=.2,
                         high_value_candidates=2, high_value_transcribed=1, high_value_coverage=.5,
                         resolved_enrichment=1, resolved_enrichment_coverage=.2)


def test_incremental_scan_counts_metrics_history_and_stable_channel_identity(db):
    first_data = payload((39.,), [20_000]).model_dump()
    first_data['profile']['author_id'] = 'stable-creator-1'
    first = create_analysis(db, AnalysisInput.model_validate(first_data))
    video = db.scalar(select(Video))
    first_seen = video.first_seen_at

    second_data = payload((39.,), [80_000]).model_dump()
    second_data['profile'].update(username='creator_renamed', nickname='Renamed', author_id='stable-creator-1')
    second_data['videos'][0].update(author='creator_renamed', nickname='Renamed', likes=80,
        comments=8, shares=4, favorites=2,
        url='https://www.tiktok.com/@creator_renamed/video/10000')
    second = create_analysis(db, AnalysisInput.model_validate(second_data))
    video = db.scalar(select(Video))
    channel = db.scalar(select(Channel))
    response = analysis_response(db, second)

    assert db.scalar(select(func.count()).select_from(Channel)) == 1
    assert channel.tiktok_user_id == 'stable-creator-1' and channel.username == 'creator_renamed'
    assert db.scalar(select(func.count()).select_from(Video)) == 1
    assert video.first_seen_at == first_seen
    assert video.last_seen_at.replace(tzinfo=None) >= first_seen.replace(tzinfo=None)
    assert db.scalar(select(func.count()).select_from(VideoSnapshot)) == 2
    assert response['top_videos'][0]['views'] == 80_000
    assert response['scan'] == dict(videos_seen_this_scan=1, videos_new=0,
                                    videos_refreshed=1, acquisition_requested=second.requested_transcripts)
    assert response['channel']['videos_known'] == 1
    assert response['channel']['transcripts_missing'] == 1
    assert first.id != second.id


def test_checkpoint_append_is_idempotent_and_keeps_one_analysis_scan_membership(db, monkeypatch):
    monkeypatch.setenv('ACQUISITION_BATCH_SIZE', '10')
    analysis_id = uuid4()
    first = scoped_payload([str(30000 + i) for i in range(50)], views=list(range(50, 0, -1)))
    second = scoped_payload([str(30050 + i) for i in range(50)], views=list(range(100, 50, -1)))
    analysis = create_analysis(db, first, analysis_id=analysis_id)
    create_analysis(db, second, analysis_id=analysis_id)
    # A duplicated browser delivery cannot duplicate a global video, snapshot,
    # or analysis acquisition association.
    create_analysis(db, first, analysis_id=analysis_id)
    assert analysis.id == analysis_id
    assert db.scalar(select(func.count()).select_from(Analysis).where(Analysis.id == analysis_id)) == 1
    assert db.scalar(select(func.count()).select_from(VideoSnapshot).where(
        VideoSnapshot.analysis_id == analysis_id)) == 100
    assert db.get(Analysis, analysis_id).video_count == 100
    associations = db.scalars(select(AnalysisAcquisition).where(
        AnalysisAcquisition.analysis_id == analysis_id)).all()
    assert len({association.video_id for association in associations}) == len(associations)
    assert analysis_response(db, analysis)['status'] != 'transcribed'


def test_checkpoint_schema_rejects_incoherent_or_secret_resume_metadata():
    data = scoped_payload(['40000']).model_dump()
    data.update(analysis_id=str(uuid4()), scan_id=str(uuid4()), checkpoint_number=1,
                checkpoint_count=2, discovered_count=1, target=50)
    with pytest.raises(ValueError):
        AnalysisCheckpointInput.model_validate(data)
    data['checkpoint_count'] = 1
    data['cookies'] = 'forbidden'
    with pytest.raises(ValueError):
        AnalysisCheckpointInput.model_validate(data)


def test_checkpoint_endpoint_replays_the_same_analysis_id(db):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.db import get_db
    first = scoped_payload(['50000']).model_dump()
    first.update(analysis_id=str(uuid4()), scan_id=str(uuid4()), checkpoint_number=1,
                 checkpoint_count=1, discovered_count=1, target=50)
    def session():
        yield db
    app.dependency_overrides[get_db] = session
    try:
        client = TestClient(app)
        one = client.post('/api/v1/analyses/checkpoints', json=first)
        two = client.post('/api/v1/analyses/checkpoints', json=first)
        assert one.status_code == two.status_code == 200
        assert one.json()['analysis_id'] == two.json()['analysis_id'] == first['analysis_id']
        assert db.scalar(select(func.count()).select_from(Analysis)) == 1
        assert db.scalar(select(func.count()).select_from(VideoSnapshot)) == 1
    finally:
        app.dependency_overrides.clear()


def test_global_gap_selection_does_not_reserve_pending_videos_from_prior_scans(db, monkeypatch):
    monkeypatch.setenv('ACQUISITION_BATCH_SIZE', '10')
    first = create_analysis(db, payload(tuple(float(20 + i) for i in range(15)),
                                        list(range(1_500, 0, -100))))
    claimed = {v.id for v in db.scalars(select(Video).where(Video.enrichment_analysis_id == first.id))}
    assert len(claimed) == 10
    for video in db.scalars(select(Video).where(Video.id.in_(claimed))):
        complete(db, video)
    db.commit()

    one_seen = payload((20.,), [1_500])
    second = create_analysis(db, one_seen)
    response = analysis_response(db, second)
    requested = {request['tiktok_id'] for request in response['enrichment_requests']}
    assert response['channel']['videos_known'] == 15
    assert requested == set()
    assert db.scalar(select(func.count()).select_from(AnalysisAcquisition).where(
        AnalysisAcquisition.analysis_id == second.id)) == 0


def test_scan_of_45_existing_and_5_new_preserves_global_video_count(db):
    def scan(count):
        return AnalysisInput.model_validate(dict(profile=dict(username='creator', nickname='Creator'), videos=[
            dict(id=str(20000 + i), author='creator', nickname='Creator', caption='caption',
                 created_at=1700000000, views=1000 - i, likes=1, comments=1, shares=1, favorites=1,
                 duration=20., url=f'https://www.tiktok.com/@creator/video/{20000 + i}')
            for i in range(count)]))
    create_analysis(db, scan(50)); db.commit()
    second = create_analysis(db, scan(55))
    response = analysis_response(db, second)
    assert db.scalar(select(func.count()).select_from(Video)) == 55
    assert response['scan']['videos_new'] == 5
    assert response['scan']['videos_refreshed'] == 50


@pytest.mark.parametrize('duration', [181.,300.,301.,900.])
def test_automatic_long_upload_rejected(db, duration):
    a = create_analysis(db, payload((duration,), [100])); db.commit()
    with pytest.raises(HTTPException) as exc:
        process_audio(a.id, '10000', wav(), db, None)
    assert exc.value.status_code == 422


@pytest.mark.parametrize('seconds', [7,181,301])
def test_actual_wav_duration_checked(db, seconds):
    a = create_analysis(db, payload((39.,), [100])); db.commit()
    with pytest.raises(HTTPException) as exc:
        process_audio(a.id, '10000', wav(seconds), db, None)
    assert exc.value.status_code == 422


@pytest.mark.parametrize('failure', [TimeoutError, RuntimeError, BlockingIOError])
def test_worker_cleanup_and_retry(db, failure):
    from app.models import TranscriptionJob
    from app.workers.whisper import process_job
    from pathlib import Path
    a = create_analysis(db, payload((39.,), [100])); db.commit()
    receipt = process_audio(a.id, '10000', wav(), db, lambda job: None)
    job = db.scalar(select(TranscriptionJob))
    path = Path(job.audio_path)
    def fail(path):
        assert path.exists()
        raise failure('test')
    assert process_job(db, job.id, job.video_id, SimpleNamespace(transcribe=fail)) == 'retry'
    assert path.exists()
    assert process_job(db, job.id, job.video_id, SimpleNamespace(transcribe=lambda p: dict(
        text='Recovery', language='es', duration=8., model='small'))) == 'ack'
    assert not path.exists()
    assert process_audio(a.id, '10000', b'invalid', db)['status'] == 'already_transcribed'
    assert db.scalar(select(func.count()).select_from(Transcript)) == 1


def test_ranking_zero_and_tiebreaks():
    def v(views, likes):
        return SimpleNamespace(views=views, likes=likes, comments=0, shares=0, favorites=0)
    median, rows = rank_videos([v(0,0),v(0,5)])
    assert median == 0 and all(all(x==0 for x in r.values()) for _,r in rows)
    a,b,c = v(10,1),v(10,3),v(100,0)
    _, rows = rank_videos([a,b,c])
    assert [v for v,_ in rows] == [c,b,a]


@pytest.mark.parametrize('key', sorted(SENSITIVE))
def test_sensitive_metadata_rejected(key):
    data = payload().model_dump()
    data['videos'][0][key] = 'sensitive'
    with pytest.raises(ValueError, match='Sensitive field forbidden'):
        AnalysisInput.model_validate(data)


@pytest.mark.parametrize('data', [b'not wav', b'RIFF'+b'\0'*80, wav()[:-1]])
def test_invalid_wav(data):
    with pytest.raises(ValueError):
        audio.validate_wav(data)


def test_competing_claims_have_single_winner(tmp_path):
    import threading
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app.models import Base, Analysis
    engine=create_engine('sqlite:///'+str(tmp_path/'claims.sqlite'), connect_args={'timeout':5})
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        first=create_analysis(session,payload((39.,),[100])); session.commit()
        second=create_analysis(session,payload((39.,),[100])); session.commit()
        video=session.scalar(select(Video)); video.enrichment_lease_until=None
        session.commit()
        video_id=video.id; ids=[first.id,second.id]
    barrier=threading.Barrier(2); results=[]; errors=[]
    def claim(analysis_id):
        try:
            with Session(engine) as session:
                barrier.wait(timeout=3)
                result=claim_video(session,video_id,analysis_id)
                session.commit(); results.append(result)
        except Exception as exc:
            errors.append(exc)
    threads=[threading.Thread(target=claim,args=(i,)) for i in ids]
    for thread in threads: thread.start()
    for thread in threads: thread.join(6)
    assert not errors and sorted(results)==[False,True]
    engine.dispose()


def test_audio_http_real_pcm_and_filename_ignored(db, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.db import get_db
    from app import queue, whisper
    from pathlib import Path
    monkeypatch.setattr(queue, 'publish_job', lambda job: None)
    monkeypatch.setattr(whisper.whisper_service, 'transcribe', lambda p: pytest.fail('Synchronous Whisper'))
    a=create_analysis(db,payload((39.,),[100])); db.commit()
    def session():
        yield db
    app.dependency_overrides[get_db]=session
    try:
        response=TestClient(app).post(f'/api/v1/analyses/{a.id}/videos/10000/audio',
            files={'audio':('../../user.wav',wav(),'audio/wav')})
        assert response.status_code == 202 and response.json()['status'] == 'queued'
        assert response.json()['audio_received'] is True
        from app.config import get_settings
        path = Path(get_settings().audio_queue_dir) / (response.json()['job_id'] + '.wav')
        assert path.read_bytes() == wav()
        assert not list(path.parent.glob('*.part'))
    finally:
        app.dependency_overrides.clear()
