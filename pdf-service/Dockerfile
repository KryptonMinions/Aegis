# Browsers + system libs come with the official image; keep the pip pin in
# requirements.txt equal to this tag.
FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy

WORKDIR /app

# Noto covers Kannada glyphs for the PDF export.
RUN apt-get update && apt-get install -y --no-install-recommends fonts-noto-core && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Cloud Run injects PORT; 8080 is the local docker-run fallback.
ENV PORT=8080
EXPOSE 8080

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
