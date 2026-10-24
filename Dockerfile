FROM python:3.10-slim

# Evitar prompts interactivos durante la instalación
ENV DEBIAN_FRONTEND=noninteractive

# El Core no administra Docker ni utiliza Node.js; el proxy posee el cliente Docker.
RUN apt-get update && apt-get install -y make \
    && rm -rf /var/lib/apt/lists/*

# Khai uses its Python coordinator; no unrelated agent runtime is installed.

# Configurar directorio de trabajo
WORKDIR /app

# Instalar dependencias de Python primero (optimización de caché de Docker)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar el resto del código
COPY . .

# Comando por defecto para iniciar el orquestador
CMD ["make", "start"]
