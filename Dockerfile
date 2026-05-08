FROM ghcr.io/open-webui/open-webui:main

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app/murmur

COPY requirements.txt pyproject.toml README.md ./
COPY murmur ./murmur
RUN pip install --no-cache-dir .

COPY scripts ./scripts

CMD ["bash", "/app/murmur/scripts/start-all.sh"]
