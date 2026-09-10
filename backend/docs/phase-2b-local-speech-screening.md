# Phase 2B: local speech screening

The extension runs Silero VAD locally after decoding to mono 16 kHz PCM and before WAV encoding. The vendored runtime is `@ricky0123/vad-web` 0.0.29 using the legacy Silero model and ONNX Runtime Web. `vad-web` is ISC-licensed; Silero VAD and ONNX Runtime are MIT-licensed. JavaScript, WASM and ONNX model files are packaged in the extension. There is no CDN, remote import, runtime executable download or backend speech-detection call.

VAD reports acoustic voice activity. It does not reliably distinguish spoken speech from singing. A song with vocals can therefore remain `speech_candidate` or `ambiguous`.

```
MP4 in MAIN-world RAM -> decode -> PCM mono/16 kHz -> local VAD
  no_speech (enforce) -> discard PCM/media; assessment; skipped, no WAV/Rabbit/Whisper
  speech_candidate or ambiguous -> WAV -> existing 202/Rabbit/Whisper flow
```

The extension defaults to `observe`: it records and displays a local classification but always creates a WAV. `enforce` skips the WAV only for `no_speech`. Thresholds are centralized in `extension/page/speech.js`: VAD positive/negative thresholds 0.65/0.45, 800 ms redemption and 250 ms minimum segment. `no_speech` requires all of speech duration below 0.75 s, ratio below 3%, and longest segment below 0.5 s. A candidate requires any of duration at least 2 s, ratio at least 12%, or longest segment at least 1.2 s; the remaining results are ambiguous. This deliberately favors false positives over false negatives.

Migration `0003_audio_assessment` adds assessment metadata to `transcription_jobs` and `skipped` to its state check. The assessment endpoint accepts only a current acquisition reservation. In enforce mode, a `no_speech` assessment changes that job to `skipped` with `skip_reason=no_speech`, leaves `audio_path` empty, publishes nothing, and excludes it from normal acquisition. Detector and detector-version are stored so a later version can be reevaluated. No reevaluation scheduler is introduced here.

Transcript coverage continues to count only completed transcripts. The response also includes `resolved_enrichment` and `resolved_enrichment_coverage`, which count a completed transcript or a `skipped/no_speech` job and must not be described as transcript coverage.

The Audio Lab remains development-only and does not call the assessment endpoint automatically. It shows safe VAD metrics and makes content-side object URLs only for generated WAV downloads. Media URLs, MP4 and PCM never leave the MAIN world.
