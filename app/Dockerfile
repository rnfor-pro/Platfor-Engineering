FROM alpine:latest AS cert

RUN apk add --no-cache bash ca-certificates curl && \
    curl -fsSL http://ppa.etech.com/certs/ca/keyfactorswroot -o /usr/local/share/ca-certificates/etechroot.crt && \
    curl -fsSL https://ppa.etech.com/certs/ca/chain -o /usr/local/share/ca-certificates/etech-internal-chain.crt && \
    update-ca-certificates

FROM python:3.12-slim

LABEL org.opencontainers.image.title="github-copilot-exporter"
LABEL org.opencontainers.image.version="v5"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates openssl && \
    rm -rf /var/lib/apt/lists/*

COPY --from=cert /usr/local/share/ca-certificates/etechroot.crt /usr/local/share/ca-certificates/etechroot.crt
COPY --from=cert /usr/local/share/ca-certificates/etech-internal-chain.crt /usr/local/share/ca-certificates/etech-internal-chain.crt

RUN update-ca-certificates

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

RUN groupadd --gid 10001 appgroup && \
    useradd --uid 10001 --gid 10001 --home-dir /app --no-create-home --shell /usr/sbin/nologin appuser && \
    chown -R 10001:10001 /app

USER 10001:10001

EXPOSE 8080

CMD ["python", "/app/main.py"]