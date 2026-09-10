# Phase 2A — revisión previa a autorización

Fecha: 2026-09-09. Implementación local de arquitectura async 0.2.0. No despliegue, migración de producción, creación de secret RabbitMQ ni bootstrap real --create.

## 1. Inspección segura de RabbitMQ

- Servicio: rabbitmq_rabbit_mq; contenedor encontrado: 07306547411a.
- Imagen: rabbitmq:management@sha256:23fe4f224d3d4d4e3c3904e82fb8a47824f0ee03412c33b7b21e6bf00b9852fa.
- Redes: general_network y traefik_public; alias rabbit_mq en ambas.
- Mount: volumen rabbitmq_rabbitmq_data → /var/lib/rabbitmq.
- Docker secret names: ninguno.
- Env, exclusivamente nombres:

```text
RABBITMQ_DEFAULT_PASS=<redacted>
RABBITMQ_DEFAULT_USER=<redacted>
RABBITMQ_DEFAULT_VHOST=<redacted>
RABBITMQ_ERLANG_COOKIE=<redacted>
```

- Published ports: ninguno. Status informa listeners internos 5672, 15672, 15692 y 25672; no implica publicación externa.
- Task actual Running, contenedor Up ~2 weeks. Hay tasks históricos fallidos; no se tocaron.
- rabbitmq-diagnostics ping: success.
- rabbitmqctl status: RabbitMQ 4.2.5, sin alarmas, una conexión, cero queues, un vhost durante inspección.
- Vhosts: solo default. Users: solo root [administrator]. **No existe /kurukin-tiktok ni kurukin_tiktok.**
- La versión rechazó `list_users user tags` porque no admite argumentos de columnas; `list_users` devuelve únicamente user y tags y se usó para completar la inspección.
- No se leyeron passwords, ni se reutilizaron credenciales de otra app.

## 2–4. DB, migración 0002 y diseño operacional

[Modelo](../app/models.py) y [0002_async_transcription_jobs.py](../alembic/versions/0002_async_transcription_jobs.py). Una fila UUID por vídeo, video_id FK UNIQUE; siete estados con CHECK, prioridad, attempts default 0, ruta/tamaño/duración WAV, timestamps de cada fase, last_error_code y timestamps de fila. Reserva ligada al análisis en videos, sin auth nueva. Transcript existente evita job nuevo; reserva expirada reutiliza fila; active job impide otra adquisición.

0001 no se editó. SHA-256 congelado y comprobado en test: `9e68000d4de9c49986d728545decec645e31f6198f8c0a7f7372f5ce4c6e9e4e`. La cadena upgrade/downgrade y metadata se prueban en SQLite; PostgreSQL se valida mediante SQL offline. Nada aplicado a DB real.

## 5–6. Upload async e inbox

[API](../app/main.py), [jobs](../app/jobs.py). POST audio devuelve 202 con status, video_id, job_id y audio_received. No dependencia ni import Whisper en API. Filename ignorado; WAV validado; `.part` 0600 → flush/fsync → replace `.wav` → fsync directorio → commit DB. Fallo parcial elimina .part. Si DB falla después de rename, la reconciliación adopta WAV válido de la reserva. Si broker falla después de commit, 202 audio_received=true conserva adquisición.

Volumen propuesto kurukin_tiktok_audio_queue en /data/audio-queue para ambos procesos. El volumen local exige mismo nodo y no protege contra pérdida del disco.

## 7. RabbitMQ y reconciliación

[Pika publisher](../app/queue.py): queue durable transcription.v1, mensajes persistentes, mandatory, confirms, prioridad 0–100. Mensaje exclusivamente job_id/video_id. No transacción distribuida. Falla publish → audio_received sin queued_at. Reconciliador pequeño cada 60 s en API, máximo 20 filas por pasada; [ops alternativo](../app/maintenance.py): `python -m app.maintenance`.

## 8–10. Worker, idempotencia y ACK/retry

[Worker](../app/workers/whisper.py), [Whisper compartido](../app/whisper.py). Proceso independiente, executor=1, prefetch=1, manual ACK y advisory lock de ownership. Un modelo lazy persistente small/cpu/int8; dos threads CPU; timeout termina hijo y siguiente intento lo recrea. Locks de filas liberados durante inferencia.

Primero comprobar transcript; comprobar de nuevo al guardar. UNIQUE global intacto. Transcript + completed en un commit; limpieza exitosa o cleanup_pending persistido antes del ACK. Fallo recuperable: queued confirmado + NACK requeue con backoff 5 s. Máximo tres intentos de inferencia; failed terminal confirmado recibe ACK. DB incierta: sin ACK, reconexión/redelivery. Poison IDs inválidos/incongruentes: NACK sin requeue. Inference puede repetirse tras crash antes de commit; transcript persistido no se duplica.

## 11–12. Cleanup y backpressure

Completados se borran inmediatamente. Failed >24 h, .part >2 h, WAV huérfanos >2 h: cleanup periódico. queued/processing válidos se conservan. Symlinks no se siguen. Defaults: máximo 3 GiB, mínimo libre 5 GiB, máximo 2000 jobs activos. Se incluye espacio entrante proyectado y se serializa admisión con flock. Al cap se rechazan nuevos batches; las reservas ya contadas pueden subir sin crear otro job. HTTP 503 `{"code":"audio_queue_capacity","retryable":true}`. Dos POST simultáneos como máximo en buffering de API.

## 13. Acquisition batch API

POST /api/v1/analyses/{analysis_id}/acquisition-batches. Default 10; rellena reservas propias pendientes sin duplicarlas y excluye transcript, active job, reserva ajena y duración ineligible. Tras cada 202 el futuro collector puede avanzar al siguiente ID. Enrichment requests incluyen job_id. Extensión solo documentada, sin conexión automática ni auto-loop nuevo. Phase 2B VAD y Collector API/auth futuros documentados.

## 14–15. Stack y recursos

[stack.yml](../../stack.yml): API y whisper-worker con imagen 0.2.0, general_network, DB/Rabbit secrets dedicados externos, volumen audio compartido; cache Whisper únicamente worker. Sin Traefik ni published ports. Réplicas 1, stop-first, pin al nodo HughesDocker2026.

Worker: CPU 2, RAM 2 GiB, reserva 512 MiB. API: CPU 1, RAM 512 MiB, reserva 256 MiB. API no carga Whisper. [Medición aislada](api-memory-0.2.0.json): import + health/ready + dos uploads multipart grandes con persistencia simulada. Realizada dentro de imagen con límite 512 MiB, CPU 1 y red deshabilitada. Resultado final: import RSS máximo 65.88 MiB; dos multipart de 9,216,044 bytes cada uno, RSS máximo 163.30 MiB; cgroup memory.peak 158,240,768 bytes; Whisper no importado. Apoya límite inicial, no sustituye carga con DB/Rabbit reales.

## 16. Bootstrap preparado

[bootstrap_rabbitmq.sh](../ops/bootstrap_rabbitmq.sh), modos --check y --create. --check real ejecutado correctamente: ping success, dedicated vhost=false y dedicated user=false; únicamente lectura. --create solo se probó contra un ejecutable Docker simulado en directorio temporal: no modificó broker real. Genera password criptográfico, archivo root:0600 exclusivo, password por stdin; namespace y permisos dedicados; no rotación de existentes. No imprime URL ni password y no crea Docker secret. Bootstrap parcial exige inspección manual conservando el archivo.

El método stdin está respaldado por [RabbitMQ access control](https://www.rabbitmq.com/docs/access-control); manejo de I/O por [Pika BlockingConnection](https://pika.readthedocs.io/en/latest/_modules/pika/adapters/blocking_connection.html).

## 17. Tests

131 backend + 165 extensión = **296 tests únicos**. Evidencias: [backend](backend-tests-0.2.0.log), [extensión](extension-tests-0.2.0.log), [misma suite backend dentro de imagen](image-tests-0.2.0.log). Migraciones aisladas, DB uniqueness, reservas, 202 sin Whisper, rename/cleanup, recuperación, publish failure, mensaje IDs, confirms, ACK/NACK, duplicados, timeout/model lifecycle, capacidad HTTP y retención, regresiones de ranking/eligibility/coverage/dedupe, bootstrap con Docker simulado.

La primera ejecución de imagen detectó que un test daba solo 1 s al arranque del hijo de recuperación bajo CPU throttling; se separó ese margen (5 s) del timeout deliberado del hijo lento (1 s). No se cambió el timeout de producción (300 s). Warning no bloqueante: deprecación de anyio.abc.BlockingPortal en Starlette TestClient.

## 18. Imagen 0.2.0

Build local completado de kurukin-tiktok-api:0.2.0 (~860 MB según Docker). [Log de build](image-build-0.2.0.log). Digest final: `sha256:dc4f2a8ed83d6c5eb9542af348c8c92a8028ae88ad5132c0dd0ce4b0eb38fc41`. Suite dentro de imagen: 131 passed, 0 failed, 1 warning (24.63 s). No push, deploy ni nuevos servicios. Servicio real kurukin-tiktok_api sigue en kurukin-tiktok-api:0.1.1, 1/1.

## 19. Riesgos y validación pendiente

- El circuito RabbitMQ + PostgreSQL real de 0.2.0 todavía no se ha probado: falta autorización de namespace, secret, migración y despliegue. Dobles y SQLite no demuestran semántica de locks de PostgreSQL ni redelivery real.
- Volumen local compartido y pin al mismo nodo; sin HA de archivos. Un purge manual o pérdida del almacenamiento RabbitMQ necesita recuperación operacional de queued desde DB.
- El repositorio no es un checkout Git; no hay commit/diff Git. Archivos y artefactos están disponibles para revisión local.
- El build dejó aproximadamente 11 GiB libres según df (redondeado); la adquisición respeta cap y mínimo libre configurados. Retención/reconciliación dependen de que API continúe ejecutándose o se invoque maintenance.
- Failed es terminal; no hay botón de retry/admin todavía. La autenticación collector y exposición pública quedan fuera de fase.
- El worker mantiene un modelo único y ownership por conexión PostgreSQL; una pérdida de ownership obliga a salir, para que Swarm reinicie y vuelva a adquirirlo.

**Detenido para revisión.** No ejecutar bootstrap real --create, Docker secret create, Alembic upgrade real ni docker stack deploy 0.2.0 sin autorización explícita.
