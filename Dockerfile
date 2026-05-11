FROM ghcr.io/open-webui/open-webui:main

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=7860

WORKDIR /app/murmur

RUN apt-get update \
    && apt-get install -y --no-install-recommends xvfb xauth \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt pyproject.toml README.md ./
COPY murmur ./murmur
RUN python -m venv /app/murmur/.venv \
    && /app/murmur/.venv/bin/pip install --no-cache-dir --upgrade pip \
    && /app/murmur/.venv/bin/pip install --no-cache-dir '.[facebook-login]' \
    && /app/murmur/.venv/bin/python -m playwright install --with-deps chromium

COPY scripts ./scripts

EXPOSE 7860

CMD ["bash", "/app/murmur/scripts/start-all.sh"]
