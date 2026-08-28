FROM python:3.12-slim

LABEL org.opencontainers.image.alpaca-cli.commit="53606273aa230a40c64b783425dcb3f4423ede30"

WORKDIR /app

ARG TARGETARCH

# Alpaca CLI v0.0.14 at the labeled commit. The installer verifies Alpaca's
# published release checksum for the selected Linux architecture.
COPY install_alpaca_cli.py /tmp/install_alpaca_cli.py
RUN python /tmp/install_alpaca_cli.py --arch "${TARGETARCH}" --output /usr/local/bin/alpaca \
    && rm /tmp/install_alpaca_cli.py

COPY requirements.txt .
RUN pip install --no-cache-dir --retries 10 --timeout 120 -r requirements.txt

COPY . .

CMD ["python", "bot.py"]
