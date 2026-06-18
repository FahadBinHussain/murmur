FROM golang:1.25 AS builder
WORKDIR /build
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -o murmur-bridge ./cmd/murmur-bridge

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=builder /build/murmur-bridge .
ENV MURMUR_COOKIES=/app/cookies.hf.json
ENV LITELLM_BASE=https://alchoholpad-litellm-huggingface-template.hf.space/v1
ENV DEFAULT_CHAT=openrouter/google/gemma-4-31b-it:free
ENV DEFAULT_IMAGE=cloudflare/@cf/black-forest-labs/flux-1-schnell
ENV NO_COLOR=1
CMD ["/bin/sh", "-c", "echo \"MURMUR_COOKIES_JSON length: ${#MURMUR_COOKIES_JSON}\"; if [ -n \"$MURMUR_COOKIES_JSON\" ]; then echo \"$MURMUR_COOKIES_JSON\" > /app/cookies.hf.json; cat /app/cookies.hf.json | wc -c; fi; exec ./murmur-bridge"]