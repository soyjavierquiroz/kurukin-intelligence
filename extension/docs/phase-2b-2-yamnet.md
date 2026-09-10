# Phase 2B.2 — local audio event classifier (experimental archive)

> Since Collector 0.8.0 this document describes **Audio Lab only**. Silero and
> YAMNet are not injected, initialized or run during startup, scan or normal
> audio acquisition. The normal client uploads every backend-reserved WAV and
> treats HTTP 202 as success; server YAMNet/Whisper are authoritative.

## Pinned source and model contract

`lib/audio-classifier/yamnet.onnx` is the Apache-2.0 YAMNet conversion pinned in its adjacent notices: fixed HF commit `f25b741c2f0bdc6d7e6db24b5fddda23347dbafd`, documented as a `tf2onnx 1.16.1` conversion from the official TF Hub `google/yamnet/1` SavedModel. The official TensorFlow Models source is `research/audioset/yamnet`; the 521-entry `yamnet_class_map.csv` is copied verbatim from that repository and is Apache-2.0.

The 16,093,355 byte ONNX graph is opset 15. Its variable-length `float32 waveform` input is the existing mono, 16 kHz PCM; output 0 is `[patches,521]` scores, output 1 is `[patches,1024]` embeddings, and output 2 is `[spectrogram_frames,64]`. Only output 0 is retained transiently. The exported graph contains standard ops supported by ONNX Runtime Web's WASM execution provider. It uses exactly the existing packaged ORT Web 1.22.0 JS/WASM, with `numThreads=1`; it packages no duplicate runtime.

The whole waveform frontend (25 ms STFT, 10 ms hop, 64 log-mel bands) is inside the pinned ONNX graph. No MP4 is decoded twice, no PCM is resampled twice, and no WAV is needed for classification.

## Aggregation and policy

`audio-classifier-config-v1` uses a patch score threshold of 0.35 and conservative strong/low thresholds 0.55/0.20. For every patch it takes the maximum score among each group; it retains group mean, maximum, and the fraction of patches at/above 0.35. Top classes are the top three mean per-class scores. All returned scores are bounded finite scores, not calibrated probabilities.

Speech is explicit AudioSet labels: Speech, Child speech, Conversation, Narration, Whispering, Speech synthesizer, Chatter, Speech noise, Shout, Yell, Bellow and Children shouting. Singing is explicitly separate: Singing, Choir, Child singing, Synthetic singing, Yodeling, Chant, Mantra and Humming. Rapping is tracked separately but is not treated as spoken speech in V1. Music derives from the official class map's `Music`, `Musical instrument`, and labels containing the word `music`; the AudioSet parent categories avoid a fragile giant hand-maintained genre/instrument list.

Rules: strong speech and music is `mixed`; strong music with low speech is `music`; strong singing without strong speech/music is `singing`; strong speech without strong singing is `speech`; otherwise `ambiguous`. `music_original`, title and author never affect this logic. In 0.8.0 this policy is invoked only through an explicit Audio Lab action; it never affects normal WAV upload, acquisition, skips or backend reporting.

## Lifecycle and validation

The YAMNet session and class map load lazily only after the explicit lab action.
Its cold initialization is reported separately as `yamnet_init_ms`; every
inference reports `yamnet_ms`. PCM, MP4, decoded buffers, WAV transient and
ONNX tensors are cleared/disposed after each experimental video. Silero remains
an experimental diagnostic signal displayed alongside YAMNet.

The Audio Lab result and copied JSON retain canonical id/url/caption, Silero ratio/seconds/segments/longest, YAMNet classification/scores/patch ratios/top classes, and decode/Silero/YAMNet/WAV/total timings. They never contain media URLs, PCM, MP4, WAV, full patch matrices, embeddings or tensors.

`tests/fixtures/audio-ground-truth.json` has the nine supplied labels (five music and four speech), without any audio asset. It is a calibration fixture only, not a representative dataset. Chrome validation must record the requested table and calculate `music correct / 5`, `speech correct / 4`, plus mixed/ambiguous counts before any threshold revision.
