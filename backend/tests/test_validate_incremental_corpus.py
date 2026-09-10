from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from sqlalchemy import select

from app.models import (AudioAssessment, Transcript, TranscriptionJob, Video,
                        VideoSnapshot)
from app.schemas import AnalysisInput
from app.services import create_analysis


SCRIPT = Path(__file__).parents[1] / 'ops' / 'validate_incremental_corpus.py'
SPEC = spec_from_file_location('validate_incremental_corpus', SCRIPT)
validator = module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(validator)


def payload():
    return AnalysisInput.model_validate({
        'profile': {'username': 'creator', 'nickname': 'Creator'},
        'videos': [
            {'id': str(10000 + index), 'author': 'creator', 'nickname': 'Creator',
             'caption': 'Public caption', 'created_at': 1700000000,
             'views': 1000 - index * 100, 'likes': 10, 'comments': 2, 'shares': 1,
             'favorites': 1, 'duration': 39.0,
             'url': f'https://www.tiktok.com/@creator/video/{10000 + index}'}
            for index in range(3)
        ],
    })


def add_assessment(db, video):
    db.add(AudioAssessment(
        video_id=video.id, classifier='yamnet', classifier_version='1', model_sha256='a' * 64,
        classification='speech', speech_score=.9, music_score=.05, singing_score=.05,
        speech_patch_ratio=.9, music_patch_ratio=.05, singing_patch_ratio=.05,
    ))


def complete(db, video):
    db.add(Transcript(video_id=video.id, text='No content is persisted by the auditor',
                      language='es', duration=39.0, model='small'))
    job = db.scalar(select(TranscriptionJob).where(TranscriptionJob.video_id == video.id))
    job.status = 'completed'


def test_compare_passes_for_an_incremental_refresh(db):
    create_analysis(db, payload())
    videos = db.scalars(select(Video).order_by(Video.tiktok_id)).all()
    complete(db, videos[0])
    add_assessment(db, videos[0])
    db.commit()
    baseline = validator.collect_snapshot(db)

    create_analysis(db, payload())
    db.commit()
    result = validator.compare_snapshots(db, baseline, validator.collect_snapshot(db))

    assert result['deltas'] == {
        'analyses': 1, 'videos': 0, 'video_snapshots': 3, 'transcripts': 0,
        'audio_assessments': 0, 'analysis_acquisitions': 0,
    }
    assert result['latest_analysis']['videos_seen_this_scan'] == 3
    assert result['latest_analysis']['videos_new'] == 0
    assert result['latest_analysis']['videos_refreshed'] == 3
    assert result['latest_analysis']['videos_known'] == 3
    assert result['latest_analysis']['transcripts_available'] == 1
    assert result['failures'] == []


def test_compare_reports_disappeared_records_and_redundant_job(db):
    create_analysis(db, payload())
    videos = db.scalars(select(Video).order_by(Video.tiktok_id)).all()
    complete(db, videos[0])
    add_assessment(db, videos[0])
    db.commit()
    baseline = validator.collect_snapshot(db)

    create_analysis(db, payload())
    db.flush()
    db.delete(db.scalar(select(Transcript).where(Transcript.video_id == videos[0].id)))
    db.delete(db.scalar(select(AudioAssessment).where(AudioAssessment.video_id == videos[0].id)))
    db.add(Transcript(video_id=videos[1].id, text='already present', language='es',
                      duration=39.0, model='small'))
    job = db.scalar(select(TranscriptionJob).where(TranscriptionJob.video_id == videos[1].id))
    job.status = 'reserved'
    db.commit()

    result = validator.compare_snapshots(db, baseline, validator.collect_snapshot(db))

    assert 'F: existing transcripts disappeared' in result['failures']
    assert 'G: existing audio_assessments disappeared' in result['failures']
    assert 'H: transcript-bearing videos have active redundant enrichment jobs' in result['failures']


def test_compare_requires_a_new_analysis(db):
    create_analysis(db, payload())
    db.commit()
    baseline = validator.collect_snapshot(db)

    result = validator.compare_snapshots(db, baseline, validator.collect_snapshot(db))

    assert result['failures'] == ['NEW_ANALYSIS_AFTER_BASELINE: no new analysis is available to validate']


def test_compare_checks_the_global_ranking_for_the_latest_scan(db):
    create_analysis(db, payload())
    db.commit()
    baseline = validator.collect_snapshot(db)
    latest = create_analysis(db, payload())
    snapshot = db.scalar(select(VideoSnapshot).where(VideoSnapshot.analysis_id == latest.id))
    snapshot.overall_rank = 999
    db.commit()

    result = validator.compare_snapshots(db, baseline, validator.collect_snapshot(db))

    assert 'I: current analysis ranking is not global for its channel' in result['failures']
