FROM python:3.11-slim

# Install Node.js (needed for DeepSeek PoW WASM solver)
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates gnupg && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV ENABLE_HEALTH=1 \
    WHISPER_SIZE=small \
    PORT=10000

EXPOSE 10000

CMD ["python", "bot.py"]
