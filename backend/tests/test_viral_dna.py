"""Phase A deterministic, global Viral DNA behaviour."""
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session

from app.models import (Analysis, AudioAssessment, Base, Channel, Transcript, Video,
                        VideoSnapshot, ViralDNA)
from app.viral_dna import (CURRENT_CLASSIFIER, CURRENT_CLASSIFIER_VERSION,
                           compute_deterministic_features, count_transcript_words,
                           extract_viral_dna_for_video, select_global_audio_assessment,
                           upsert_viral_dna)
from ops.backfill_viral_dna import backfill_viral_dna


def make_video(db, *, caption='Public caption', duration=39.0):
    channel = Channel(platform='tiktok', username=f'creator-{uuid4()}', nickname='Creator')
    db.add(channel); db.flush()
    video = Video(
        channel_id=channel.id, tiktok_id=str(uuid4().int)[:30], author='creator', nickname='Creator',
        caption=caption, published_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        duration=duration, url='https://www.tiktok.com/@creator/video/1', enrichment_status='missing',
    )
    db.add(video); db.flush()
    return video


def add_transcript(db, video, *, text='Hola mundo', duration=2.0):
    transcript = Transcript(video_id=video.id, text=text, language='es', duration=duration, model='small')
    db.add(transcript); db.flush()
    return transcript


def add_assessment(db, video, *, classifier='legacy', classifier_version='1', model_sha256='a' * 64):
    assessment = AudioAssessment(
        video_id=video.id, classifier=classifier, classifier_version=classifier_version,
        model_sha256=model_sha256, classification='speech', speech_score=.9, music_score=.1,
        singing_score=0, speech_patch_ratio=.9, music_patch_ratio=.1, singing_patch_ratio=0,
    )
    db.add(assessment); db.flush()
    return assessment


def test_video_without_transcript_or_audio_uses_global_null_assets(db):
    video = make_video(db)
    result = extract_viral_dna_for_video(db, video)
    row = result.row
    assert result.action == 'inserted'
    assert row.duration_seconds == 39.0
    assert row.caption_present and row.caption_char_count == len('Public caption')
    assert row.transcript_id is None and row.transcript_word_count is None
    assert row.audio_assessment_id is None
    assert row.semantic_status == 'skipped_no_transcript'


def test_video_with_transcript_populates_deterministic_fields(db):
    video = make_video(db)
    transcript = add_transcript(db, video, text='Hola mundo cruel', duration=1.5)
    row = extract_viral_dna_for_video(db, video).row
    assert row.transcript_id == transcript.id
    assert row.transcript_word_count == 3
    assert row.transcript_duration_seconds == 1.5
    assert row.words_per_second == Decimal('2.0000')
    assert row.semantic_status == 'not_requested'


def test_whitespace_caption_is_absent_and_counts_after_strip(db):
    video = make_video(db, caption=' \n\t ')
    row = extract_viral_dna_for_video(db, video).row
    assert not row.caption_present and row.caption_char_count == 0


def test_unicode_word_rule_handles_spanish_punctuation_and_whitespace():
    # Unicode alphanumeric runs are words; punctuation and whitespace separate them.
    assert count_transcript_words("¡Hola, corazón! ¿Cómo estás?\nniño's café — mañana.") == 7
    assert count_transcript_words('uno   dos\n\ttres') == 3


def test_empty_transcript_is_present_but_has_zero_words(db):
    video = make_video(db)
    add_transcript(db, video, text='', duration=3)
    row = extract_viral_dna_for_video(db, video).row
    assert row.transcript_word_count == 0
    assert row.words_per_second == Decimal('0.0000')
    assert row.semantic_status == 'not_requested'


def test_zero_transcript_duration_has_no_words_per_second(db):
    video = make_video(db)
    add_transcript(db, video, text='uno dos', duration=0)
    assert extract_viral_dna_for_video(db, video).row.words_per_second is None


def test_words_per_second_is_quantized_exactly_to_numeric_scale(db):
    video = make_video(db)
    add_transcript(db, video, text='uno dos tres cuatro cinco seis', duration=2.5)
    assert extract_viral_dna_for_video(db, video).row.words_per_second == Decimal('2.4000')


def test_audio_assessment_selection_is_current_config_then_lexical_fallback(db):
    video = make_video(db)
    zulu = add_assessment(db, video, classifier='zulu', classifier_version='1')
    alpha = add_assessment(db, video, classifier='alpha', classifier_version='9', model_sha256='b' * 64)
    assert select_global_audio_assessment(db, video.id).id == alpha.id
    current = add_assessment(
        db, video, classifier=CURRENT_CLASSIFIER, classifier_version=CURRENT_CLASSIFIER_VERSION,
        model_sha256='c' * 64,
    )
    assert select_global_audio_assessment(db, video.id).id == current.id
    assert zulu.id != current.id


def test_hash_is_stable_for_same_inputs_and_changes_for_caption_or_transcript(db):
    video = make_video(db, caption='same')
    transcript = add_transcript(db, video, text='texto uno', duration=2)
    first = compute_deterministic_features(video, transcript, None)
    assert first.deterministic_input_sha256 == compute_deterministic_features(video, transcript, None).deterministic_input_sha256
    video.caption = 'changed'
    second = compute_deterministic_features(video, transcript, None)
    assert second.deterministic_input_sha256 != first.deterministic_input_sha256
    video.caption = 'same'; transcript.text = 'texto dos'
    assert compute_deterministic_features(video, transcript, None).deterministic_input_sha256 != first.deterministic_input_sha256


def test_hash_includes_selected_audio_assessment_provenance(db):
    video = make_video(db)
    first_assessment = add_assessment(db, video, classifier='alpha', classifier_version='1')
    second_assessment = add_assessment(db, video, classifier='beta', classifier_version='1', model_sha256='b' * 64)
    first = compute_deterministic_features(video, None, first_assessment)
    second = compute_deterministic_features(video, None, second_assessment)
    assert first.deterministic_input_sha256 != second.deterministic_input_sha256


def test_same_input_is_idempotent_and_creates_one_row(db):
    video = make_video(db)
    assert extract_viral_dna_for_video(db, video).action == 'inserted'
    assert extract_viral_dna_for_video(db, video).action == 'unchanged'
    assert db.scalar(select(func.count()).select_from(ViralDNA)) == 1


def test_changed_input_updates_same_version_instead_of_creating_second_row(db):
    video = make_video(db, caption='first')
    first = extract_viral_dna_for_video(db, video).row
    video.caption = 'second'
    changed = extract_viral_dna_for_video(db, video)
    assert changed.action == 'updated' and changed.row.id == first.id
    assert db.scalar(select(func.count()).select_from(ViralDNA)) == 1


def test_concurrent_insert_conflict_keeps_winner_features_and_loser_session_usable(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'viral-dna-race.sqlite'}")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as setup:
            video = make_video(setup, caption='winner caption', duration=10.0)
            transcript = add_transcript(setup, video, text='uno dos tres', duration=1.5)
            assessment = add_assessment(setup, video)
            video_id = video.id
            setup.commit()

        winner = Session(engine)
        loser = Session(engine)
        try:
            winner_video = winner.get(Video, video_id)
            winner_features = compute_deterministic_features(
                winner_video,
                winner.scalar(select(Transcript).where(Transcript.video_id == video_id)),
                winner.scalar(select(AudioAssessment).where(AudioAssessment.video_id == video_id)),
            )

            loser_video = loser.get(Video, video_id)
            loser_video.caption = '   '
            loser_video.duration = 99.0
            loser_features = compute_deterministic_features(loser_video, None, None)
            loser.refresh(loser_video)

            def insert_winner_before_loser_insert(session, flush_context, instances):
                winner_result = upsert_viral_dna(winner, video_id, winner_features)
                assert winner_result.action == 'inserted'
                winner.commit()

            event.listen(loser, 'before_flush', insert_winner_before_loser_insert, once=True)
            loser_result = upsert_viral_dna(loser, video_id, loser_features)

            assert loser_result.action == 'unchanged'
            assert loser.scalar(select(func.count()).select_from(ViralDNA)) == 1
            final = loser.scalar(select(ViralDNA).where(ViralDNA.video_id == video_id))
            assert final is not None
            assert final.deterministic_input_sha256 == winner_features.deterministic_input_sha256
            for field, winner_value in winner_features.values().items():
                assert getattr(final, field) == winner_value
                assert getattr(final, field) != loser_features.values()[field]
            assert loser.scalar(select(ViralDNA.id).where(ViralDNA.video_id == video_id)) == final.id
        finally:
            winner.close()
            loser.close()
    finally:
        engine.dispose()


def test_new_extractor_version_creates_a_new_row(db):
    video = make_video(db)
    extract_viral_dna_for_video(db, video)
    extract_viral_dna_for_video(db, video, extractor_version='viral-dna-v2')
    assert db.scalar(select(func.count()).select_from(ViralDNA)) == 2


def test_multiple_analysis_snapshots_share_one_global_viral_dna_row(db):
    video = make_video(db)
    for rank in (1, 2):
        analysis = Analysis(channel_id=video.channel_id, status='awaiting_audio', video_count=1,
                            median_views=Decimal('10'), requested_transcripts=0, completed_transcripts=0)
        db.add(analysis); db.flush()
        db.add(VideoSnapshot(
            analysis_id=analysis.id, video_id=video.id, views=rank, likes=0, comments=0, shares=0,
            favorites=0, like_rate=0, comment_rate=0, share_rate=0, favorite_rate=0,
            engagement_rate=0, outlier_score=0, overall_rank=rank, transcription_rank=None,
            transcript_eligible=False, transcript_skip_reason=None,
        ))
    db.flush()
    extract_viral_dna_for_video(db, video)
    extract_viral_dna_for_video(db, video)
    assert db.scalar(select(func.count()).select_from(ViralDNA)) == 1
    assert {foreign_key.column.table.name for foreign_key in ViralDNA.__table__.foreign_keys} == {
        'videos', 'transcripts', 'audio_assessments'
    }


def test_viral_dna_does_not_copy_dynamic_metrics(db):
    dynamic = {'views', 'likes', 'comments', 'shares', 'favorites', 'like_rate', 'comment_rate',
               'share_rate', 'favorite_rate', 'engagement_rate', 'median_views', 'outlier_score'}
    assert not (dynamic & set(ViralDNA.__table__.columns.keys()))


def test_semantic_status_is_only_phase_a_statuses(db):
    without_transcript = make_video(db)
    with_transcript = make_video(db)
    add_transcript(db, with_transcript)
    assert extract_viral_dna_for_video(db, without_transcript).row.semantic_status == 'skipped_no_transcript'
    assert extract_viral_dna_for_video(db, with_transcript).row.semantic_status == 'not_requested'


def test_backfill_is_repeatable_without_duplicate_rows(db):
    make_video(db); video_with_transcript = make_video(db)
    add_transcript(db, video_with_transcript)
    first = backfill_viral_dna(db, batch_size=1, commit=False)
    second = backfill_viral_dna(db, batch_size=1, commit=False)
    assert first == {'processed': 2, 'inserted': 2, 'updated': 0, 'unchanged': 0, 'failed': 0}
    assert second == {'processed': 2, 'inserted': 0, 'updated': 0, 'unchanged': 2, 'failed': 0}
    assert db.scalar(select(func.count()).select_from(ViralDNA)) == 2
