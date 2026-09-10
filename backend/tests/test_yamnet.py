import wave
import numpy as np
from app.yamnet import MODEL, CLASS_MAP, YamnetService, aggregate


def write_wav(path):
    with wave.open(str(path),'wb') as out:
        out.setnchannels(1); out.setsampwidth(2); out.setframerate(16000)
        out.writeframes(np.zeros(16000,dtype='<i2').tobytes())


def test_backend_yamnet_assets_are_pinned_and_local():
    import hashlib
    from app.yamnet import MODEL_SHA256
    assert MODEL.is_file() and CLASS_MAP.is_file()
    assert MODEL.stat().st_size==16093355
    assert hashlib.sha256(MODEL.read_bytes()).hexdigest()==MODEL_SHA256


def test_patch_aggregation_keeps_singing_out_of_speech():
    labels=['Speech','Singing','Music']+['x']*518
    scores=np.zeros((2,521),dtype=np.float32); scores[:,1]=.9
    result=aggregate(scores,labels)
    assert result['classification']=='singing' and result['speech_score']==0 and result['singing_patch_ratio']==1
    assert len(result['top_classes'])==3 and all(0<=row['score']<=1 for row in result['top_classes'])


def test_session_is_lazy_and_reused(tmp_path):
    calls=[]
    class Input: name='waveform'
    class Session:
        def get_inputs(self): return [Input()]
        def run(self, _, feeds):
            assert feeds['waveform'].dtype==np.float32
            scores=np.zeros((1,521),dtype=np.float32); scores[0,0]=.9
            return [scores]
    service=YamnetService(runtime_factory=lambda _:calls.append(1) or Session())
    path=tmp_path/'audio.wav'; write_wav(path)
    assert service.classify(path)['classification']=='speech'
    assert service.classify(path)['classification']=='speech'
    assert calls==[1] and service.cold_init_ms is not None
