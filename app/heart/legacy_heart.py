import os
import json
import logging
import asyncio
import psutil
from datetime import datetime, timedelta

from app.voice.gateway.llm_gateway import LLMManager
from app.mind.memory import MemoryEngine
from app.voice.gateway.telegram_gateway import TelegramHandler
import importlib

logger = logging.getLogger("CoreHeart")

class CoreHeart:
    """
    Khai's Heart V3: Sistema de consolidación de memoria y latidos.
    - Fast Beat: Determinista y barato. Gatillado bajo demanda.
    - Slow Beat: Analítico, usa LLM para generar Hipótesis y limpiar la memoria.
    """
    
    def __init__(self, llm: LLMManager, memory: MemoryEngine, telegram: TelegramHandler, data_dir: str, state_manager=None, pixy=None):
        self.llm = llm
        self.memory = memory
        self.telegram = telegram
        self.data_dir = data_dir
        self.state = state_manager
        self.pixy = pixy
        # BUG #8 FIX: data_dir ahora es la raíz "data", no "data/publisher"
        self.log_path = os.path.join(data_dir, "logs", "publisher.log")
        
        self.running = False
        
        # Estado del Fast Beat
        self.anomaly_monitor_until = datetime.min
        self._alerted_resources = {"ram": False, "disk": False}
        self.last_log_size = 0
        self._hypothesis_confidence_snapshots = {}
        
        # Estado del Slow Beat
        self._last_slow_beat = datetime.min

    async def start_beating(self):
        """Inicia el motor de latidos V3."""
        self.running = True
        logger.info("❤️ CoreHeart V3 iniciado. Latido basal activado.")
        
        if os.path.exists(self.log_path):
            self.last_log_size = os.path.getsize(self.log_path)
            
        while self.running:
            try:
                # El Fast Beat opera de dos formas:
                # 1. Modo Basal (ligero, cada 60s)
                # 2. Modo Anomalía (intenso, cada 10s) gatillado por Khai
                
                now = datetime.now()
                is_anomaly_mode = now < self.anomaly_monitor_until
                
                if is_anomaly_mode:
                    await self._fast_beat_intense()
                    await asyncio.sleep(10)
                else:
                    await self._fast_beat_basal()
                    await asyncio.sleep(60)
                    
                # El Slow Beat opera en ciclos largos (ej. cada 1 hora, para dev: 5 min)
                if (now - self._last_slow_beat).total_seconds() >= 300: # 5 minutos para debug
                    await self._slow_beat_cognitive()
                    self._last_slow_beat = now
                    
            except Exception as e:
                logger.error(f"Error en el ciclo del corazón: {e}")
                await asyncio.sleep(10)

    # ==========================================
    # FAST BEAT (Determinista y Barato)
    # ==========================================
    def trigger_anomaly_monitor(self, minutes: int = 5):
        """Gatillado por Khai tras crear o modificar un agente."""
        self.anomaly_monitor_until = datetime.now() + timedelta(minutes=minutes)
        self.telegram.send_message("🫀 <b>CoreHeart:</b> Latido rápido gatillado. Monitoreando anomalías intensamente...")

    async def _fast_beat_basal(self):
        """Chequeo ligero de supervivencia."""
        await self._check_vital_signs()

    async def _fast_beat_intense(self):
        """Chequeo profundo de logs, memoria y agentes caídos (Sin LLM)."""
        await self._check_vital_signs()
        await self._check_new_errors()
        await self._check_dead_agents()
        await self._check_behavioral_drift()

    async def _check_behavioral_drift(self):
        """Revisa si Khai ha entrado en un bucle ciego de regeneración o explosión de skills."""
        if not self.state or not self.pixy: return
        
        pending = self.state.get_pending_tasks()
        
        # Evaluar bucles de regeneración en la misma tarea (Error Diversity)
        for tid, tdata in pending.items():
            if tdata.get("status") in ["RUNNING", "WAITING_APPROVAL", "ERROR", "WAITING_PIXY"]:
                # Buscar en memoria los eventos de error de esta tarea
                events = self.memory.search_events(f"TASK {tid}", limit=20)
                error_outputs = [e["content"] for e in events if "ERROR DEFINITIVO" in e["content"]]
                
                if len(error_outputs) >= 4:
                    # Verificar diversidad de los últimos 4 errores
                    unique_errors = set([err.strip() for err in error_outputs[-4:]])
                    if len(unique_errors) == 1:
                        hyp_id = tdata.get("hypothesis_id")
                        if hyp_id:
                            hypothesis = self.memory.get_hypothesis(hyp_id)
                            current_confidence = hypothesis.get("confidence") if hypothesis else None
                            snapshot = self._hypothesis_confidence_snapshots.get(tid)
                            self._hypothesis_confidence_snapshots[tid] = {
                                "hypothesis_id": hyp_id,
                                "confidence": current_confidence,
                            }
                            if (
                                snapshot
                                and snapshot.get("hypothesis_id") == hyp_id
                                and current_confidence is not None
                                and snapshot.get("confidence") is not None
                                and current_confidence < snapshot.get("confidence")
                            ):
                                logger.info(
                                    "FastBeat: tarea %s repite error, pero la confianza de hipótesis bajó %.3f -> %.3f; no se congela.",
                                    tid,
                                    snapshot.get("confidence"),
                                    current_confidence,
                                )
                                continue
                            if not snapshot:
                                logger.info(
                                    "FastBeat: snapshot inicial de confianza para tarea %s / hipótesis %s.",
                                    tid,
                                    hyp_id,
                                )
                                continue

                        msg = f"Behavioral Drift: La tarea {tid} está en un BUCLE CIEGO repitiendo exactamente el mismo error {len(error_outputs)} veces."
                        logger.warning(msg)
                        self.state.update_task_status(tid, "FROZEN_BY_PIXY")
                        self.pixy.trigger_panic_mode(msg)
                    
        # Evaluar abuso de CoreEyes (Token Factory)
        recent_events = self.memory.get_recent_events(limit=20)
        last_code_idx = -1
        for idx, event in enumerate(recent_events):
            if event.get("type") == "code_generated":
                last_code_idx = idx

        events_after_code = recent_events[last_code_idx + 1:]
        recent_eyes = [
            event for event in events_after_code
            if event.get("metadata", {}).get("source") == "CoreEyes" or "Evidencia EYES" in event.get("content", "")
        ]
        if len(recent_eyes) >= 3:
            last_three = recent_eyes[-3:]
            signatures = {
                (
                    event.get("metadata", {}).get("resource_id"),
                    event.get("metadata", {}).get("output_hash"),
                )
                for event in last_three
            }
            has_hashes = all(event.get("metadata", {}).get("output_hash") for event in last_three)
            if has_hashes and len(signatures) == 1:
                resource_id, output_hash = next(iter(signatures))
                msg = (
                    "Behavioral Drift: Khai está leyendo repetidamente el mismo output de CoreEyes "
                    f"({resource_id}, hash {output_hash[:8]}) sin producir código."
                )
                logger.warning(msg)
                self.pixy.trigger_panic_mode(msg)

        # Evaluar explosión de tareas (ej > 15 tareas pending)
        if len(pending) > 15:
            msg = f"Behavioral Drift: Explosión de tareas detectada ({len(pending)} pending). Posible descontrol."
            logger.warning(msg)
            self.pixy.trigger_panic_mode(msg)

    async def _check_vital_signs(self):
        """Revisa RAM y Disco de forma determinista."""
        ram = psutil.virtual_memory()
        disk = psutil.disk_usage('/')
        
        # Lógica simplificada de RAM
        if ram.percent >= 90.0 and not self._alerted_resources["ram"]:
            self.notify_error("CoreHeart.FastBeat", Exception(f"RAM Crítica al {ram.percent}%"))
            self._alerted_resources["ram"] = True
        elif ram.percent < 85.0:
            self._alerted_resources["ram"] = False

    async def _check_new_errors(self):
        """Ya no guardamos logs basura. Los errores se evaluarán contextual y cognitivamente a través de Khai y el StateManager."""
        pass

    async def _check_dead_agents(self):
        """Consulta contenedores colapsados."""
        import requests
        proxy_url = os.environ.get("PROXY_URL", "http://docker_proxy:8001/run-skill").replace("/run-skill", "/cleanup-agents")
        try:
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(None, lambda: requests.get(proxy_url, timeout=10))
            if response.status_code == 200:
                dead_agents = response.json().get("dead_agents", [])
                for agent_data in dead_agents:
                    agent_name = agent_data.get("agent")
                    # Enviar al Graveyard
                    self.memory.append_long_term("agent_death", f"Agente '{agent_name}' crasheó repentinamente. Revisar logs del contenedor.")
        except:
            pass

    def notify_error(self, location: str, error: Exception):
        """Canal determinista de emergencia hacia el usuario."""
        logger.error(f"Error en {location}: {error}")
        msg = f"🫀 <b>CoreHeart (Exception)</b>\n<b>Loc:</b> {location}\n<b>Err:</b> <code>{str(error)}</code>"
        try:
            self.telegram.send_message(text=msg)
        except:
            pass

    # ==========================================
    # DIAGNÓSTICO BAJO DEMANDA (Invocable por Pixy)
    # ==========================================
    def run_diagnostic_fast(self) -> dict:
        """
        FastBeat síncrono para diagnóstico bajo demanda.
        Retorna un dict con el estado vital del sistema sin usar LLM.
        Pixy puede invocar esto directamente.
        """
        import psutil
        
        report = {
            "ram": {},
            "disk": {},
            "log_errors": [],
            "integrity": self.check_integrity()
        }
        
        # RAM
        ram = psutil.virtual_memory()
        report["ram"] = {
            "total_gb": round(ram.total / (1024**3), 2),
            "used_pct": ram.percent,
            "available_mb": round(ram.available / (1024 * 1024), 0),
            "status": "critical" if ram.percent >= 90 else "warning" if ram.percent >= 75 else "ok"
        }
        
        # Disco
        try:
            disk = psutil.disk_usage('/')
            report["disk"] = {
                "total_gb": round(disk.total / (1024**3), 2),
                "used_pct": round(100 * disk.used / disk.total, 1),
                "free_gb": round(disk.free / (1024**3), 2),
                "status": "critical" if disk.free < 1 * (1024**3) else "warning" if disk.free < 3 * (1024**3) else "ok"
            }
        except Exception:
            report["disk"] = {"status": "unavailable"}
        
        # Errores recientes en logs
        if os.path.exists(self.log_path):
            try:
                with open(self.log_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                    lines = content.split('\n')
                    error_lines = [l for l in lines[-100:] if 'ERROR' in l or 'CRITICAL' in l]
                    report["log_errors"] = error_lines[-10:]
            except Exception:
                pass
        
        # Dead agents (síncrono)
        import requests
        proxy_url = os.environ.get("PROXY_URL", "http://docker_proxy:8001/run-skill").replace("/run-skill", "/cleanup-agents")
        try:
            resp = requests.get(proxy_url, timeout=10)
            if resp.status_code == 200:
                report["dead_agents"] = resp.json().get("dead_agents", [])
            else:
                report["dead_agents"] = []
        except Exception:
            report["dead_agents"] = []
        
        return report

    def run_diagnostic_slow(self) -> str:
        """
        SlowBeat síncrono para diagnóstico cognitivo bajo demanda.
        Analiza el Graveyard, genera hipótesis y ejecuta garbage collection.
        Retorna un resumen textual del resultado.
        Pixy puede invocar esto directamente.
        """
        import asyncio
        
        results = []
        
        # Ejecutar el ciclo cognitivo
        try:
            loop = asyncio.new_event_loop()
            loop.run_until_complete(self._slow_beat_cognitive())
            loop.close()
            results.append("Ciclo cognitivo ejecutado correctamente.")
        except RuntimeError:
            # Si ya hay un event loop corriendo, usar run_in_executor
            try:
                loop = asyncio.get_event_loop()
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    future = pool.submit(self._run_slow_beat_sync)
                    future.result(timeout=60)
                results.append("Ciclo cognitivo ejecutado (via thread).")
            except Exception as e:
                results.append(f"Error ejecutando ciclo cognitivo: {e}")
        except Exception as e:
            results.append(f"Error ejecutando ciclo cognitivo: {e}")
        
        # Reporte de hipótesis activas
        hypotheses = self.memory.get_active_hypotheses()
        results.append(f"Hipótesis activas: {len(hypotheses)}")
        for h in hypotheses[:5]:
            results.append(f"  - [{h.get('topic', '?')}] {h.get('content', '?')[:80]}... (conf: {h.get('confidence', '?')})")
        
        return "\n".join(results)

    def register_multiple_hypotheses(self, task_id: str, causes: list, evidence: str) -> tuple[str, list]:
        """Registra múltiples hipótesis (Causal Layer) y devuelve (ID principal, [IDs alternativas])."""
        if not causes:
            return None, []
            
        # Ordenar por confianza descendente
        causes = sorted(causes, key=lambda x: x.get("confidence", 0.0), reverse=True)
        
        main_cause = causes[0]
        content = f"Causa Raíz Probable: {main_cause.get('cause')}\nEvidencia: {evidence}\nFix: {main_cause.get('proposed_fix')}"
        main_id = self.memory.add_hypothesis(f"Error Task {task_id}", content, initial_confidence=main_cause.get("confidence", 0.5))
        
        alt_ids = []
        for alt in causes[1:]:
            alt_content = f"Causa Raíz Alternativa: {alt.get('cause')}\nEvidencia: {evidence}\nFix: {alt.get('proposed_fix')}"
            alt_id = self.memory.add_hypothesis(f"Error Task {task_id} (Alt)", alt_content, initial_confidence=alt.get("confidence", 0.5))
            alt_ids.append(alt_id)
            
        msg = f"🧪 <b>CoreHeart (Capa Causal):</b> Registré la hipótesis <code>#{main_id}</code> ({main_cause.get('confidence', 0)})."
        if alt_ids:
            msg += f"\n<i>Alternativas guardadas: {', '.join(map(str, alt_ids))}</i>"
            
        try:
            self.telegram.send_message(text=msg)
        except: pass
        
        return str(main_id), [str(i) for i in alt_ids]

    def _run_slow_beat_sync(self):
        """Helper síncrono para ejecutar el slow beat en un thread."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._slow_beat_cognitive())
        finally:
            loop.close()

    # ==========================================
    # SLOW BEAT (Cognitivo y Científico)
    # ==========================================
    async def _slow_beat_cognitive(self):
        """
        Ciclo Analítico Lento:
        1. Lee el Graveyard reciente.
        2. Genera Hipótesis.
        3. Realiza Garbage Collection de Guidelines.
        """
        logger.info("🫀 CoreHeart: Iniciando ciclo cognitivo (Slow Beat)...")
        await self._generate_hypotheses_from_graveyard()
        await self._cognitive_garbage_collector()

    async def _generate_hypotheses_from_graveyard(self):
        """Analiza errores recientes y propone hipótesis al sistema."""
        # Extraer logs recientes (Graveyard)
        recent_errors = self.memory.search_events("", event_type="system_error") + self.memory.search_events("", event_type="agent_death")
        
        # Si hay menos de 3 errores, no hay suficientes datos para generalizar
        if len(recent_errors) < 3:
            return
            
        logs_text = "\\n".join([e["content"] for e in recent_errors[-10:]])
        
        prompt = f"""
        Eres el subsistema cognitivo de OpenClaw. Analiza los siguientes eventos de error extraídos del Graveyard.
        No debes generar soluciones inmediatas ni código.
        Debes formular una HIPÓTESIS general que prevenga estos errores en el futuro, si identificas un patrón claro.
        
        Si los errores son aislados o ruidosos, responde "NO_PATTERN".
        Si hay un patrón, responde estrictamente en este formato JSON:
        {{
            "topic": "palabra_clave_del_tema",
            "hypothesis": "La hipótesis científica sobre cómo mitigar esto a nivel sistémico."
        }}
        
        Logs recientes:
        {logs_text}
        """
        
        try:
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(None, self.llm.generate_content, prompt)
            
            if "NO_PATTERN" not in response:
                # Limpiar Markdown si el LLM lo añade
                if response.startswith("```json"): response = response[7:-3]
                elif response.startswith("```"): response = response[3:-3]
                
                data = json.loads(response.strip())
                if "topic" in data and "hypothesis" in data:
                    self.memory.add_hypothesis(data["topic"], data["hypothesis"])
                    self.telegram.send_message(f"🧠 <b>CoreHeart (Nueva Hipótesis):</b>\nTema: {data['topic']}\n<i>{data['hypothesis']}</i>")
                    
                    # Limpiar los eventos analizados para no reprocesarlos
                    with self.memory._get_conn() as conn:
                        conn.execute("UPDATE long_term_events SET event_type = event_type || '_analyzed' WHERE id IN (SELECT id FROM long_term_events WHERE event_type IN ('system_error', 'agent_death') ORDER BY id LIMIT ?)", (len(recent_errors),))
                        conn.commit()
                        
        except Exception as e:
            logger.error(f"Fallo generando hipótesis: {e}")

    async def _cognitive_garbage_collector(self):
        """Retira conocimiento obsoleto y promueve hipótesis exitosas."""
        # LearningLedger owns verified feedback, candidate creation and retirement.
        return

    # --- Integrity Check (BUG #7 FIX: campos alineados con format_health) ---
    def check_integrity(self) -> dict:
        """Verifica integridad estructural básica."""
        file_check = self._check_file_structure()
        modules_check = self._check_modules_health()
        config_check = self._check_config_validity()
        
        overall = "ok"
        if file_check["status"] == "error" or modules_check["status"] == "error" or config_check["status"] == "error":
            overall = "error"
        
        return {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "file_structure": file_check,
            "modules_health": modules_check,
            "config_validity": config_check,
            "status": overall
        }

    def _check_file_structure(self) -> dict:
        required_paths = ["modules/__init__.py", "modules/memory_engine.py", "config/config.yaml"]
        missing = [p for p in required_paths if not os.path.exists(p)]
        if missing:
            return {"status": "error", "missing": missing}
        return {"status": "ok"}

    def _check_modules_health(self) -> dict:
        """Verifica que todos los módulos core se puedan importar."""
        required_modules = [
            "modules.core_mind", "modules.skill_forge", "modules.pixy",
            "modules.core_eyes", "modules.memory_engine", "modules.state_manager"
        ]
        failed = []
        for mod_name in required_modules:
            try:
                importlib.import_module(mod_name)
            except Exception as e:
                failed.append(f"{mod_name}: {e}")
        if failed:
            return {"status": "error", "failed": failed}
        return {"status": "ok"}

    def _check_config_validity(self) -> dict:
        """Verifica que los archivos de configuración sean parseables."""
        import yaml
        configs = ["config/config.yaml", "config/constitution.yaml"]
        errors = []
        for cfg in configs:
            if not os.path.exists(cfg):
                errors.append(f"{cfg}: no existe")
                continue
            try:
                with open(cfg, "r", encoding="utf-8") as f:
                    yaml.safe_load(f)
            except Exception as e:
                errors.append(f"{cfg}: {e}")
        if errors:
            return {"status": "error", "errors": errors}
        return {"status": "ok"}
