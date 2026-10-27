FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir -r requirements.txt

# Bake the model weights into the image so Cloud Run cold starts don't download
# from Hugging Face. Must equal Settings.embed_model_id (app/config.py).
ARG EMBED_MODEL_ID=intfloat/multilingual-e5-base
RUN python -c "from transformers import AutoModel, AutoTokenizer; AutoTokenizer.from_pretrained('${EMBED_MODEL_ID}'); AutoModel.from_pretrained('${EMBED_MODEL_ID}')"

COPY app ./app

# Cloud Run injects PORT; 8080 is the local docker-run fallback.
ENV PORT=8080
EXPOSE 8080

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
