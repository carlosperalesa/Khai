FROM python:3.10-slim

WORKDIR /app

# Instalar cliente de docker para que el proxy pueda comunicarse con docker.sock
RUN apt-get update && apt-get install -y docker.io && rm -rf /var/lib/apt/lists/*

COPY docker_proxy/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY docker_proxy ./docker_proxy
COPY app ./app

EXPOSE 8001

CMD ["uvicorn", "docker_proxy.proxy:app", "--host", "0.0.0.0", "--port", "8001"]
