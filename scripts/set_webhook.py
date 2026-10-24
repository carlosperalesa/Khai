import os
import requests
from dotenv import load_dotenv
import logging

logging.basicConfig(level=logging.INFO, format='%(message)s')

load_dotenv()
from app.voice.secrets import Secrets
BOT_TOKEN = Secrets(os.getenv("DATA_DIR", "data")).get("TELEGRAM_BOT_TOKEN")
DROPLET_IP = os.getenv("DROPLET_IP", "127.0.0.1")
PORT = 8443

if not BOT_TOKEN:
    logging.error("TELEGRAM_BOT_TOKEN no encontrado en el entorno.")
    exit(1)

WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
if len(WEBHOOK_SECRET) < 32:
    raise SystemExit("Configura TELEGRAM_WEBHOOK_SECRET (mínimo 32 caracteres)")

cert_path = "data/certs/cert.pem"
if not os.path.exists(cert_path):
    logging.error(f"No se encontró el certificado en {cert_path}")
    exit(1)

url = f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook"
webhook_url = f"https://{DROPLET_IP}:{PORT}/webhook"

logging.info(f"Configurando Webhook en: {webhook_url}")

with open(cert_path, "rb") as cert_file:
    response = requests.post(
        url,
        data={"url": webhook_url, "secret_token": WEBHOOK_SECRET},
        files={"certificate": cert_file}, timeout=30
    )

if response.status_code == 200:
    logging.info("¡Webhook configurado exitosamente en Telegram!")
    logging.info(response.json())
else:
    logging.error("Error configurando Webhook.")
    logging.error("HTTP %s", response.status_code)
