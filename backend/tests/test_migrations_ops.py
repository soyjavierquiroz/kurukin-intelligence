import importlib.util
import io
import os
from pathlib import Path
import subprocess
import textwrap
import pytest
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.autogenerate import compare_metadata
from sqlalchemy import create_engine, inspect, text
from app.llm.semantic_contract import (
    SEMANTIC_ENUM_FIELDS, SEMANTIC_OUTPUT_FIELDS, SEMANTIC_SECONDARY_PRIMARY_PAIRS,
    semantic_secondary_constraint_name,
)
from app.models import Base

ROOT = Path(__file__).resolve().parents[1]


def test_initial_migration_matches_metadata_and_downgrade():
    spec=importlib.util.spec_from_file_location('initial', ROOT/'alembic/versions/0001_global_corpus.py')
    migration=importlib.util.module_from_spec(spec); spec.loader.exec_module(migration)
    with create_engine('sqlite://').connect() as connection:
        context=MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()
            assert set(inspect(connection).get_table_names()) == {'channels','analyses','videos','video_snapshots','transcripts'}
            spec2=importlib.util.spec_from_file_location('async_jobs', ROOT/'alembic/versions/0002_async_transcription_jobs.py')
            second=importlib.util.module_from_spec(spec2); spec2.loader.exec_module(second)
            second.upgrade()
            spec3=importlib.util.spec_from_file_location('audio_assessment', ROOT/'alembic/versions/0003_audio_assessment.py')
            third=importlib.util.module_from_spec(spec3); spec3.loader.exec_module(third)
            third.upgrade()
            spec4=importlib.util.spec_from_file_location('video_music_metadata', ROOT/'alembic/versions/0004_video_music_metadata.py')
            fourth=importlib.util.module_from_spec(spec4); spec4.loader.exec_module(fourth)
            fourth.upgrade()
            spec5=importlib.util.spec_from_file_location('global_incremental_channel_corpus', ROOT/'alembic/versions/0005_global_incremental_channel_corpus.py')
            fifth=importlib.util.module_from_spec(spec5); spec5.loader.exec_module(fifth)
            fifth.upgrade()
            spec6=importlib.util.spec_from_file_location('viral_dna', ROOT/'alembic/versions/0006_viral_dna.py')
            sixth=importlib.util.module_from_spec(spec6); spec6.loader.exec_module(sixth)
            sixth.upgrade()
            spec7=importlib.util.spec_from_file_location('semantic_viral_dna', ROOT/'alembic/versions/0007_semantic_viral_dna.py')
            seventh=importlib.util.module_from_spec(spec7); spec7.loader.exec_module(seventh)
            seventh.upgrade()
            spec8=importlib.util.spec_from_file_location('channel_intelligence_analysis', ROOT/'alembic/versions/0008_channel_intelligence_analysis.py')
            eighth=importlib.util.module_from_spec(spec8); spec8.loader.exec_module(eighth)
            eighth.upgrade()
            spec9=importlib.util.spec_from_file_location('private_content_packs', ROOT/'alembic/versions/0009_private_content_packs.py')
            ninth=importlib.util.module_from_spec(spec9); spec9.loader.exec_module(ninth)
            ninth.upgrade()
            spec10=importlib.util.spec_from_file_location('knowledge_strategist', ROOT/'alembic/versions/0010_knowledge_strategist.py')
            tenth=importlib.util.module_from_spec(spec10); spec10.loader.exec_module(tenth)
            tenth.upgrade()
            assert compare_metadata(context, Base.metadata) == []
            tenth.downgrade()
            ninth.downgrade()
            eighth.downgrade()
            seventh.downgrade()
            sixth.downgrade()
            fifth.downgrade()
            fourth.downgrade()
            third.downgrade()
            second.downgrade()
            migration.downgrade()
            assert inspect(connection).get_table_names() == []


def test_0003_to_0004_preserves_existing_video_transcript_and_assessment():
    def migration(name):
        spec = importlib.util.spec_from_file_location(name, ROOT/f'alembic/versions/{name}.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    first, second, third, fourth = (migration(name) for name in (
        '0001_global_corpus', '0002_async_transcription_jobs', '0003_audio_assessment',
        '0004_video_music_metadata'))
    with create_engine('sqlite://').begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            first.upgrade(); second.upgrade(); third.upgrade()
            ids = dict(channel='00000000-0000-0000-0000-000000000001',
                       analysis='00000000-0000-0000-0000-000000000002',
                       video='00000000-0000-0000-0000-000000000003',
                       transcript='00000000-0000-0000-0000-000000000004',
                       assessment='00000000-0000-0000-0000-000000000005')
            connection.execute(text("INSERT INTO channels (id, created_at, platform, username, nickname, updated_at) VALUES (:id, :at, 'tiktok', 'creator', 'Creator', :at)"), {**ids, 'id': ids['channel'], 'at': '2026-01-01 00:00:00'})
            connection.execute(text("INSERT INTO analyses (id, created_at, channel_id, status, video_count, median_views, requested_transcripts, completed_transcripts) VALUES (:id, :at, :channel_id, 'transcribed', 1, 100, 1, 1)"), {**ids, 'id': ids['analysis'], 'channel_id': ids['channel'], 'at': '2026-01-01 00:00:00'})
            connection.execute(text("INSERT INTO videos (id, created_at, channel_id, tiktok_id, author, nickname, caption, published_at, duration, url, enrichment_status, updated_at) VALUES (:id, :at, :channel_id, '7678773331130125582', 'creator', 'Creator', 'caption', :at, 39, 'https://www.tiktok.com/@creator/video/7678773331130125582', 'completed', :at)"), {**ids, 'id': ids['video'], 'channel_id': ids['channel'], 'at': '2026-01-01 00:00:00'})
            connection.execute(text("INSERT INTO transcripts (id, created_at, video_id, text, language, duration, model) VALUES (:id, :at, :video_id, 'existing transcript', 'es', 39, 'small')"), {**ids, 'id': ids['transcript'], 'video_id': ids['video'], 'at': '2026-01-01 00:00:00'})
            connection.execute(text("INSERT INTO audio_assessments (id, created_at, video_id, classifier, classifier_version, model_sha256, classification, speech_score, music_score, singing_score, speech_patch_ratio, music_patch_ratio, singing_patch_ratio, updated_at) VALUES (:id, :at, :video_id, 'yamnet', '1', 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'speech', 1, 0, 0, 1, 0, 0, :at)"), {**ids, 'id': ids['assessment'], 'video_id': ids['video'], 'at': '2026-01-01 00:00:00'})
            fourth.upgrade()
            assert [column['name'] for column in inspect(connection).get_columns('videos') if column['name'].startswith('music_')] == ['music_id', 'music_title', 'music_author', 'music_original']
            assert connection.scalar(text("SELECT caption FROM videos WHERE id = :id"), {'id': ids['video']}) == 'caption'
            assert connection.scalar(text("SELECT text FROM transcripts WHERE id = :id"), {'id': ids['transcript']}) == 'existing transcript'
            assert connection.scalar(text("SELECT classification FROM audio_assessments WHERE id = :id"), {'id': ids['assessment']}) == 'speech'


def test_0004_to_0005_preserves_corpus_and_backfills_acquisition_reference():
    def migration(name):
        spec = importlib.util.spec_from_file_location(name, ROOT/f'alembic/versions/{name}.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        return module
    first, second, third, fourth, fifth = (migration(name) for name in (
        '0001_global_corpus', '0002_async_transcription_jobs', '0003_audio_assessment',
        '0004_video_music_metadata', '0005_global_incremental_channel_corpus'))
    with create_engine('sqlite://').begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            first.upgrade(); second.upgrade(); third.upgrade(); fourth.upgrade()
            values = dict(channel='00000000-0000-0000-0000-000000000001',
                          analysis='00000000-0000-0000-0000-000000000002',
                          video='00000000-0000-0000-0000-000000000003', at='2026-01-01 00:00:00')
            connection.execute(text("INSERT INTO channels (id, created_at, platform, username, nickname, updated_at) VALUES (:channel, :at, 'tiktok', 'creator', 'Creator', :at)"), values)
            connection.execute(text("INSERT INTO analyses (id, created_at, channel_id, status, video_count, median_views, requested_transcripts, completed_transcripts) VALUES (:analysis, :at, :channel, 'awaiting_audio', 1, 100, 1, 0)"), values)
            connection.execute(text("INSERT INTO videos (id, created_at, channel_id, tiktok_id, author, nickname, caption, published_at, duration, url, enrichment_status, updated_at) VALUES (:video, :at, :channel, '7678773331130125582', 'creator', 'Creator', 'caption', :at, 39, 'https://www.tiktok.com/@creator/video/7678773331130125582', 'requested', :at)"), values)
            connection.execute(text("INSERT INTO video_snapshots (id, created_at, analysis_id, video_id, views, likes, comments, shares, favorites, like_rate, comment_rate, share_rate, favorite_rate, engagement_rate, outlier_score, overall_rank, transcription_rank, transcript_eligible) VALUES ('00000000-0000-0000-0000-000000000004', :at, :analysis, :video, 100, 1, 1, 1, 1, .01, .01, .01, .01, .04, 1, 1, 1, 1)"), values)
            fifth.upgrade()
            assert connection.scalar(text("SELECT first_seen_at FROM videos WHERE id = :video"), values) == values['at']
            assert connection.scalar(text("SELECT last_seen_at FROM videos WHERE id = :video"), values) == values['at']
            assert connection.scalar(text("SELECT COUNT(*) FROM analysis_acquisitions WHERE analysis_id = :analysis AND video_id = :video"), values) == 1
            assert connection.scalar(text("SELECT videos_new FROM analyses WHERE id = :analysis"), values) == 0


def test_0005_to_0006_preserves_existing_corpus_and_downgrades_only_viral_dna():
    def migration(name):
        spec = importlib.util.spec_from_file_location(name, ROOT/f'alembic/versions/{name}.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        return module
    first, second, third, fourth, fifth, sixth = (migration(name) for name in (
        '0001_global_corpus', '0002_async_transcription_jobs', '0003_audio_assessment',
        '0004_video_music_metadata', '0005_global_incremental_channel_corpus', '0006_viral_dna'))
    with create_engine('sqlite://').begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            first.upgrade(); second.upgrade(); third.upgrade(); fourth.upgrade(); fifth.upgrade()
            tables_before = set(inspect(connection).get_table_names())
            sixth.upgrade()
            assert set(inspect(connection).get_table_names()) == tables_before | {'viral_dna'}
            columns = {column['name'] for column in inspect(connection).get_columns('viral_dna')}
            assert columns == {
                'id', 'video_id', 'extractor_version', 'deterministic_input_sha256',
                'duration_seconds', 'caption_present', 'caption_char_count', 'transcript_id',
                'transcript_word_count', 'transcript_duration_seconds', 'words_per_second',
                'audio_assessment_id', 'semantic_status', 'created_at', 'updated_at',
            }
            sixth.downgrade()
            assert set(inspect(connection).get_table_names()) == tables_before


def test_0006_to_0007_adds_and_removes_only_semantic_viral_dna_fields():
    def migration(name):
        spec = importlib.util.spec_from_file_location(name, ROOT/f'alembic/versions/{name}.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        return module

    first, second, third, fourth, fifth, sixth, seventh = (migration(name) for name in (
        '0001_global_corpus', '0002_async_transcription_jobs', '0003_audio_assessment',
        '0004_video_music_metadata', '0005_global_incremental_channel_corpus', '0006_viral_dna',
        '0007_semantic_viral_dna'))
    semantic_columns = set(SEMANTIC_OUTPUT_FIELDS) | {
        'semantic_input_sha256', 'semantic_model', 'semantic_prompt_version', 'semantic_extracted_at',
    }
    semantic_constraints = {f'ck_viral_dna_{field}' for field in SEMANTIC_ENUM_FIELDS} | {
        semantic_secondary_constraint_name(primary, secondary)
        for primary, secondary in SEMANTIC_SECONDARY_PRIMARY_PAIRS
    }
    with create_engine('sqlite://').begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            first.upgrade(); second.upgrade(); third.upgrade(); fourth.upgrade(); fifth.upgrade(); sixth.upgrade()
            columns_before = {column['name'] for column in inspect(connection).get_columns('viral_dna')}
            sixth_constraints = {item['name'] for item in inspect(connection).get_check_constraints('viral_dna')}
            seventh.upgrade()
            assert {column['name'] for column in inspect(connection).get_columns('viral_dna')} == columns_before | semantic_columns
            assert {item['name'] for item in inspect(connection).get_check_constraints('viral_dna')} == sixth_constraints | semantic_constraints
            seventh.downgrade()
            assert {column['name'] for column in inspect(connection).get_columns('viral_dna')} == columns_before
            assert {item['name'] for item in inspect(connection).get_check_constraints('viral_dna')} == sixth_constraints


def test_0008_adds_and_removes_only_channel_intelligence_tables():
    def migration(name):
        spec = importlib.util.spec_from_file_location(name, ROOT/f'alembic/versions/{name}.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        return module

    migrations = [migration(name) for name in (
        '0001_global_corpus', '0002_async_transcription_jobs', '0003_audio_assessment',
        '0004_video_music_metadata', '0005_global_incremental_channel_corpus', '0006_viral_dna',
        '0007_semantic_viral_dna', '0008_channel_intelligence_analysis')]
    with create_engine('sqlite://').begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            for item in migrations[:-1]:
                item.upgrade()
            before = set(inspect(connection).get_table_names())
            migrations[-1].upgrade()
            assert set(inspect(connection).get_table_names()) == before | {
                'channel_intelligence_analyses', 'channel_video_intelligence'
            }
            assert {column['name'] for column in inspect(connection).get_columns('channel_intelligence_analyses')} >= {
                'research_pack_hash', 'schema_version', 'payload_sha256', 'channel_intelligence'
            }
            migrations[-1].downgrade()
            assert set(inspect(connection).get_table_names()) == before


def test_0009_adds_and_removes_only_private_content_packs_table():
    def migration(name):
        spec = importlib.util.spec_from_file_location(name, ROOT/f'alembic/versions/{name}.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        return module
    migrations = [migration(name) for name in (
        '0001_global_corpus', '0002_async_transcription_jobs', '0003_audio_assessment',
        '0004_video_music_metadata', '0005_global_incremental_channel_corpus', '0006_viral_dna',
        '0007_semantic_viral_dna', '0008_channel_intelligence_analysis', '0009_private_content_packs')]
    with create_engine('sqlite://').begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            for item in migrations[:-1]:
                item.upgrade()
            before = set(inspect(connection).get_table_names())
            migrations[-1].upgrade()
            assert set(inspect(connection).get_table_names()) == before | {'private_content_packs'}
            assert {column['name'] for column in inspect(connection).get_columns('private_content_packs')} >= {
                'channel_id', 'analysis_id', 'private_context', 'content_pack'
            }
            migrations[-1].downgrade()
            assert set(inspect(connection).get_table_names()) == before


def test_postgres_offline_sql_and_revision(monkeypatch):
    monkeypatch.setenv('DATABASE_URL', 'postgresql+psycopg://kurukin_tiktok:test-secret@postgres:5432/kurukin_tiktok')
    output=io.StringIO()
    command.upgrade(Config(str(ROOT/'alembic.ini'), output_buffer=output), 'head', sql=True)
    sql=output.getvalue()
    assert 'CREATE TABLE channels' in sql and 'CREATE TABLE transcripts' in sql
    assert 'UNIQUE (video_id)' in sql and 'UNIQUE (tiktok_id)' in sql
    assert 'CREATE TABLE viral_dna' in sql and 'uq_viral_dna_video_extractor_version' in sql
    assert 'semantic_input_sha256' in sql and 'ck_viral_dna_hook_type' in sql
    assert 'CREATE TABLE channel_intelligence_analyses' in sql and 'CREATE TABLE channel_video_intelligence' in sql
    assert 'CREATE TABLE private_content_packs' in sql
    assert 'test-secret' not in sql and 'n8n_v2_data' not in sql


@pytest.fixture
def mock_ops(tmp_path):
    # Run isolated COPIES only; all Docker calls are intercepted. No real bootstrap.
    bin_dir=tmp_path/'bin'; bin_dir.mkdir()
    state=tmp_path/'state'; state.write_text('f\nf\nf')
    fake=bin_dir/'docker'
    fake.write_text('''#!/usr/bin/env python3
import os, pathlib, sys
state=pathlib.Path(os.environ['MOCK_STATE'])
args=sys.argv[1:]
if args[0]=='ps':
    print('fake-container')
elif 'alembic' in args:
    print(os.environ.get('MOCK_OUTPUT',''), file=sys.stderr)
    sys.exit(int(os.environ.get('MOCK_FAIL','0')))
else:
    sql=sys.stdin.read()
    if 'CREATE DATABASE' in sql:
        assert 'DROP ' not in sql
        assert 'ALTER ROLE' not in sql
        assert 'ALTER DATABASE n8n_v2_data' not in sql
        state.write_text('t\\nt\\nt')
    else:
        print(state.read_text())
''')
    fake.chmod(0o700)
    env={**os.environ, 'PATH':str(bin_dir)+os.pathsep+os.environ['PATH'], 'MOCK_STATE':str(state)}
    bootstrap=tmp_path/'bootstrap.sh'
    bootstrap.write_text((ROOT/'ops/bootstrap_database.sh').read_text().replace('/root/',str(tmp_path)+'/'))
    return tmp_path, env, bootstrap, state


def test_bootstrap_check_safe_and_create_idempotent(mock_ops):
    root,env,script,state=mock_ops
    check=subprocess.run(['bash',str(script),'--check'],env=env,capture_output=True,text=True,check=True)
    assert 'role kurukin_tiktok: no' in check.stdout
    assert not (root/'.kurukin-tiktok-db-url').exists()
    first=subprocess.run(['bash',str(script),'--create'],env=env,capture_output=True,text=True,check=True)
    credential=root/'.kurukin-tiktok-db-url'
    content=credential.read_text()
    assert credential.stat().st_mode & 0o777 == 0o600
    assert content.startswith('postgresql+psycopg://kurukin_tiktok:')
    second=subprocess.run(['bash',str(script),'--create'],env=env,capture_output=True,text=True,check=True)
    assert credential.read_text()==content
    password=content.split(':')[2].split('@')[0]
    assert password not in first.stdout+first.stderr+second.stdout+second.stderr


def test_bootstrap_existing_role_does_not_generate_password(mock_ops):
    root,env,script,state=mock_ops
    state.write_text('t\nt\nt')
    result=subprocess.run(['bash',str(script),'--create'],env=env,capture_output=True,text=True,check=True)
    assert not (root/'.kurukin-tiktok-db-url').exists()
    assert 'password not changed' in result.stdout


def test_bootstrap_refuses_symlink(mock_ops):
    root,env,script,state=mock_ops
    (root/'.kurukin-tiktok-db-url').symlink_to(root/'elsewhere')
    result=subprocess.run(['bash',str(script),'--create'],env=env,capture_output=True,text=True)
    assert result.returncode != 0
    assert state.read_text()=='f\nf\nf'


@pytest.mark.parametrize('failure', [0,1,7])
def test_migration_script_aborts_on_failure(mock_ops,failure):
    _,env,_,_=mock_ops
    env['MOCK_FAIL']=str(failure)
    result=subprocess.run(['bash',str(ROOT/'ops/run_migrations.sh')],env=env,capture_output=True,text=True)
    assert result.returncode==failure


def test_wrapper_only_emits_fixed_safe_diagnostics(mock_ops):
    _, env, _, _ = mock_ops
    env['MOCK_FAIL'] = '7'
    env['MOCK_OUTPUT'] = '\n'.join([
        'postgresql+psycopg://user:do-not-print@postgres/db',
        'password=do-not-print', 'Migration error: OperationalError',
        'SQLSTATE: 28P01', 'Configuration field: whisper_concurrency',
        'Migration error: OperationalError password=do-not-print',
        'Configuration field: do-not-print', 'SQLSTATE: do-not-print',
    ])
    result = subprocess.run(['bash', str(ROOT/'ops/run_migrations.sh')], env=env, capture_output=True, text=True)
    assert result.returncode == 7
    assert 'do-not-print' not in result.stdout + result.stderr
    assert 'postgresql' not in result.stdout + result.stderr
    assert 'Migration error: OperationalError' in result.stderr
    assert 'SQLSTATE: 28P01' in result.stderr
    assert 'Configuration field: whisper_concurrency' in result.stderr


def test_safe_error_excludes_messages_inputs_and_sql():
    from app.migration_diagnostics import safe_migration_error
    from app.config import Settings
    from pydantic import ValidationError
    from sqlalchemy.exc import OperationalError
    try:
        Settings(whisper_concurrency='do-not-print')
    except ValidationError as exc:
        assert safe_migration_error(exc) == 'Migration error: ValidationError\nConfiguration field: whisper_concurrency'
    class DriverError(Exception):
        sqlstate = '28P01'
    exc = OperationalError('do-not-print', {'password':'do-not-print'}, DriverError('do-not-print'))
    assert safe_migration_error(exc) == 'Migration error: OperationalError\nSQLSTATE: 28P01'
    assert safe_migration_error(RuntimeError('do-not-print')) == 'Migration error: RuntimeError'


def test_alembic_config_error_is_safe_and_actionable(monkeypatch):
    monkeypatch.setenv('WHISPER_CONCURRENCY', 'do-not-print')
    with pytest.raises(SystemExit) as caught:
        command.current(Config(str(ROOT/'alembic.ini')))
    assert str(caught.value) == 'Migration error: ValidationError\nConfiguration field: whisper_concurrency'
