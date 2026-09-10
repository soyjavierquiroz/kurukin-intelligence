> Documento del milestone síncrono 0.1.x. Para 0.2.0 consultar [revisión Phase 2A](phase-2a-review.md) y [arquitectura](architecture.md). No ejecutar los pasos históricos como procedimiento de migración 0.2.0.

## Auditoría incremental 0.4.1 (solo lectura)

Tras un scan real iniciado desde la extensión, el operador puede guardar un
baseline local y compararlo sin llamar a TikTok ni modificar PostgreSQL,
RabbitMQ o la API. El auditor usa la misma configuración validada del backend
(`DATABASE_URL_FILE` en producción) y nunca muestra su valor.

```bash
cd backend
python ops/validate_incremental_corpus.py baseline --output /tmp/kurukin-incremental-baseline.json
# Ejecutar el scan real desde la extensión; este script no lo inicia.
python ops/validate_incremental_corpus.py compare /tmp/kurukin-incremental-baseline.json
```

Ejecutarlo donde ya esté disponible el secreto/configuración del backend (por
ejemplo, dentro del contenedor API con el código del repo accesible). No requiere
migración ni despliegue. `compare` termina en `RESULT: PASS` o `RESULT: FAIL` y
enumera las invariantes fallidas A--I. El JSON conserva únicamente contadores e
IDs internos de transcript/assessment para detectar desapariciones; no guarda
texto, audio, URLs ni secretos.

# Validación de esta iteración

Fecha: 2026-09-09.

- Backend: **64 tests pasan**. Configuración y secretos; health/ready; global reuse y presupuesto; reservas y claims concurrentes; duración y ranking separados; coverage sin duplicados; seguridad de metadata; WAV real PCM generado para tests; limpieza en éxito/error/timeout; worker spawn real con inferencia simulada, reap y recuperación; migración inicial comparada con metadata y downgrade en SQLite; SQL PostgreSQL generado offline; scripts ops con Docker simulado y paths aislados.
- Extensión: **165 tests pasan**, sin modificar código de extensión ni ZIP. Validación real Chrome 0.5.0 comunicada por el operador: metadata y audio correctos.
- Total: **229 tests, 229 pasan**. Un DeprecationWarning externo de Starlette/AnyIO sobre BlockingPortal; no fallo funcional.
- `bash -n` de ambos scripts ops: correcto.
- `docker stack config -c stack.yml`: correcto. Es validación local de YAML, no deploy.
- Build local `kurukin-tiktok-api:0.1.0`: correcto. ID final `sha256:cf17f2157091d469911990b94ce2336c8f3b669de98870af12bcfb8e9bdc6309`; Docker informa 205.386.011 bytes (el espacio total en disco incluye layers/build cache). Smoke test de imagen en contenedor efímero con `--network none`, sin secrets ni volúmenes: import faster-whisper/CTranslate2, soporte CPU int8, /health 200 y /ready 503 seguro sin configuración; modelo no cargado. La migración PostgreSQL offline también pasó dentro de la imagen final. Los hashes previos de la extensión coinciden.

Los tests usan SQLite aislado y un Docker falso para ops: **no** ejecutan bootstrap, migraciones ni queries contra PostgreSQL real. El SQL PostgreSQL se compila offline; quedan pendientes DNS overlay, permisos del role, Alembic online y contención real de locks PostgreSQL.

Pendiente tras autorización: crear role/database dedicados, secret, stack, migración real, health/ready dentro del task, metadata real y WAV real hacia backend; medir RAM/pico de cold start e inferencia small/int8. El backend no se ha validado con audio real del operador todavía.

Los logs locales de build/tests están en docs/*.log y excluidos de Git. El directorio no es actualmente un repositorio Git, así que no hay diff/commit Git disponible. La lista de cambios se documenta en esta entrega; no se inicializó un repositorio nuevo.

Riesgos delimitados: lease de 30 min puede caducar si se abandona o tarda demasiado el aporte; POST nuevo recupera trabajo pendiente. Ingestión del mismo canal puede esperar al lock de una inferencia activa. Coverage high value mezcla la última observación por vídeo, relativa a su análisis de origen. La caché es local al nodo. Modelo lazy necesita acceso a HuggingFace y puede superar el timeout inicial. No hay autenticación ni puertos públicos; exposición por Traefik queda fuera de esta fase.
