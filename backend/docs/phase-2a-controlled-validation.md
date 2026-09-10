# Phase 2A — validación real controlada

Fecha: 2026-09-09. Autorizada por el operador. **Detenida en checkpoint 13: falta WAV real de un candidato nuevo.** No se ejecutaron pruebas destructivas ni nueva inferencia.

## 1–2. Disco e imagen

Docker data usa /dev/vda1, filesystem /. Libre antes: 11,170,050,048 bytes; al detenerse: 11,159,117,824 bytes (~10.39 GiB), 94% utilizado.

| Imagen | Content size, bytes | Disk usage Docker |
|---|---:|---:|
| 0.1.1 | 205386608 | 858 MB |
| 0.2.0 | 205790618 | 860 MB |

La diferencia aparente 205 → 860 MB mezclaba dos métricas. `docker image ls --tree` distingue CONTENT SIZE y DISK USAGE; este Docker usa io.containerd.snapshotter.v1. El incremento comparable de contenido es 404,010 bytes. Sin optimización ni rebuild en esta validación.

/app: 100 → 140 KiB. site-packages: 494 → 496 MiB. Los directorios grandes corresponden a dependencias existentes de Whisper: ctranslate2.libs ~75 MiB, av.libs ~72 MiB, onnxruntime ~66 MiB, ctranslate2 ~60 MiB, numpy ~43 MiB y av ~32 MiB. Pika y código async explican el pequeño incremento.

La imagen no contiene WAV, MP4, fixture local, ZIP de extensión, .git, virtualenv duplicado, reference/myfavett ni caches accidentales de tests/build/modelos en las rutas inspeccionadas. /models ocupa 8 KiB y solo contiene la estructura de directorios, sin modelo descargado. Imagen desplegada: sha256:dc4f2a8ed83d6c5eb9542af348c8c92a8028ae88ad5132c0dd0ce4b0eb38fc41.

## 3–6. RabbitMQ y secret

Precheck: rabbitmq_rabbit_mq Running, contenedor 07306547411a, ping OK, sin alarmas. Inicialmente no existían /kurukin-tiktok ni kurukin_tiktok, ni el secret dedicado ni el archivo temporal.

`bootstrap_rabbitmq.sh --create` real completado. `--check` posterior: vhost=true, user=true. Verificación adicional de permisos:

- /kurukin-tiktok: kurukin_tiktok configure/write/read = .* / .* / .*.
- list_user_permissions kurukin_tiktok: únicamente /kurukin-tiktok.
- tags kurukin_tiktok = []; root conserva [administrator]. No se modificó default ni otros usuarios/vhosts.

Archivo temporal validado con lstat: regular, UID 0, 0600, sin symlink y nlink=1. Secret kurukin_tiktok_rabbitmq_url_v1 creado después de comprobar ausencia. Segundo lstat antes de `unlink -- /root/.kurukin-tiktok-rabbitmq-url`; ausencia verificada.

**rabbit credential temp removed: yes**. No URL ni password impresos.

## 7–8. Migración y esquema

Baseline con API aún 0.1.1: revision 0001_global_corpus; channels=1, analyses=3, videos=50, video_snapshots=150, transcripts=1.

Servicio temporal kurukin-tiktok-migrate-0002: imagen 0.2.0, general_network, solo DB secret, DATABASE_URL_FILE, restart-condition none; sin puertos, Rabbit ni Traefik. `alembic upgrade head`: complete, exit 0. Se actualizó únicamente su comando a `alembic current` para segunda tarea de verificación: complete, exit 0, `0002_async_transcription_jobs (head)`. Después se eliminó el servicio temporal. Logs mostrados mediante whitelist sanitizada.

Tablas verificadas: channels, analyses, videos, video_snapshots, transcripts, alembic_version, transcription_jobs. UNIQUE videos.tiktok_id, transcripts.video_id y transcription_jobs.video_id presentes. Todos los counts originales intactos; jobs=0 inmediatamente después de migrar. Transcript original intacto: UUID 863d27ce-fb68-4222-84ab-48f27fb910c6, vídeo 7676101081532747021, 698 caracteres. No se imprimió el texto ni se ejecutó Whisper.

## 9–11. Deploy y volumen

Se ejecutó `docker stack deploy --resolve-image never -c /opt/apps/kurukin-intelligence/stack.yml kurukin-tiktok` después de verificar migración y esquema.

- kurukin-tiktok_api: 0.2.0, 1/1, container f184f9734ce5, restart_count=0, OOM=false.
- kurukin-tiktok_whisper-worker: 0.2.0, 1/1, container 74871dd2166e, restart_count=0, OOM=false.
- Ambos estables durante múltiples muestras durante al menos 25 s. Sin nuevas tareas failed/rejected.
- API: DB + Rabbit secrets y kurukin_tiktok_audio_queue:/data/audio-queue.
- Worker: los mismos secrets/audio y kurukin_tiktok_whisper_cache:/models/huggingface.
- API sin cache Whisper; ambos sin published ports ni Traefik.
- Marker inocuo escrito desde API, leído correctamente desde worker y eliminado: filesystem compartido confirmado.

Muestra final en reposo: API 63.71 MiB / 512 MiB y 0.26% CPU; worker 55.11 MiB / 2 GiB y 0.00% CPU. No son mediciones de inferencia.

## 12–15. Rabbit connection, health y regresión

Desde API, usando secret real: connect → declare queue → close OK, sin imprimir configuración. Worker consumidor listo.

| name | durable | messages_ready | messages_unacknowledged | consumers | arguments |
|---|---|---:|---:|---:|---|
| transcription.v1 | true | 0 | 0 | 1 | x-max-priority=100, x-queue-type=classic |

/health 200 {status:ok}; /ready 200 {status:ready,database:ok}. API sin hijos de modelo ni librerías de inferencia en maps de PID 1; importar app.main no importa app.whisper ni faster_whisper.

martamarcilla conserva 50 vídeos. El transcript 7676101081532747021 conserva UUID y longitud; job inexistente y needs_audio=false. No nueva inferencia para ese vídeo.

## 16–17. Acquisition batch y WAV pendiente

Se solicitó vía HTTP POST el batch del análisis existente e8f91140-f27f-4952-96bf-be9f5740557f, sin reenviar ni cambiar metadata. ACQUISITION_BATCH_SIZE=10. HTTP 200, diez solicitudes válidas, cada una con job reserved, reserva del análisis correcto, duración elegible y sin transcript. Excluido 7676101081532747021.

| tiktok_id | overall_rank | transcription_rank | priority | duration s | views | outlier_score |
|---|---:|---:|---:|---:|---:|---:|
| 7678100857002429710 | 7 | 1 | 50 | 24 | 607500 | 5.1923076923 |
| 7678032080709717261 | 8 | 2 | 50 | 27 | 483900 | 4.1358974359 |
| 7675805902699531533 | 9 | 3 | 50 | 39 | 440600 | 3.7658119658 |
| 7616798367796153614 | 2 | 4 | 50 | 23 | 9600000 | 82.0512820513 |
| 7676915189500923149 | 3 | 5 | 50 | 31 | 1500000 | 12.8205128205 |
| 7678773331130125582 | 4 | 6 | 50 | 21 | 1400000 | 11.9658119658 |
| 7679146690628062477 | 5 | 7 | 50 | 21 | 1300000 | 11.1111111111 |
| 7675713182261890318 | 6 | 8 | 50 | 17 | 1100000 | 9.4017094017 |
| 7677706683468025102 | 10 | 9 | 50 | 20 | 337600 | 2.8854700855 |
| 7683249073582869773 | 11 | 10 | 50 | 42 | 315200 | 2.6940170940 |

Los primeros tres del orden de adquisición son 7678100857002429710, 7678032080709717261 y 7675805902699531533. transcription_rank conserva posiciones del análisis previo; no equivale a overall_rank. Prioridad Rabbit normal=50 para todos.

Solo se encontró `.local-fixtures/martamarcilla/kurukin-test-7676101081532747021.wav` (1,269,130 bytes): pertenece al vídeo ya transcrito y **no se utilizó**. No hay WAV inequívoco para los nuevos candidatos en el proyecto. No se fabricó, renombró ni reasoció audio.

Detenido en checkpoint 13. Upload 202 real, tiempos, transiciones, inferencia, cleanup y reuse de un nuevo transcript quedan pendientes del WAV y la siguiente autorización. Las reservas duran 30 minutos; antes del futuro upload se debe revalidar/reobtener la reserva si caducó. Cola permanece vacía y worker esperando.
