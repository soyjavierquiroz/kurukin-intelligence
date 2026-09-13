# Kurukin Intelligence 0.8.7

La ruta normal es deliberadamente estrecha:

```text
TikTok MP4 (RAM, MAIN) -> decode -> PCM mono 16 kHz -> WAV PCM16
  -> base64 runtime handoff -> service worker HTTPS POST -> HTTP 202
  -> liberar referencias -> siguiente vídeo
```

El cliente crea el análisis con metadata pública normalizada, recibe las
`enrichment_requests` reservadas por el backend y procesa como máximo el lote
que éste devuelve (la validación actual usa un único vídeo). Un `202` es **Audio secured**:
no hay polling de YAMNet, Whisper, transcript ni estado de job.

## Backend endpoint

La URL de producción está centralizada en `lib/backend.js`:
`https://intelligence.kuruk.in`. Las reservas y uploads HTTPS salen del
service worker; TikTok MAIN world sólo obtiene el MP4 y genera el WAV. No hay
servidor local, túneles, puertos ni configuración manual en la ruta normal. No se
guarda ninguna cookie, token, header ni URL de media.

## Uso

1. Carga `extension/` sin empaquetar en `chrome://extensions` y recarga la
   pestaña TikTok.
2. En un perfil TikTok con sesión iniciada, abre el sidebar y pega o escribe los
   canales en **AUTO CURATOR v1**. La cola válida se guarda y arranca sola; el
   curador realiza scan, reserva y adquisición secuencialmente. Los únicos
   controles operativos son Pause, Resume, Stop y Skip current channel.
3. Para depuración manual, usa **Analizar canal** y, en **Audio acquisition**, pulsa **Reservar lote en servidor** y luego
   **Adquirir audio reservado**. La UI muestra requested, uploaded, failed,
   current, el último error seguro (`STAGE · SAFE_ERROR_CODE`) y los tiempos
   disponibles de Fetch MP4, Decode, WAV, Handoff y Upload. Una etapa no ejecutada se
   muestra como `—`.

La ruta de datos no entrega `playAddr`, `downloadAddr`, cookies, tokens ni
headers al content script, panel o servidor. Para el handoff, el WAV se
valida contra `MAX_AUDIO_MB=10`, se codifica como base64 y se envía sólo al
service worker junto con los identificadores de reserva y `mime=audio/wav`.
El worker lo reconstruye y hace el POST HTTPS. Tras cada POST se anulan las
referencias MP4, AudioBuffer/PCM, WAV, base64, `Uint8Array` y `Blob`; no hay
acumulación ni persistencia de audio del lote.

## Audio Lab

**Experimental / Developer Diagnostics** está colapsado por defecto. Pulsar
su acción es la única vía que inyecta los scripts locales de ONNX Runtime,
Silero y YAMNet; los modelos ONNX se abren únicamente cuando esa acción llama
al clasificador. No se inician en startup, scan ni acquisition.

Para un vídeo puntual, primero reserva un lote y luego, dentro de Audio Lab,
usa **Adquirir solo ID reservado**. La extensión rechaza un ID que no figure
en la respuesta de reserva actual; no acepta IDs TikTok arbitrarios.

## Verificación

```bash
node --test extension/tests/*.test.cjs
```

Las pruebas cubren el endpoint HTTPS de producción, ausencia de servidor local,
ausencia de Silero/YAMNet en adquisición, secuencialidad, avance por 202,
liberación de buffers, reserva obligatoria y límites de seguridad.
