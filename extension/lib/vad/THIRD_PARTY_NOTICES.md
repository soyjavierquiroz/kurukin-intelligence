# Local VAD assets

- `vad-web-0.0.29.min.js`: `@ricky0123/vad-web` 0.0.29, ISC license. Its original license notice is retained in `vad-web-0.0.29.LICENSE.txt`.
- `silero-vad-legacy.onnx`: Silero VAD legacy model, MIT license.
- `ort-wasm-1.22.0.min.js`, `ort-wasm-simd-threaded.wasm`, and `ort-wasm-simd-threaded.mjs`: ONNX Runtime Web 1.22.0, MIT license.

These files are packaged with the extension and are loaded only from `chrome-extension://…/lib/vad/`. They are not fetched from a CDN or any Kurukin service.

YAMNet reuses this exact ONNX Runtime Web copy and its WASM assets; see `../audio-classifier/THIRD_PARTY_NOTICES.md`. No additional ONNX Runtime binary is packaged.
