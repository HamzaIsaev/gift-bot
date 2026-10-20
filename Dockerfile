FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY gift_bot ./gift_bot
COPY scripts ./scripts
CMD ["python", "-m", "gift_bot"]
