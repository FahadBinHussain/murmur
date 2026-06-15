FROM golang:1.22 AS builder
WORKDIR /build
RUN go mod init bridge && \
    go mod edit -require go.mau.fi/mautrix-meta@v0.0.0-20260612210338-d7d8128567b5 && \
    go mod tidy
COPY bridge/main.go .
RUN go build -o /messenger-bridge .

FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=builder /messenger-bridge .
COPY ai-bridge-wrapper.py .
COPY start.sh .
RUN chmod +x start.sh
ENV LITELLM_BASE=https://alchoholpad-litellm-huggingface-template.hf.space/v1
ENV DEFAULT_CHAT=openrouter/google/gemma-4-31b-it:free
ENV DEFAULT_IMAGE=cloudflare/@cf/black-forest-labs/flux-1-schnell
ENV MY_UID=100094912747838
ENV NO_COLOR=1
EXPOSE 7860
CMD ["./start.sh"]
