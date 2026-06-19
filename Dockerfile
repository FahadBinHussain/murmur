FROM golang:1.25 AS builder
WORKDIR /build
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -o murmur-bridge ./cmd/murmur-bridge

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates ffmpeg wget curl gnupg lsb-release && \
    curl -fsSL https://pkg.cloudflareclient.com/pubkey.gpg | gpg --yes --dearmor -o /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg && \
    echo "deb [signed-by=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg] https://pkg.cloudflareclient.com/ bookworm main" > /etc/apt/sources.list.d/cloudflare-client.list && \
    apt-get update && apt-get install -y cloudflare-warp && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=builder /build/murmur-bridge .
EXPOSE 7860
ENV MURMUR_COOKIES=/app/cookies.hf.json
ENV LITELLM_BASE=https://alchoholpad-litellm-huggingface-template.hf.space/v1
ENV DEFAULT_CHAT=openrouter/google/gemma-4-31b-it:free
ENV DEFAULT_IMAGE=cloudflare/@cf/black-forest-labs/flux-1-schnell
ENV NO_COLOR=1
ENV PORT=7860
ENV WHATSAPP_ENABLED=0
ENV WHATSAPP_PROXY=socks5://127.0.0.1:40000
CMD ["/bin/sh", "-c", "echo \"MURMUR_COOKIES_JSON length: ${#MURMUR_COOKIES_JSON}\"; if [ -n \"$MURMUR_COOKIES_JSON_B64\" ]; then echo \"$MURMUR_COOKIES_JSON_B64\" | base64 -d > /app/cookies.hf.json; echo \"Decoded from base64\"; elif [ -n \"$MURMUR_COOKIES_JSON\" ]; then echo \"$MURMUR_COOKIES_JSON\" > /app/cookies.hf.json; fi; cat /app/cookies.hf.json | wc -c; mkdir -p /app/wacli; echo \"Attempting WARP setup...\"; warp-cli registration new 2>&1; warp-cli mode proxy 2>&1; warp-cli connect 2>&1; echo \"WARP status: $(warp-cli status 2>&1)\"; sleep 2; exec ./murmur-bridge"]
