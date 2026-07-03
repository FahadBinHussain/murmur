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
EXPOSE 7860
ENV MURMUR_COOKIES=/app/cookies.hf.json
ENV LITELLM_BASE=https://alchoholpad-litellm.hf.space/v1
ENV DEFAULT_CHAT=console_groq_com_fahadbinhussain001/llama-3.1-8b-instant
ENV DEFAULT_IMAGE=cloudflare/@cf/black-forest-labs/flux-1-schnell
ENV BNP_MESSENGER_OUTBOX_URL=https://dailybnp.com/api/internal/bnp-messenger-outbox
ENV BNP_MESSENGER_THREAD_ID=984803114200952
ENV BNP_MESSENGER_POLL_SECONDS=30
ENV BNP_MESSENGER_CLAIM_LIMIT=2
ENV BNP_MESSENGER_REQUEST_TIMEOUT_SECONDS=30
ENV MURMUR_AUTOMATION_NOTIFICATION_DEFAULT_THREAD_ID=2637078310061988
ENV NO_COLOR=1
ENV PORT=7860
ENV WHATSAPP_ENABLED=0
ENV WACLI_SEND_WEBHOOK_URL=
ENV HTTP_PROXY=
ENV HTTPS_PROXY=
CMD ["/bin/sh", "-c", "echo \"MURMUR_COOKIES_JSON length: ${#MURMUR_COOKIES_JSON}\"; if [ -n \"$MURMUR_COOKIES_JSON_B64\" ]; then echo \"$MURMUR_COOKIES_JSON_B64\" | base64 -d > /app/cookies.hf.json; echo \"Decoded from base64\"; elif [ -n \"$MURMUR_COOKIES_JSON\" ]; then echo \"$MURMUR_COOKIES_JSON\" > /app/cookies.hf.json; fi; cat /app/cookies.hf.json | wc -c; mkdir -p /app/wacli; exec ./murmur-bridge"]

