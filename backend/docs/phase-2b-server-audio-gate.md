# Phase 2B — server-side audio gate (0.3.0)

Chrome adquiere WAV PCM16 mono 16 kHz y recibe HTTP 202 sin VAD/clasificador obligatorio. El worker existente de `transcription.v1` conserva Rabbit, advisory singleton y prefetch 1; tras almacenamiento durable, ejecuta YAMNet antes de iniciar el ciclo lazy de Whisper.

YAMNet está empaquetado en `app/audio_models/`: ONNX opset 15, 16,093,355 bytes, SHA-256 `d3835ffbbd4a1bb3e777f0ca217b5007907f5171dd5d17c4236b95b2af8f908e`, class map y notices Apache-2.0. ONNX Runtime CPU crea una sesión lazy/singleton por proceso. `wave` + NumPy leen el WAV directamente; no hay ffmpeg ni resampling.

La migration local no aplicada `0003_audio_assessment` crea `audio_assessments`, global por `video_id + classifier + classifier_version`, y una relación opcional desde el job. Se persisten agregados/top-3/timing, nunca matriz de patches, WAV, PCM ni media URL.

`AUDIO_GATE_MODE=observe` es el default y siempre continúa a Whisper. `enforce` solo salta para `music`; `speech`, `singing`, `mixed`, `ambiguous`, rapping o error continúan a Whisper. El error `yamnet_failed` es fail-open. Skip music elimina WAV y ACKea únicamente tras estado terminal durable + cleanup; cuenta como resolved enrichment, no transcript coverage.
