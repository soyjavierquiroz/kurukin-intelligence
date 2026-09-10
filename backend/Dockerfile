FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HF_HOME=/models/huggingface
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 ca-certificates && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
RUN test -f /app/app/audio_models/yamnet.onnx && test -f /app/app/audio_models/yamnet_class_map.csv
COPY alembic.ini .
COPY alembic ./alembic
RUN mkdir -p /models/huggingface /tmp/kurukin /data/audio-queue && chmod 700 /tmp/kurukin
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
