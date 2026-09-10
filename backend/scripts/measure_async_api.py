"""Isolated image smoke/memory check; no secrets, database or broker required."""
import io
import json
import resource
import sys
import wave
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from fastapi.testclient import TestClient
from app import main
from app.db import get_db

assert 'faster_whisper' not in sys.modules
assert 'app.whisper' not in sys.modules
import_mib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
main.app.dependency_overrides[get_db] = lambda: None
main.process_audio = lambda analysis_id, tiktok_id, data, db: {
    'status': 'queued', 'audio_received': True, 'bytes': len(data)}
buf = io.BytesIO()
with wave.open(buf, 'wb') as stream:
    stream.setnchannels(2)
    stream.setsampwidth(4)
    stream.setframerate(96000)
    stream.writeframes(b'\0' * (12 * 96000 * 8))
blob = buf.getvalue()
with TestClient(main.app) as client:
    assert client.get('/health').status_code == 200
    assert client.get('/ready').status_code == 503  # no DB config supplied
    def upload(index):
        response = client.post('/api/v1/analyses/00000000-0000-0000-0000-000000000001/videos/10000/audio',
                               files={'audio':('untrusted.wav',blob,'audio/wav')})
        assert response.status_code == 202 and response.json()['bytes'] == len(blob)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(upload,range(2)))
assert 'faster_whisper' not in sys.modules and 'app.whisper' not in sys.modules
print(json.dumps({'api_import_max_rss_mib':import_mib,
                  'two_multipart_requests_max_rss_mib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024,
                  'wav_bytes_each':len(blob), 'whisper_imported':False,
                  'cgroup_memory_peak_bytes':Path('/sys/fs/cgroup/memory.peak').read_text().strip()},indent=2))
