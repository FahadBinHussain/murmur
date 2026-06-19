FROM golang:1.25 AS builder
WORKDIR /build
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -o murmur-bridge ./cmd/murmur-bridge

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates ffmpeg wget && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=builder /build/murmur-bridge .
RUN wget -q https://github.com/openclaw/wacli/releases/download/v0.11.1/wacli_0.11.1_linux_amd64.tar.gz -O /tmp/wacli.tar.gz && \
    tar -xzf /tmp/wacli.tar.gz -C /tmp && \
    mv /tmp/wacli /usr/local/bin/wacli && \
    chmod +x /usr/local/bin/wacli && \
    rm -rf /tmp/wacli*
EXPOSE 7860
ENV MURMUR_COOKIES=/app/cookies.hf.json
ENV LITELLM_BASE=https://alchoholpad-litellm-huggingface-template.hf.space/v1
ENV DEFAULT_CHAT=openrouter/google/gemma-4-31b-it:free
ENV DEFAULT_IMAGE=cloudflare/@cf/black-forest-labs/flux-1-schnell
ENV NO_COLOR=1
ENV WHATSAPP_ENABLED=0
ENV WHATSAPP_BINARY=/usr/local/bin/wacli
CMD ["/bin/sh", "-c", "echo \"MURMUR_COOKIES_JSON length: ${#MURMUR_COOKIES_JSON}\"; if [ -n \"$MURMUR_COOKIES_JSON_B64\" ]; then echo \"$MURMUR_COOKIES_JSON_B64\" | base64 -d > /app/cookies.hf.json; echo \"Decoded from base64\"; elif [ -n \"$MURMUR_COOKIES_JSON\" ]; then echo \"$MURMUR_COOKIES_JSON\" > /app/cookies.hf.json; fi; cat /app/cookies.hf.json | wc -c; exec ./murmur-bridge"]
