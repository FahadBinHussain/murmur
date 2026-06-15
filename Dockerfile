FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*
COPY messenger-bridge-linux.part.* /app/
RUN cat /app/messenger-bridge-linux.part.* > /app/messenger-bridge && rm /app/messenger-bridge-linux.part.* && chmod +x /app/messenger-bridge
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
