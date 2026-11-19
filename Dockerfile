FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY build.py plaid_sync.py server.py template.html ./
ENV FIN_DATA=/data PYTHONUNBUFFERED=1
CMD ["python3", "server.py"]
