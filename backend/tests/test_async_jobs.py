from datetime import timedelta
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4
import pytest
from fastapi import HTTPException
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError

from app.config import get_settings, Settings
from app.models import AudioAssessment, TranscriptionJob, Transcript, Video, now
from app.services import create_analysis, acquisition_batch, analysis_response
from app.jobs import (receive_audio, requeue_pending_jobs, cleanup_audio, inbox_lock, capacity,
                      atomic_store, job_path)
from app.workers.whisper import process_job, handle_delivery
from test_corpus import payload, wav, complete


@pytest.fixture
def received(db):
    a = create_analysis(db, payload((39.,), [100])); db.commit()
    receive_audio(a.id, '10000', wav(), db, lambda job: None)
    return db.scalar(select(TranscriptionJob))


def service(text='Global transcript'):
    return SimpleNamespace(transcribe=lambda path: dict(text=text, language='es', duration=8., model='small'))


def classifier(kind='music'):
    from app.yamnet import VERSION
    calls=[]
    def classify(path):
        calls.append(path)
        return dict(classifier='yamnet',classifier_version=VERSION,model_sha256='a'*64,
                    classification=kind,speech_score=.1,music_score=.9 if kind=='music' else .1,singing_score=.1,
                    speech_patch_ratio=0.,music_patch_ratio=1. if kind=='music' else 0.,singing_patch_ratio=0.,
                    top_classes=[{'label':'Music' if kind=='music' else 'Speech','score':.9}],processing_ms=7)
    return SimpleNamespace(classify=classify,calls=calls)


def gate_settings(mode): return SimpleNamespace(audio_gate_mode=mode, transcription_max_attempts=3, max_audio_mb=10)


def test_assessment_global_reused_and_observe_still_transcribes(db, received):
    c=classifier('music'); asr=service()
    assert process_job(db,received.id,received.video_id,asr,c,gate_settings('observe'))=='ack'
    assessment=db.scalar(select(AudioAssessment)); assert assessment.classification=='music' and len(c.calls)==1
    assert received.status=='completed' and db.scalar(select(Transcript))
    # A redelivery sees the transcript before classifier/ASR.
    assert process_job(db,received.id,received.video_id,SimpleNamespace(transcribe=lambda _:pytest.fail()),c,gate_settings('observe'))=='ack'
    assert len(c.calls)==1


def test_enforce_music_skips_after_persist_and_cleanup(db, received):
    c=classifier('music'); path=job_path(received)
    assert process_job(db,received.id,received.video_id,SimpleNamespace(transcribe=lambda _:pytest.fail()),c,gate_settings('enforce'))=='ack'
    assert received.status=='skipped' and received.skip_reason=='music' and received.assessment_id and not path.exists()
    assert db.scalar(select(AudioAssessment)).classification=='music' and not db.scalar(select(Transcript))


@pytest.mark.parametrize('kind',['speech','mixed','ambiguous','singing'])
def test_enforce_non_music_fails_open_to_whisper(db, received, kind):
    assert process_job(db,received.id,received.video_id,service(),classifier(kind),gate_settings('enforce'))=='ack'
    assert received.status=='completed' and db.scalar(select(Transcript))


def test_classifier_failure_fails_open_to_lazy_whisper(db, received):
    c=SimpleNamespace(classify=lambda _:(_ for _ in ()).throw(RuntimeError('bad model')))
    assert process_job(db,received.id,received.video_id,service(),c,gate_settings('enforce'))=='ack'
    assert received.status=='completed' and received.classifier_error_code=='yamnet_failed' and db.scalar(select(Transcript))


def test_redelivery_reuses_committed_assessment_before_retrying_whisper(db, received):
    c=classifier('speech'); attempts=[]
    def asr(_):
        attempts.append(1)
        if len(attempts)==1: raise TimeoutError()
        return dict(text='ok',language='es',duration=8.,model='small')
    assert process_job(db,received.id,received.video_id,SimpleNamespace(transcribe=asr),c,gate_settings('observe'))=='retry'
    assert db.scalar(select(AudioAssessment)) and len(c.calls)==1
    assert process_job(db,received.id,received.video_id,SimpleNamespace(transcribe=asr),c,gate_settings('observe'))=='ack'
    assert len(c.calls)==1 and received.status=='completed'


def test_default_batch_and_legacy_alias():
    assert Settings().acquisition_batch_size == 10
    assert Settings(initial_enrichment_budget=3).acquisition_batch_size == 3


def test_unique_job(db, received):
    db.add(TranscriptionJob(video_id=received.video_id))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    assert db.scalar(select(func.count()).select_from(TranscriptionJob)) == 1


def test_transcript_bypasses_new_job(db):
    a = create_analysis(db, payload((39.,), [100])); db.commit()
    video = db.scalar(select(Video))
    job = db.scalar(select(TranscriptionJob)); db.delete(job)
    complete(db, video); db.commit()
    create_analysis(db, payload((39.,), [100]))
    assert db.scalar(select(func.count()).select_from(TranscriptionJob)) == 0
    assert receive_audio(a.id, '10000', b'invalid', db)['status'] == 'already_transcribed'


def test_active_job_reused_after_lease_expiry(db, received):
    video = db.get(Video, received.video_id)
    video.enrichment_lease_until = now() - timedelta(seconds=1); db.commit()
    a = create_analysis(db, payload((39.,), [100])); db.commit()
    assert analysis_response(db, a)['enrichment_requests'] == []
    assert db.scalar(select(func.count()).select_from(TranscriptionJob)) == 1


def test_expired_reacquires_same_job(db):
    a = create_analysis(db, payload((39.,), [100])); db.commit()
    job = db.scalar(select(TranscriptionJob)); original = job.id
    video = db.get(Video, job.video_id)
    video.enrichment_lease_until = now() - timedelta(seconds=1); db.commit()
    requeue_pending_jobs(db, lambda j: None)
    assert job.status == 'expired'
    second = create_analysis(db, payload((39.,), [100])); db.commit()
    assert job.id == original and job.status == 'reserved'
    assert analysis_response(db, second)['enrichment_requests']


def test_publish_failure_and_reconciliation(db):
    a = create_analysis(db, payload((39.,), [100])); db.commit()
    def fail(job): raise RuntimeError('broker down')
    response = receive_audio(a.id, '10000', wav(), db, fail)
    job = db.scalar(select(TranscriptionJob))
    assert response['audio_received'] and job.status == 'audio_received' and job.queued_at is None
    assert job_path(job).exists()
    calls=[]
    assert requeue_pending_jobs(db, lambda j: calls.append(j.id)) == 1
    assert calls == [job.id] and job.status == 'queued'
    assert requeue_pending_jobs(db, lambda j: calls.append(j.id)) == 0


def test_message_ids_only_confirms_persistent(monkeypatch, received):
    from app import queue
    connection = MagicMock(); monkeypatch.setattr(queue, 'connect', lambda: connection)
    queue.publish_job(received)
    channel = connection.channel.return_value
    channel.confirm_delivery.assert_called_once()
    channel.queue_declare.assert_called_once_with(queue=queue.QUEUE, durable=True, arguments={'x-max-priority':100})
    message = channel.basic_publish.call_args.kwargs
    assert json.loads(message['body']) == {'job_id':str(received.id), 'video_id':str(received.video_id)}
    assert b'RIFF' not in message['body'] and len(message['body']) < 256
    assert message['properties'].delivery_mode == 2 and message['mandatory']


def test_atomic_order_and_partial_cleanup(db, monkeypatch):
    from app import jobs
    a = create_analysis(db, payload((39.,), [100])); db.commit()
    seen=[]; real = os.replace
    def replace(part, target):
        assert part.suffix == '.part' and part.read_bytes() == wav()
        assert not target.exists()
        seen.append(True); real(part, target)
    monkeypatch.setattr(jobs.os, 'replace', replace)
    receive_audio(a.id, '10000', wav(), db, lambda j: None)
    assert seen


@pytest.mark.parametrize('stage', ['fsync', 'replace'])
def test_partial_failure_cleans(db, monkeypatch, stage):
    from app import jobs
    a = create_analysis(db, payload((39.,), [100])); db.commit()
    def fail(*args): raise OSError('disk failure')
    monkeypatch.setattr(jobs.os, stage, fail)
    with pytest.raises(OSError): receive_audio(a.id, '10000', wav(), db, lambda j: None)
    db.rollback()
    assert not list(Path(get_settings().audio_queue_dir).glob('*.part'))
    assert db.scalar(select(TranscriptionJob)).status == 'reserved'


def test_rename_before_commit_recovered(db):
    create_analysis(db, payload((39.,), [100])); db.commit()
    job = db.scalar(select(TranscriptionJob))
    with inbox_lock() as root: atomic_store(root, job, wav())
    assert requeue_pending_jobs(db, lambda j: None) == 1
    assert job.status == 'queued' and job.audio_received_at


def test_size_validation(db, monkeypatch):
    monkeypatch.setenv('MAX_AUDIO_MB', '1'); get_settings.cache_clear()
    a = create_analysis(db, payload((39.,), [100])); db.commit()
    with pytest.raises(HTTPException) as exc: receive_audio(a.id, '10000', wav(40), db)
    assert exc.value.status_code == 413


def test_duplicate_delivery_and_cleanup(db, received):
    path = job_path(received)
    assert process_job(db, received.id, received.video_id, service()) == 'ack'
    assert received.status == 'completed' and not path.exists()
    assert process_job(db, received.id, received.video_id, SimpleNamespace(transcribe=lambda p: pytest.fail('Duplicate inference'))) == 'ack'
    assert db.scalar(select(func.count()).select_from(Transcript)) == 1


def test_terminal_failure_retains_bounded_audio(db, received):
    def fail(path): raise TimeoutError()
    for attempt in range(1,4):
        outcome = process_job(db, received.id, received.video_id, SimpleNamespace(transcribe=fail))
        assert received.attempts == attempt
        assert outcome == ('ack' if attempt == 3 else 'retry')
    assert received.status == 'failed' and received.failed_at and job_path(received).exists()
    assert process_job(db, received.id, received.video_id, service()) == 'ack'
    assert db.scalar(select(func.count()).select_from(Transcript)) == 0


def test_cleanup_failure_recorded_before_ack(db, received, monkeypatch):
    path = job_path(received); real = Path.unlink
    def fail(p, *args, **kwargs):
        if p == path: raise OSError('busy')
        return real(p, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', fail)
    assert process_job(db, received.id, received.video_id, service()) == 'ack'
    db.expire_all()
    assert received.last_error_code == 'cleanup_pending' and received.status == 'completed'
    assert db.scalar(select(Transcript))


@pytest.mark.parametrize('setting,value', [('AUDIO_QUEUE_MAX_BYTES','1'),('AUDIO_QUEUE_MIN_FREE_BYTES',str(2**63)),('AUDIO_QUEUE_MAX_JOBS','1')])
def test_capacity_http_retryable(db, received, monkeypatch, setting, value):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.db import get_db
    monkeypatch.setenv(setting,value); get_settings.cache_clear()
    def session(): yield db
    app.dependency_overrides[get_db] = session
    try:
        a = db.get(Video,received.video_id).enrichment_analysis_id
        response = TestClient(app).post(f'/api/v1/analyses/{a}/acquisition-batches')
        assert response.status_code == 503
        assert response.json() == {'code':'audio_queue_capacity','retryable':True}
    finally: app.dependency_overrides.clear()


def test_capacity_projects_incoming_bytes(db, monkeypatch):
    monkeypatch.setenv('AUDIO_QUEUE_MAX_BYTES','100'); get_settings.cache_clear()
    with inbox_lock() as root:
        with pytest.raises(HTTPException): capacity(db, root, incoming=100)


@pytest.mark.parametrize('state,age,deleted', [('completed',0,True),('failed',25,True),('failed',1,False),('queued',72,False),('processing',72,False)])
def test_cleanup_states(db, received, state, age, deleted):
    path=job_path(received)
    received.status=state; received.failed_at=now()-timedelta(hours=age); db.commit()
    cleanup_audio(db)
    assert path.exists() is not deleted


def test_cleanup_orphan_and_part(db):
    with inbox_lock() as root:
        stale = root / f'{uuid4()}.part'; stale.write_bytes(b'partial')
        orphan = root / f'{uuid4()}.wav'; orphan.write_bytes(wav())
        fresh = root / f'{uuid4()}.part'; fresh.write_bytes(b'partial')
        for p in (stale, orphan): os.utime(p,(0,0))
    cleanup_audio(db)
    assert not stale.exists() and not orphan.exists() and fresh.exists()


def test_next_batch_excludes_received_and_existing_reservations(db, monkeypatch):
    monkeypatch.setenv('ACQUISITION_BATCH_SIZE','2'); get_settings.cache_clear()
    a=create_analysis(db,payload()); db.commit()
    initial=analysis_response(db,a)['enrichment_requests']
    assert len(initial)==2
    for request in initial: receive_audio(a.id, request['tiktok_id'], wav(), db, lambda j: None)
    second=acquisition_batch(db,a.id)
    assert len(second['enrichment_requests'])==2
    assert not set(r['tiktok_id'] for r in initial) & set(r['tiktok_id'] for r in second['enrichment_requests'])
    assert len(acquisition_batch(db,a.id)['enrichment_requests'])==2


def test_ack_after_commits(db, received, monkeypatch):
    from concurrent.futures import Future
    from app.workers import whisper as worker
    events=[]
    class Executor:
        def submit(self, func):
            f=Future()
            f.set_result(process_job(db,received.id,received.video_id,service()))
            events.append('committed')
            return f
    channel=MagicMock()
    def ack(**kwargs):
        assert received.status == 'completed' and received.audio_path is None
        assert db.scalar(select(Transcript))
        events.append('ack')
    channel.basic_ack.side_effect=ack
    worker.handle_delivery(channel,SimpleNamespace(delivery_tag=1),json.dumps({'job_id':str(received.id),'video_id':str(received.video_id)}).encode(),Executor(),MagicMock())
    assert events==['committed','ack']


@pytest.mark.parametrize('outcome,requeue',[('retry',True),('reject',False)])
def test_manual_nack(outcome,requeue):
    from concurrent.futures import Future
    f=Future(); f.set_result(outcome)
    executor=MagicMock(); executor.submit.return_value=f
    channel=MagicMock()
    handle_delivery(channel,SimpleNamespace(delivery_tag=3),json.dumps({'job_id':str(uuid4()),'video_id':str(uuid4())}).encode(),executor,MagicMock())
    channel.basic_nack.assert_called_once_with(delivery_tag=3,requeue=requeue)
    channel.basic_ack.assert_not_called()


def test_uncertain_db_failure_never_ack():
    from concurrent.futures import Future
    f=Future(); f.set_exception(RuntimeError('DB down'))
    executor=MagicMock(); executor.submit.return_value=f; channel=MagicMock()
    with pytest.raises(RuntimeError):
        handle_delivery(channel,SimpleNamespace(delivery_tag=3),json.dumps({'job_id':str(uuid4()),'video_id':str(uuid4())}).encode(),executor,MagicMock())
    channel.basic_ack.assert_not_called(); channel.basic_nack.assert_not_called()


def test_lost_singleton_connection_stops_delivery_without_ack():
    from concurrent.futures import Future
    future=Future()
    executor=MagicMock(); executor.submit.return_value=future
    channel=MagicMock()
    with pytest.raises(RuntimeError, match='ownership'):
        handle_delivery(channel, SimpleNamespace(delivery_tag=3),
                        json.dumps({'job_id':str(uuid4()),'video_id':str(uuid4())}).encode(),
                        executor, MagicMock(), lambda: (_ for _ in ()).throw(RuntimeError('ownership lost')))
    channel.basic_ack.assert_not_called(); channel.basic_nack.assert_not_called()


def test_credentials_reject_other_apps():
    with pytest.raises(RuntimeError, match='configuration'):
        Settings(rabbitmq_url='amqp://n8n:private@rabbit_mq:5672/default').resolve_rabbitmq_url()
    assert Settings(rabbitmq_url='amqp://kurukin_tiktok:test@rabbit_mq:5672/%2Fkurukin-tiktok').resolve_rabbitmq_url()


def test_one_lazy_model_instance(monkeypatch):
    import sys
    from app.whisper import model_process
    models=[]; paths=iter(['one.wav','two.wav'])
    class Model:
        def __init__(self,*args,**kwargs): models.append(self)
        def transcribe(self,path,**kwargs):
            return [SimpleNamespace(text='speech')], SimpleNamespace(language='es',duration=8.)
    class Pipe:
        def recv(self):
            try: return next(paths)
            except StopIteration: raise EOFError()
        def send(self,result): assert result[0]=='ok'
        def close(self): pass
    monkeypatch.setitem(sys.modules,'faster_whisper',SimpleNamespace(WhisperModel=Model))
    model_process(Pipe(),get_settings())
    assert len(models)==1


def test_upload_duplicate_preserves_audio_and_attempts(db, received):
    video=db.get(Video,received.video_id)
    path=job_path(received)
    before=path.stat().st_mtime_ns
    response=receive_audio(video.enrichment_analysis_id,video.tiktok_id,b'invalid retry',db,
                           lambda job: pytest.fail('Already published'))
    assert response['audio_received'] and response['job_id']==str(received.id)
    assert path.stat().st_mtime_ns==before and received.attempts==0


def test_worker_final_wav_validation(db,received):
    job_path(received).write_bytes(b'bad wav')
    assert process_job(db,received.id,received.video_id,SimpleNamespace(transcribe=lambda p:pytest.fail('Invalid WAV'))) == 'retry'
    assert not db.scalar(select(Transcript))


def test_completed_stale_audio_reused(db,received):
    complete(db,db.get(Video,received.video_id)); db.commit()
    path=job_path(received)
    assert process_job(db,received.id,received.video_id,SimpleNamespace(transcribe=lambda p:pytest.fail('Cached'))) == 'ack'
    assert received.status=='completed' and not path.exists()


def test_mismatched_message_never_processes(db,received):
    assert process_job(db,received.id,uuid4(),service())=='reject'
    assert job_path(received).exists()


def test_capped_initial_reservations(db,monkeypatch):
    monkeypatch.setenv('AUDIO_QUEUE_MAX_JOBS','2'); get_settings.cache_clear()
    a=create_analysis(db,payload()); db.commit()
    assert len(analysis_response(db,a)['enrichment_requests'])==2


def test_rabbit_bootstrap_isolated(tmp_path):
    import subprocess
    from test_migrations_ops import ROOT
    fake=tmp_path/'docker'
    log=tmp_path/'calls'
    fake.write_text('''#!/usr/bin/env python3
import os,sys,json,pathlib
args=sys.argv[1:]
with open(os.environ['CALL_LOG'],'a') as f: f.write(json.dumps(args)+'\\n')
if args[0]=='ps': print('fake-container')
elif 'list_vhosts' in args: print('default')
elif 'list_users' in args: print('root\\t[administrator]')
elif 'add_user' in args:
    data=sys.stdin.read()
    assert len(data.strip())==64
    assert args[-1]=='kurukin_tiktok'
''')
    fake.chmod(0o700)
    script=tmp_path/'bootstrap.sh'
    script.write_text((ROOT/'ops/bootstrap_rabbitmq.sh').read_text().replace('/root/',str(tmp_path)+'/'))
    env={**os.environ,'PATH':str(tmp_path)+os.pathsep+os.environ['PATH'],'CALL_LOG':str(log)}
    checked=__import__('subprocess').run(['bash',str(script),'--check'],env=env,capture_output=True,text=True)
    assert checked.returncode==0
    credential=tmp_path/'.kurukin-tiktok-rabbitmq-url'
    assert not credential.exists() and 'add_user' not in log.read_text()
    created=subprocess.run(['bash',str(script),'--create'],env=env,capture_output=True,text=True)
    assert created.returncode==0
    assert credential.stat().st_mode & 0o777==0o600
    password=credential.read_text().split(':')[2].split('@')[0]
    assert password not in created.stdout+created.stderr+log.read_text()
    calls=[json.loads(line) for line in log.read_text().splitlines()]
    mutations=[c for c in calls if any(x in c for x in ('add_user','add_vhost','set_permissions'))]
    assert len(mutations)==3
    assert all('default' not in c and 'root' not in c for c in mutations)
    assert 'secret' not in log.read_text()
    again=subprocess.run(['bash',str(script),'--create'],env=env,capture_output=True,text=True)
    assert again.returncode!=0  # A partial bootstrap never overwrites the credential.
    assert password in credential.read_text()


@pytest.mark.parametrize('setting,value', [('AUDIO_QUEUE_MAX_BYTES','1'),('AUDIO_QUEUE_MIN_FREE_BYTES',str(2**63)),('AUDIO_QUEUE_MAX_JOBS','1')])
def test_new_upload_capacity_http(db,monkeypatch,setting,value):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.db import get_db
    a=create_analysis(db,payload((39.,39.),[100,50])); db.commit()
    monkeypatch.setenv(setting,value); get_settings.cache_clear()
    def session(): yield db
    app.dependency_overrides[get_db]=session
    try:
        response=TestClient(app).post(f'/api/v1/analyses/{a.id}/videos/10000/audio',files={'audio':('x.wav',wav(),'audio/wav')})
        assert response.status_code==503
        assert response.json()=={'code':'audio_queue_capacity','retryable':True}
        assert not list(Path(get_settings().audio_queue_dir).glob('*.wav'))
    finally: app.dependency_overrides.clear()


def test_broker_down_still_http_202(db,monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.db import get_db
    from app import queue
    a=create_analysis(db,payload((39.,),[100])); db.commit()
    def fail(job): raise RuntimeError('broker unavailable')
    monkeypatch.setattr(queue,'publish_job',fail)
    def session(): yield db
    app.dependency_overrides[get_db]=session
    try:
        response=TestClient(app).post(f'/api/v1/analyses/{a.id}/videos/10000/audio',files={'audio':('x.wav',wav(),'audio/wav')})
        assert response.status_code==202
        assert response.json()['status']=='audio_received' and response.json()['audio_received']
    finally: app.dependency_overrides.clear()


def test_frozen_0001():
    import hashlib
    from test_migrations_ops import ROOT
    assert hashlib.sha256((ROOT/'alembic/versions/0001_global_corpus.py').read_bytes()).hexdigest() == '9e68000d4de9c49986d728545decec645e31f6198f8c0a7f7372f5ce4c6e9e4e'
