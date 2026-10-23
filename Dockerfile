# Образ со встроенным Chromium — нужен для входа на площадки через Telegram Web
FROM mcr.microsoft.com/playwright/python:v1.64.0-noble
WORKDIR /app
ENV PYTHONUNBUFFERED=1 TZ=Europe/Moscow
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY gift_bot ./gift_bot
COPY scripts ./scripts
CMD ["python", "-m", "gift_bot"]
