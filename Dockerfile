FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

ENV HCWEBHOOK_HOST=0.0.0.0 \
    HCWEBHOOK_PORT=8787 \
    HCWEBHOOK_DB=/data/health_connect.sqlite \
    HCWEBHOOK_TOKEN_FILE=/run/secrets/hcwebhook_token

VOLUME ["/data"]
EXPOSE 8787
CMD ["hcwebhook-receiver"]
