import requests
import json
import time
from typing import Dict, List, Optional
import logging
import functools
from app.common.utils import retry

logger = logging.getLogger("TelegramHandler")


def private_transport(cls):
    def wrap(method):
        @functools.wraps(method)
        def call(*args,**kwargs):
            try:
                return method(*args,**kwargs)
            except requests.RequestException:
                raise RuntimeError('Telegram transport failed') from None
        return call
    for name,method in list(vars(cls).items()):
        if callable(method) and not name.startswith('_'):
            setattr(cls,name,wrap(method))
    return cls

@private_transport
class TelegramHandler:
    """Manejador centralizado de Telegram usando requests (Long-Polling option A)"""
    
    def __init__(self, bot_token: str, user_id: str):
        if not bot_token or not user_id:
            raise ValueError("bot_token and user_id must be provided")
        self.bot_token = bot_token
        self.user_id = user_id
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
        self.last_update_id = None

    def send_document(self, path):
        with open(path,'rb') as stream:
            try:
                response = requests.post(self.base_url+'/sendDocument',data={'chat_id':self.user_id},
                                         files={'document':stream},timeout=30)
                response.raise_for_status()
                return response.json()
            except requests.RequestException:
                raise RuntimeError('Telegram document delivery failed') from None
    
    @retry(max_attempts=3, delay=2)
    def send_message(
        self,
        text: str,
        buttons: Optional[List[Dict]] = None,
        parse_mode: str = "HTML",
        image_path: Optional[str] = None
    ) -> Dict:
        """Enviar mensaje con botones opcionales"""
        
        payload = {
            "chat_id": self.user_id,
            "text": text,
            "parse_mode": parse_mode
        }
        
        if not parse_mode:
            payload.pop("parse_mode", None)

        if buttons:
            reply_markup = {
                "inline_keyboard": [
                    [{"text": btn['label'], "callback_data": btn['value']}]
                    for btn in buttons
                ]
            }
            payload["reply_markup"] = json.dumps(reply_markup)
        
        try:
            if image_path:
                with open(image_path, 'rb') as f:
                    files = {'photo': f}
                    # When sending photo, caption is used instead of text
                    payload['caption'] = payload.pop('text')
                    response = requests.post(
                        f"{self.base_url}/sendPhoto",
                        data=payload,
                        files=files,
                        timeout=30
                    )
            else:
                response = requests.post(
                    f"{self.base_url}/sendMessage",
                    json=payload,
                    timeout=10
                )
            
            response.raise_for_status()
            return response.json()
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 400 and payload.get("parse_mode"):
                logger.warning("Telegram rechazó el parse_mode (posibles etiquetas no cerradas). Reintentando en texto plano...")
                payload.pop("parse_mode", None)
                if image_path:
                    with open(image_path, 'rb') as f:
                        response = requests.post(f"{self.base_url}/sendPhoto", data=payload, files={'photo': f}, timeout=30)
                else:
                    response = requests.post(f"{self.base_url}/sendMessage", json=payload, timeout=10)
                response.raise_for_status()
                return response.json()
            raise
    
    def edit_message(self, message_id: int, text: str, parse_mode: str = "HTML") -> Dict:
        """Editar un mensaje existente por su message_id."""
        payload = {
            "chat_id": self.user_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": parse_mode
        }
        try:
            response = requests.post(
                f"{self.base_url}/editMessageText",
                json=payload,
                timeout=10
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.HTTPError as e:
            if e.response and e.response.status_code == 400:
                # Telegram rechaza edición si el texto no cambió o parse_mode falla
                payload.pop("parse_mode", None)
                try:
                    response = requests.post(f"{self.base_url}/editMessageText", json=payload, timeout=10)
                    response.raise_for_status()
                    return response.json()
                except:
                    pass
            logger.warning(f"Error editando mensaje {message_id}: {type(e).__name__}")
            return {}
        except Exception as e:
            logger.warning(f"Error editando mensaje {message_id}: {type(e).__name__}")
            return {}
        
    def send_chat_action(self, action: str = "typing") -> Dict:
        """Enviar acción de chat (ej: typing, upload_video)"""
        payload = {
            "chat_id": self.user_id,
            "action": action
        }
        try:
            response = requests.post(
                f"{self.base_url}/sendChatAction",
                json=payload,
                timeout=5
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.warning(f"Error enviando chat action: {type(e).__name__}")
            return {}
    
    def wait_for_response(self, timeout_seconds: int = 1800, poll_interval: int = 3) -> Optional[str]:
        """Esperar respuesta del usuario usando long-polling (Opción A)"""
        logger.info(f"Esperando respuesta de Telegram por máximo {timeout_seconds}s...")
        start_time = time.time()
        
        # Limpiar actualizaciones previas (obtener el último offset)
        try:
            resp = requests.get(f"{self.base_url}/getUpdates", timeout=10).json()
            if resp.get('ok') and resp.get('result'):
                self.last_update_id = resp['result'][-1]['update_id']
        except Exception as e:
            logger.warning(f"Error limpiando actualizaciones previas: {type(e).__name__}")
        
        while time.time() - start_time < timeout_seconds:
            try:
                params = {"timeout": 10}
                if self.last_update_id:
                    params["offset"] = self.last_update_id + 1
                    
                response = requests.get(f"{self.base_url}/getUpdates", params=params, timeout=15)
                data = response.json()
                
                if data.get("ok") and data.get("result"):
                    for update in data["result"]:
                        self.last_update_id = update["update_id"]
                        
                        # Revisar si es un callback query (botón)
                        if "callback_query" in update:
                            choice = update["callback_query"]["data"]
                            logger.info("Callback recibido")
                            
                            # Responder al callback para quitar el estado de carga en la app
                            requests.post(f"{self.base_url}/answerCallbackQuery", 
                                        json={"callback_query_id": update["callback_query"]["id"]})
                            return choice
                            
                        # O si es un mensaje de texto normal
                        elif "message" in update and "text" in update["message"]:
                            # Filtrar solo mensajes del user_id configurado
                            if str(update["message"]["from"]["id"]) == str(self.user_id):
                                text = update["message"]["text"]
                                logger.info("Mensaje recibido")
                                return text
                                
            except requests.exceptions.RequestException as e:
                logger.warning(f"Error haciendo polling a Telegram: {type(e).__name__}")
            
            time.sleep(poll_interval)
            
        logger.warning("Timeout de Telegram excedido.")
        return None
        
    @retry(max_attempts=2, delay=5)
    def send_video_preview(self, video_path: str, caption: str) -> Dict:
        """Enviar video a Telegram"""
        with open(video_path, 'rb') as f:
            files = {'video': f}
            payload = {
                "chat_id": self.user_id,
                "caption": caption,
                "parse_mode": "HTML"
            }
            response = requests.post(
                f"{self.base_url}/sendVideo",
                data=payload,
                files=files,
                timeout=300 # Videos can take a long time to upload
            )
        
        response.raise_for_status()
        return response.json()
