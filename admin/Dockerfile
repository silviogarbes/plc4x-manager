FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends openssl curl git && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL https://github.com/docker/compose/releases/download/v2.32.4/docker-compose-linux-x86_64 -o /usr/local/bin/docker-compose \
    && chmod +x /usr/local/bin/docker-compose

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN sed -i 's/\r$//' /app/entrypoint.sh && chmod +x /app/entrypoint.sh

RUN addgroup --system plc4x && adduser --system --ingroup plc4x plc4x
# admin needs docker socket access
RUN usermod -aG docker plc4x 2>/dev/null || true
RUN chown -R plc4x:plc4x /app
RUN apt-get update && apt-get install -y --no-install-recommends gosu && rm -rf /var/lib/apt/lists/*

EXPOSE 8080 8443

HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=2 \
  CMD curl -sf http://localhost:8080/healthz || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
