FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV ENABLE_HEALTH=1 \
    WHISPER_SIZE=small \
    PORT=10000

EXPOSE 10000

CMD ["python", "bot.py"]
