# Kurukin Intelligence

Current extension release: **Kurukin Intelligence 0.8.16 — reset current action wiring AUTO CURATOR**. Normal audio
acquisition is browser MP4→WAV followed by server HTTP 202; local Silero/YAMNet
remain explicit Audio Lab diagnostics only. See [extension/README.md](extension/README.md).

Kurukin Extension **0.5.0** está validada por el operador en Chrome real: sesión TikTok, sidebar, perfil, paginación, metadata y conversión local MP4 → WAV PCM16 mono 16 kHz. Ejemplo observado: 39,66 s, MP4 en RAM ~2,45 MB, WAV ~1,27 MB, conversión ~0,4 s.

El backend amplía el proyecto hacia un **corpus global compartido**: un vídeo por `tiktok_id`, un transcript por vídeo y reservas globales de enriquecimiento. El servidor nunca hace requests a TikTok; recibe metadata pública y WAV desde la extensión. El MP4 permanece en RAM de Chrome y el WAV del servidor se elimina tras procesarlo.

```text
kurukin-tiktok/
├── .gitignore
├── README.md
├── stack.yml
├── extension/
├── backend/
├── reference/
└── kurukin-extension.zip
```

- [Extensión: instalación y uso](extension/README.md). En `chrome://extensions`, Load unpacked con `extension/`; tras Reload, recargar también la pestaña TikTok.
- [Backend: contrato, corpus y arquitectura futura](backend/docs/architecture.md).
- [Operación: bootstrap, secrets, despliegue y validación](backend/docs/deployment.md).
- [Validación real controlada de Phase 2A](backend/docs/phase-2a-controlled-validation.md).
- [Revisión Phase 2A y resultados](backend/docs/phase-2a-review.md).
- [Resultados anteriores de validación y límites](backend/docs/validation.md).
- [Referencia de implementación](reference/myfavett-analysis.md), excluida del ZIP distribuido.

El milestone 1 síncrono fue validado en producción. **Phase 2A / backend 0.2.1** usa inbox WAV durable, RabbitMQ y un worker independiente con singleton PostgreSQL mediante conexión psycopg dedicada. El lock garantiza como máximo un worker Whisper global en esta instalación single-node; futuras instalaciones multi-worker, GPU o multi-node deberán sustituir o acotar esta exclusión. **Phase 2B / código 0.2.2 y extensión 0.6.0, aún sin desplegar**, añade evaluación local de habla antes del WAV: `no_speech` puede resolver un job como `skipped` sin audio, Rabbit ni Whisper; `speech_candidate` y `ambiguous` mantienen el flujo WAV. VAD no es un clasificador de canto. No hay frontend SaaS, autenticación, LLM ni integración automática de la extensión con `enrichment_requests` todavía.

```bash
node --test extension/tests/*.test.cjs
cd backend
.venv/bin/python -m pytest -q
```
