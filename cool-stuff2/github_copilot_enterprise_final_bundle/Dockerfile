FROM alpine:3.20 AS certs

# Pull internal CA certificates used in the enterprise environment.
RUN apk add --no-cache ca-certificates curl && \
    curl -fsSL http://ppa.etech.com/certs/ca/keyfactorswroot -o /usr/local/share/ca-certificates/etechroot.crt && \
    curl -fsSL https://ppa.etech.com/certs/ca/chain -o /usr/local/share/ca-certificates/etech-internal-chain.crt && \
    update-ca-certificates

FROM python:3.12-slim

WORKDIR /app

# Trust internal CA chain in the final runtime image.
COPY --from=certs /usr/local/share/ca-certificates/etechroot.crt /usr/local/share/ca-certificates/etechroot.crt
COPY --from=certs /usr/local/share/ca-certificates/etech-internal-chain.crt /usr/local/share/ca-certificates/etech-internal-chain.crt

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && \
    update-ca-certificates && \
    rm -rf /var/lib/apt/lists/*

ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt

COPY requirements.txt /app/requirements.txt

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r /app/requirements.txt

COPY github_copilot_enterprise_exporter.py /app/github_copilot_enterprise_exporter.py

# Non-root runtime user.
RUN useradd -r -u 10001 appuser && chown -R appuser:appuser /app
USER 10001

EXPOSE 8080

CMD ["python", "/app/github_copilot_enterprise_exporter.py"]
