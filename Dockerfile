FROM ghcr.io/open-webui/open-webui:main

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app/murmur

COPY requirements.txt pyproject.toml README.md ./
COPY murmur ./murmur
RUN python -m venv /app/murmur/.venv \
    && /app/murmur/.venv/bin/pip install --no-cache-dir --upgrade pip \
    && /app/murmur/.venv/bin/pip install --no-cache-dir .

COPY scripts ./scripts

CMD ["bash", "/app/murmur/scripts/start-all.sh"]
