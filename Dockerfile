FROM python:3.12-slim

# RUNNING_IN_CONTAINER gates the dev-only dotenv fallback in app/config.py.
# Inside the container, Compose's env_file is the sole source of secrets.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    RUNNING_IN_CONTAINER=1

WORKDIR /app

# UID/GID 1000 must match the owner of the bind-mounted ./data on the host.
RUN groupadd -g 1000 app && useradd -u 1000 -g 1000 -m -s /bin/bash app

# Dependencies in their own layer so code edits don't reinstall them.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code only. config/ and data/ arrive as bind mounts at runtime --
# baking them in is exactly what this setup avoids.
COPY app/ ./app/
COPY scripts/ ./scripts/

RUN mkdir -p /app/data /app/config && chown -R 1000:1000 /app

USER app
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
