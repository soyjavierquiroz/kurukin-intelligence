# Kurukin Intelligence 0.8.13

La ruta normal es deliberadamente estrecha:

```text
TikTok MP4 (RAM, MAIN) -> decode -> PCM mono 16 kHz -> WAV PCM16
  -> base64 runtime handoff -> service worker HTTPS POST -> HTTP 202
  -> liberar referencias -> siguiente vídeo
```

Auto Curator descubre en checkpoints internos de 50 publicaciones. Cada
checkpoint seguro se confirma contra el mismo `analysis_id` y scan lógico.
Discovery continúa hasta `hasMore=false` aunque la adquisición esté ocupada:
la capacidad temporal activa un cooldown sin pausar scroll ni paginación. Al
finalizar discovery, el navegador conserva el contexto del canal y hace final
drain con backoff hasta que todos los candidatos del scan actual estén
aceptados, resueltos globalmente o liberados. Tras un reinicio, reconcilia las
reservas activas, restaura el perfil activo y reacquire únicamente IDs pendientes
desde el navegador. No persiste cookies, tokens, URLs
de media ni audio. Un `202` significa que el backend aceptó el trabajo: no
espera polling de YAMNet, Whisper, transcript ni estado de job.

Las referencias de media permanecen sólo en el `Map` del MAIN world del canal
actual durante discovery y final drain. Si TikTok invalida una referencia tras
una espera prolongada, la extensión vuelve a solicitarla únicamente desde ese
contexto browser-side y libera la reserva según el contrato; no hay fetch
server-side ni fallback externo.

## Backend endpoint

La URL de producción está centralizada en `lib/backend.js`:
`https://intelligence.kuruk.in`. Las reservas y uploads HTTPS salen del
service worker; TikTok MAIN world sólo obtiene el MP4 y genera el WAV. No hay
servidor local, túneles, puertos ni configuración manual en la ruta normal. No se
guarda ninguna cookie, token, header ni URL de media.

## Uso

1. Carga `extension/` sin empaquetar en `chrome://extensions` y recarga la
   pestaña TikTok.
2. En un perfil TikTok con sesión iniciada, abre el sidebar y agrega los
   canales en **AUTO CURATOR**. Acepta `@creator`, `creator` o la URL del perfil;
   espacios, comas, tabs y líneas nuevas se convierten en chips deduplicados.
   La cola válida se guarda y arranca sola. Mientras corre, las nuevas entradas
   sólo se agregan como pendientes y sólo esos chips son eliminables.
3. La extensión abre en Product Mode. Genera los dos paquetes locales con
   `./extension/build-packages.sh`: Product es **Kurukin Intelligence** y Admin
   es **Kurukin Intelligence ADMIN**. Ambos usan los mismos permisos y motor;
   Admin Debug revela los controles manuales y diagnósticos seguros. En ese
   modo, usa **Analizar canal** y, en **Audio acquisition**, pulsa **Reservar lote en servidor** y luego
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
