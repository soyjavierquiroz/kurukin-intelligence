> Documento del milestone síncrono 0.1.x. Para 0.2.0 consultar [revisión Phase 2A](phase-2a-review.md) y [arquitectura](architecture.md). No ejecutar los pasos históricos como procedimiento de migración 0.2.0.

# Operación Swarm — ejecutar solo después de autorización

El stack consta únicamente de `kurukin-tiktok_api`, imagen local `kurukin-tiktok-api:0.5.0`, una réplica y un proceso uvicorn en `0.0.0.0:8000`. No publica ports ni configura Traefik. La imagen no se sube a registry; al ser single-node está disponible localmente para Swarm.

## Infraestructura existente

PostgreSQL `postgres_postgres`, imagen ankane/pgvector:latest, alias `postgres`, puerto interno 5432, red `general_network` overlay/swarm/attachable. La aplicación usa exclusivamente database y role `kurukin_tiktok`. `n8n_v2_data` solo es la conexión administrativa existente que usa bootstrap para consultar catálogos y crear la database separada; nunca se crean/alteran tablas de Kurukin allí.

No actualizar/redeployar el servicio PostgreSQL, su stack, volumen `postgres_pgvector_data`, variables ni otros roles/databases. No ejecutar DROP de roles/databases. Redis, RabbitMQ y MinIO existentes no se usan ni se duplican.

`general_network` se declara **external**. No se crea otra overlay/bridge, ni `kurukin-tiktok_default`. Esa red vieja es **legacy candidate for cleanup**, no se usa ni se borra. Traefik se conectará más adelante a `traefik_public` para `intelligence.kuruk.in`, websecure y certresolver le; este stack no incluye esa configuración.

## Credenciales

Driver conservado: SQLAlchemy síncrono + **psycopg v3**. Resolución:

1. `DATABASE_URL_FILE`, si está definida. Un archivo ausente, vacío, ilegible o inválido falla sin fallback.
2. `DATABASE_URL`, solo cuando no se definió el archivo.

La configuración exige scheme `postgresql+psycopg`, host postgres, puerto 5432, database kurukin_tiktok, username kurukin_tiktok y password no vacío. Rechaza parámetros de URL que podrían sustituir el destino. No hay URL por defecto ni carga implícita de `.env`. `/health` funciona sin configuración DB; `/ready` devuelve 503 genérico si falta.

Bootstrap genera 32 bytes criptográficos mediante openssl y codifica en hexadecimal (64 caracteres, seguros en URL). Guarda directamente `postgresql+psycopg://kurukin_tiktok:PASSWORD@postgres:5432/kurukin_tiktok` en `/root/.kurukin-tiktok-db-url`, root/0600. No imprime el contenido, no expone password en argv/history, no borra el archivo.

`--check` localiza exactamente un contenedor local running por label `com.docker.swarm.service.name=postgres_postgres`, ejecuta psql con POSTGRES_USER/POSTGRES_DB internos y solo imprime existencia de role/database y owner correcto. `--create` usa ON_ERROR_STOP, lock local y advisory lock de sesión; crea únicamente lo ausente y corrige owner solamente de kurukin_tiktok. Nunca rota/modifica un role existente.

Si una creación se interrumpe, conservar el archivo y ejecutar --check antes de reintentar. Si el role aún no existe, reutiliza el archivo válido root/0600 sin symlink ni hardlinks. Si el role ya existe, conserva su password; el script no puede recuperarlo ni garantizar que un archivo creado manualmente coincida. Usar el secret existente correcto. No sobrescribir credenciales para intentar reparar un role.

Las sentencias sensibles se envían por stdin; se desactiva logging SQL/duración para esa sesión sin cambiar el servicio. Errores de psql se sustituyen por mensajes seguros. Si existe auditoría externa (por ejemplo plugins que ignoren estos ajustes de sesión), revisar su política antes de autorizar bootstrap. No se han inspeccionado ni cambiado esos ajustes globales.

Secret Swarm externo: `kurukin_tiktok_database_url_v1`, target `/run/secrets/kurukin_tiktok_database_url`, mode 0400. Solo el path está en environment; no URL en stack, Dockerfile, Git ni logs. El proceso actual del contenedor corre como root y puede leerlo. Rotación/versionado de credenciales queda para otra fase; no cambiar el password de un role existente con este bootstrap.

## Orden exacto del despliegue futuro

**Estos comandos son documentación; aún no se ejecutaron.** Ejecutar como root desde `/opt/apps/kurukin-intelligence`, en el nodo que aloja los tasks. No encadenar pasos si uno falla.

1. Comprobar bootstrap:
   ```bash
   backend/ops/bootstrap_database.sh --check
   ```
2. Crear únicamente role/database dedicados:
   ```bash
   backend/ops/bootstrap_database.sh --create
   ```
3. Crear secret desde archivo, sin mostrar contenido:
   ```bash
   docker secret create kurukin_tiktok_database_url_v1 /root/.kurukin-tiktok-db-url
   ```
   Si el secret ya existe, Docker falla de forma segura; no eliminarlo ni rotarlo automáticamente. Verificar el estado antes de continuar.
4. Solo tras creación exitosa del secret, borrar el archivo temporal:
   ```bash
   rm -f /root/.kurukin-tiktok-db-url
   ```
5. Construir imagen local (ya se probó el build; repetir si cambió el código):
   ```bash
   docker build -t kurukin-tiktok-api:0.5.0 backend
   ```
6. Desplegar el único stack de Kurukin:
   ```bash
   docker stack deploy --resolve-image never -c stack.yml kurukin-tiktok
   docker service ps kurukin-tiktok_api
   ```
   Esperar un task running. Sin registry, --resolve-image never evita intentar resolver el tag local contra Docker Hub.
7. Migración explícita dentro del task actual:
   ```bash
   backend/ops/run_migrations.sh
   ```
   Localiza por `com.docker.swarm.service.name=kurukin-tiktok_api`, exige exactamente un contenedor running y ejecuta `alembic upgrade head` en /app. Aborta y devuelve error genérico si falla. No migra en cada startup/restart.
8. GET /health desde el task:
   ```bash
   api_container=$(docker ps --filter label=com.docker.swarm.service.name=kurukin-tiktok_api --filter status=running --format '{{.ID}}')
   docker exec "$api_container" python -c 'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=5).read().decode())'
   ```
   Esperado: `{"status":"ok"}`.
9. GET /ready desde el task:
   ```bash
   docker exec "$api_container" python -c 'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:8000/ready", timeout=15).read().decode())'
   ```
   Esperado: `{"status":"ready","database":"ok"}`. Comprobar DNS y TCP como parte de esta validación:
   ```bash
   docker exec "$api_container" python -c 'import socket; socket.getaddrinfo("postgres",5432); s=socket.create_connection(("postgres",5432),5); s.close(); print("Swarm DNS/TCP: ok")'
   ```
   Ambas tareas comparten exclusivamente general_network. Esta prueba aún no se ha ejecutado contra el servicio real.
10. Metadata real exportada desde la extensión:
    Preparar en el host un JSON con `profile` y `videos` según [contrato](architecture.md), sin cookies/headers/contexto TikTok. En el siguiente comando, sustituir la ruta por ese archivo real:
    ```bash
    docker exec -i "$api_container" python -c 'import json,sys,httpx; r=httpx.post("http://127.0.0.1:8000/api/v1/analyses", json=json.load(sys.stdin), timeout=30); r.raise_for_status(); print(r.text)' < /ruta/metadata-real.json
    ```
    Guardar analysis_id y escoger exclusivamente un tiktok_id de enrichment_requests. Confirmar ranking, URL, eligibility y coverage. No usar fixtures sintéticos como evidencia de validación real.
11. WAV real de uno de esos requests, generado por Chrome:
    ```bash
    analysis_id='UUID_DEVUELTO'
    tiktok_id='ID_DE_ENRICHMENT_REQUESTS'
    docker exec -i "$api_container" python -c 'import sys,httpx; r=httpx.post(f"http://127.0.0.1:8000/api/v1/analyses/{sys.argv[1]}/videos/{sys.argv[2]}/audio",files={"audio":("audio.wav",sys.stdin.buffer.read(),"audio/wav")},timeout=330); r.raise_for_status(); print(r.text)' "$analysis_id" "$tiktok_id" < /ruta/audio-real.wav
    ```
    Confirmar transcript, volver a consultar análisis y repetir análisis para verificar reutilización. El primer request puede necesitar descargar el modelo desde HuggingFace. No hay llamadas a TikTok en estos comandos. El WAV entra por stdin y no se copia de forma persistente al task.

## Recursos, caché e imagen

Límites iniciales: 2 CPU / 2 GiB, reserva 256 MiB, réplica 1, concurrency 1. El benchmark oficial de [faster-whisper small/int8 en CPU](https://github.com/SYSTRAN/faster-whisper#small-model-on-cpu) registra 1477 MB para el modelo/carga de referencia sin batching. Es una orientación, no una medición del task completo: buffers WAV, API, runtime y picos de carga añaden memoria. Mantener 2 GiB inicialmente y medir con `docker stats` durante el WAV real; si hay OOM, evaluar 2,5 GiB. No memoria ilimitada ni cambio automático a base.

Volume local nombrado exactamente `kurukin_tiktok_whisper_cache`, montado en `/models/huggingface`; HF_HOME y WHISPER_CACHE_DIR apuntan allí. Se creará al desplegar, no se creó ahora. Reutilizable al recrear tasks en este nodo, no distribuido entre nodos. `/tmp/kurukin` no tiene volumen; cleanup finally y barrido de restos antiguos al arrancar.

Python 3.12 slim-bookworm, libgomp1 y certificados CA; PyAV incluye librerías FFmpeg, por lo que no se instala ffmpeg CLI. No CUDA, PyTorch, Playwright, Selenium ni paquetes para scraping. El modelo no se descarga durante build. Los pins directos están en requirements.txt; las dependencias transitivas y el tag base aún no están congelados por hash, por lo que builds futuros pueden variar.

No existe compose.yml de producción. No se requiere reiniciar ni redeployar ningún servicio existente para estos pasos.
