"""Pinned local YAMNet AudioSet gate; no media leaves the server."""
import csv
import hashlib
import time
import wave
from pathlib import Path

import numpy as np

MODEL_SHA256 = 'd3835ffbbd4a1bb3e777f0ca217b5007907f5171dd5d17c4236b95b2af8f908e'
CLASSIFIER = 'yamnet'
VERSION = 'tfhub-yamnet-1-tf2onnx-1.16.1'
CONFIG_VERSION = 'audio-classifier-config-v1'
PATCH_SCORE, STRONG, LOW = .35, .55, .20
ROOT = Path(__file__).with_name('audio_models')
MODEL = ROOT/'yamnet.onnx'
CLASS_MAP = ROOT/'yamnet_class_map.csv'
SPEECH = frozenset(('Speech','Child speech, kid speaking','Conversation','Narration, monologue','Whispering','Speech synthesizer','Chatter','Speech noise','Shout','Yell','Bellow','Children shouting'))
SINGING = frozenset(('Singing','Choir','Child singing','Synthetic singing','Yodeling','Chant','Mantra','Humming'))
RAPPING = frozenset(('Rapping',))


def _score(value):
    return max(0., min(1., float(value))) if np.isfinite(value) else 0.


def _indices(labels, names):
    return np.array([i for i, label in enumerate(labels) if label in names], dtype=np.intp)


class YamnetService:
    """A process-local lazy ONNX Runtime CPU session reused across deliveries."""
    def __init__(self, model=MODEL, class_map=CLASS_MAP, runtime_factory=None):
        self.model, self.class_map, self.runtime_factory = Path(model), Path(class_map), runtime_factory
        self._session = None
        self._labels = None
        self.cold_init_ms = None

    def _load(self):
        if self._session is not None:
            return self._session, self._labels
        started = time.perf_counter()
        if hashlib.sha256(self.model.read_bytes()).hexdigest() != MODEL_SHA256:
            raise RuntimeError('yamnet_model_checksum')
        with self.class_map.open(newline='', encoding='utf-8') as stream:
            labels = tuple(row['display_name'] for row in csv.DictReader(stream))
        if len(labels) != 521:
            raise RuntimeError('yamnet_class_map')
        if self.runtime_factory is None:
            import onnxruntime
            session = onnxruntime.InferenceSession(str(self.model), providers=['CPUExecutionProvider'])
        else:
            session = self.runtime_factory(str(self.model))
        self._session, self._labels = session, labels
        self.cold_init_ms = round((time.perf_counter()-started)*1000)
        return session, labels

    def classify(self, wav_path):
        started = time.perf_counter()
        with wave.open(str(wav_path), 'rb') as reader:
            if reader.getnchannels()!=1 or reader.getframerate()!=16000 or reader.getsampwidth()!=2 or reader.getcomptype()!='NONE':
                raise ValueError('yamnet_requires_pcm16_mono_16khz')
            pcm = reader.readframes(reader.getnframes())
        waveform = np.frombuffer(pcm, dtype='<i2').astype(np.float32) / np.float32(32768.)
        if not waveform.size:
            raise ValueError('yamnet_empty_audio')
        session, labels = self._load()
        input_name = session.get_inputs()[0].name
        scores = np.asarray(session.run(None, {input_name: waveform})[0], dtype=np.float32)
        if scores.ndim != 2 or scores.shape[1] != 521:
            raise RuntimeError('yamnet_output_shape')
        result = aggregate(scores, labels)
        result.update(classifier=CLASSIFIER, classifier_version=VERSION, classifier_config_version=CONFIG_VERSION,
                      model_sha256=MODEL_SHA256, processing_ms=round((time.perf_counter()-started)*1000),
                      cold_init_ms=self.cold_init_ms)
        return result


def aggregate(scores, labels):
    groups = {'speech':_indices(labels,SPEECH), 'singing':_indices(labels,SINGING), 'rapping':_indices(labels,RAPPING),
              'music':np.array([i for i,label in enumerate(labels) if label in ('Music','Musical instrument') or 'music' in label.lower()],dtype=np.intp)}
    def metrics(index):
        values = np.max(scores[:, index], axis=1) if index.size else np.zeros(scores.shape[0], dtype=np.float32)
        return _score(values.mean()), _score(values.max()), _score((values >= PATCH_SCORE).mean())
    speech, music, singing, rapping = (metrics(groups[name]) for name in ('speech','music','singing','rapping'))
    classification = 'ambiguous'
    if singing[0]>=STRONG and speech[0]<STRONG and music[0]<STRONG: classification='singing'
    elif speech[0]>=STRONG and music[0]>=STRONG: classification='mixed'
    elif music[0]>=STRONG and speech[0]<=LOW and singing[0]<STRONG: classification='music'
    elif speech[0]>=STRONG and singing[0]<STRONG: classification='speech'
    elif (speech[0]>=STRONG and singing[0]>=STRONG) or (music[0]>=STRONG and singing[0]>=STRONG): classification='mixed'
    means = scores.mean(axis=0)
    top = sorted(({'label': labels[i], 'score': _score(value)} for i,value in enumerate(means)), key=lambda value:(-value['score'],value['label']))[:3]
    return {'classification':classification, 'speech_score':speech[0], 'music_score':music[0], 'singing_score':singing[0],
            'speech_patch_ratio':speech[2], 'music_patch_ratio':music[2], 'singing_patch_ratio':singing[2],
            'speech_score_max':speech[1], 'music_score_max':music[1], 'singing_score_max':singing[1], 'rapping_score':rapping[0],
            'top_classes':top}


yamnet_service = YamnetService()
