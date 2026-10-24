import os
import sqlite3
from app.common.database import connect
from contextlib import contextmanager
import json
import logging
import hashlib
from datetime import datetime
from typing import Dict, Any, Optional

logger = logging.getLogger("StateManager")

class StateManager:
    """
    Gestor de la memoria transaccional basado en SQLite.
    Mantiene el estado de las tareas activas y sus pasos (checkpoints) para
    permitir pausar, reanudar y recuperar tras errores.
    """
    
    def __init__(self, data_dir: str = "data", db_path=None):
        self.data_dir = data_dir
        os.makedirs(self.data_dir, exist_ok=True)
        self.db_path = os.path.abspath(os.getenv("KHAI_DB_PATH") or os.path.join(self.data_dir if os.path.basename(os.path.normpath(self.data_dir)) != "memory" else os.path.dirname(self.data_dir), "openclaw.db"))
        if db_path:
            self.db_path = str(db_path)
        self._init_db()
        from app.common.database import adopt_schema
        adopt_schema(self.db_path,'legacy-state',['tasks','steps','audits'])
        
    @contextmanager
    def _get_conn(self):
        conn = connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self):
        with self._get_conn() as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    skill_name TEXT,
                    request TEXT,
                    status TEXT,
                    error_msg TEXT,
                    attempts INTEGER DEFAULT 0,
                    created_at TEXT,
                    updated_at TEXT
                )
            ''')
            
            # Migración manual para entornos existentes
            try:
                conn.execute('ALTER TABLE tasks ADD COLUMN attempts INTEGER DEFAULT 0')
            except sqlite3.OperationalError:
                pass # La columna ya existe
            try:
                conn.execute('ALTER TABLE tasks ADD COLUMN hypothesis_id TEXT')
            except sqlite3.OperationalError:
                pass # La columna ya existe
            try:
                conn.execute('ALTER TABLE tasks ADD COLUMN alternative_hypotheses TEXT')
            except sqlite3.OperationalError:
                pass # La columna ya existe
                
            conn.execute('''
                CREATE TABLE IF NOT EXISTS steps (
                    task_id TEXT,
                    step_name TEXT,
                    status TEXT,
                    data_json TEXT,
                    completed_at TEXT,
                    PRIMARY KEY (task_id, step_name)
                )
            ''')
            conn.execute('''
                CREATE TABLE IF NOT EXISTS hypothesis_contexts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    hypothesis_id INTEGER,
                    context TEXT,
                    successes INTEGER DEFAULT 0,
                    failures INTEGER DEFAULT 0,
                    UNIQUE(hypothesis_id, context)
                )
            ''')
            conn.execute('''
                CREATE TABLE IF NOT EXISTS skills_library (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    family_name TEXT,
                    version INTEGER,
                    description TEXT,
                    file_path TEXT,
                    mode TEXT,
                    is_active BOOLEAN,
                    created_at TEXT,
                    UNIQUE(family_name, version)
                )
            ''')
            conn.execute('''
                CREATE TABLE IF NOT EXISTS audits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT,
                    skill_name TEXT,
                    file_path TEXT,
                    file_hash TEXT,
                    pixy_evaluation TEXT,
                    created_at TEXT
                )
            ''')
            conn.commit()

    def get_latest_version(self, family_name: str) -> int:
        with self._get_conn() as conn:
            cur = conn.execute('SELECT MAX(version) as max_v FROM skills_library WHERE family_name = ?', (family_name,))
            row = cur.fetchone()
            return row["max_v"] if row and row["max_v"] else 0

    def save_to_library(self, family_name: str, description: str, file_path: str, mode: str):
        """Registra una habilidad. Si existe, la autoincrementa y desactiva las versiones anteriores."""
        with self._get_conn() as conn:
            now = datetime.now().isoformat()
            latest = self.get_latest_version(family_name)
            new_version = latest + 1
            
            # Desactivar todas las versiones anteriores
            conn.execute('UPDATE skills_library SET is_active = 0 WHERE family_name = ?', (family_name,))
            
            # Insertar nueva versión activa
            conn.execute('''
                INSERT INTO skills_library (family_name, version, description, file_path, mode, is_active, created_at)
                VALUES (?, ?, ?, ?, ?, 1, ?)
            ''', (family_name, new_version, description, file_path, mode, now))
            conn.commit()
            logger.info(f"Habilidad {family_name} v{new_version} guardada como activa en la librería.")

    def get_all_skills(self) -> list:
        """Obtiene todas las habilidades ACTIVAS de la librería."""
        with self._get_conn() as conn:
            cur = conn.execute('SELECT family_name as name, description, file_path, mode FROM skills_library WHERE is_active = 1')
            return [dict(row) for row in cur.fetchall()]

    def delete_skill(self, family_name: str):
        """Elimina una habilidad (todas sus versiones) de la librería."""
        with self._get_conn() as conn:
            conn.execute('DELETE FROM skills_library WHERE family_name = ?', (family_name,))
            conn.commit()
            logger.info(f"Habilidad {family_name} eliminada permanentemente de la librería SQLite.")

    def create_task(self, task_id: str, skill_name: str, request: str, hypothesis_id: str = None, alternative_hypotheses: list = None):
        """Crea o actualiza una tarea, preservando alternativas si no se pasan explícitamente."""
        alt_json = json.dumps(alternative_hypotheses) if alternative_hypotheses is not None else None
        
        with self._get_conn() as conn:
            now = datetime.now().isoformat()
            conn.execute('''
                INSERT OR IGNORE INTO tasks 
                (id, skill_name, request, status, error_msg, attempts, created_at, updated_at, hypothesis_id, alternative_hypotheses)
                VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
            ''', (task_id, skill_name, request, "CREATED", "", now, now, hypothesis_id, alt_json))
            
            if alternative_hypotheses is None:
                conn.execute('''
                    UPDATE tasks 
                    SET skill_name = ?, request = ?, status = ?, error_msg = ?, updated_at = ?,
                        hypothesis_id = COALESCE(?, hypothesis_id)
                    WHERE id = ?
                ''', (skill_name, request, "CREATED", "", now, hypothesis_id, task_id))
            else:
                conn.execute('''
                    UPDATE tasks 
                    SET skill_name = ?, request = ?, status = ?, error_msg = ?, updated_at = ?,
                        hypothesis_id = ?, alternative_hypotheses = ?
                    WHERE id = ?
                ''', (skill_name, request, "CREATED", "", now, hypothesis_id, alt_json, task_id))
            
            conn.commit()

    def promote_next_hypothesis(self, task_id: str) -> tuple[str | None, list[str]]:
        """Promueve la siguiente hipótesis alternativa y resetea la tarea para reejecución."""
        with self._get_conn() as conn:
            now = datetime.now().isoformat()
            row = conn.execute('SELECT alternative_hypotheses FROM tasks WHERE id = ?', (task_id,)).fetchone()
            if not row:
                return None, []

            alternatives = json.loads(row["alternative_hypotheses"] or "[]")
            if not alternatives:
                return None, []

            next_hypothesis = str(alternatives.pop(0))
            conn.execute('''
                UPDATE tasks
                SET hypothesis_id = ?,
                    alternative_hypotheses = ?,
                    attempts = 0,
                    status = ?,
                    error_msg = ?,
                    updated_at = ?
                WHERE id = ?
            ''', (next_hypothesis, json.dumps(alternatives), "CREATED", "", now, task_id))
            conn.commit()
            return next_hypothesis, [str(h) for h in alternatives]

    def get_task_attempts(self, task_id: str) -> int:
        with self._get_conn() as conn:
            row = conn.execute('SELECT attempts FROM tasks WHERE id = ?', (task_id,)).fetchone()
            return row["attempts"] if row and row["attempts"] else 0

    def increment_task_attempts(self, task_id: str):
        with self._get_conn() as conn:
            conn.execute('UPDATE tasks SET attempts = attempts + 1, updated_at = ? WHERE id = ?', (datetime.now().isoformat(), task_id))
            conn.commit()
        logger.info(f"Task {task_id} actualizada: intento incrementado.")

    def update_task_status(self, task_id: str, status: str, error_msg: Optional[str] = None):
        """Actualiza el estado principal de una tarea (ej. pausar para aprobación o error)."""
        with self._get_conn() as conn:
            now = datetime.now().isoformat()
            if error_msg is not None:
                conn.execute('''
                    UPDATE tasks SET status = ?, error_msg = ?, updated_at = ? WHERE id = ?
                ''', (status, error_msg, now, task_id))
            else:
                conn.execute('''
                    UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?
                ''', (status, now, task_id))
            conn.commit()

    def save_step(self, task_id: str, step_name: str, data: Any = None):
        """Guarda un checkpoint de un paso completado exitosamente."""
        with self._get_conn() as conn:
            now = datetime.now().isoformat()
            data_json = json.dumps(data, ensure_ascii=False) if data is not None else None
            conn.execute('''
                INSERT OR REPLACE INTO steps 
                (task_id, step_name, status, data_json, completed_at)
                VALUES (?, ?, ?, ?, ?)
            ''', (task_id, step_name, "COMPLETED", data_json, now))
            conn.execute('''
                UPDATE tasks SET updated_at = ? WHERE id = ?
            ''', (now, task_id))
            conn.commit()
            
    def is_step_completed(self, task_id: str, step_name: str) -> bool:
        """Verifica si un paso ya se ejecutó en el pasado (para reanudar)."""
        with self._get_conn() as conn:
            cur = conn.execute('''
                SELECT status FROM steps WHERE task_id = ? AND step_name = ?
            ''', (task_id, step_name))
            row = cur.fetchone()
            return row is not None and row["status"] == "COMPLETED"

    def get_step_data(self, task_id: str, step_name: str) -> Any:
        """Obtiene los datos cacheados de un paso previo."""
        with self._get_conn() as conn:
            cur = conn.execute('''
                SELECT data_json FROM steps WHERE task_id = ? AND step_name = ?
            ''', (task_id, step_name))
            row = cur.fetchone()
            if row and row["data_json"]:
                return json.loads(row["data_json"])
            return None

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._get_conn() as conn:
            cur = conn.execute('SELECT * FROM tasks WHERE id = ?', (task_id,))
            row = cur.fetchone()
            if not row: return None
            task = dict(row)
            
            cur_steps = conn.execute('SELECT * FROM steps WHERE task_id = ?', (task_id,))
            steps = {}
            for s in cur_steps.fetchall():
                data = json.loads(s["data_json"]) if s["data_json"] else None
                steps[s["step_name"]] = {
                    "status": s["status"],
                    "completed_at": s["completed_at"],
                    "data": data
                }
            task["steps"] = steps
            return task

    def get_pending_tasks(self) -> Dict[str, Any]:
        with self._get_conn() as conn:
            cur = conn.execute('''
                SELECT * FROM tasks 
                WHERE status IN ('RUNNING', 'WAITING_APPROVAL', 'WAITING_SKILL_APPROVAL', 'ERROR', 'WAITING_PIXY')
            ''')
            tasks = {}
            for row in cur.fetchall():
                tid = row["id"]
                tasks[tid] = dict(row)
                
                cur_steps = conn.execute('SELECT * FROM steps WHERE task_id = ?', (tid,))
                steps = {}
                for s in cur_steps.fetchall():
                    data = json.loads(s["data_json"]) if s["data_json"] else None
                    steps[s["step_name"]] = {
                        "status": s["status"],
                        "completed_at": s["completed_at"],
                        "data": data
                    }
                tasks[tid]["steps"] = steps
                
            return tasks

    def calculate_file_hash(self, file_path: str) -> str:
        """Calcula el hash SHA256 de un archivo."""
        if not os.path.exists(file_path):
            return ""
        sha256_hash = hashlib.sha256()
        with open(file_path, "rb") as f:
            for byte_block in iter(lambda: f.read(4096), b""):
                sha256_hash.update(byte_block)
        return sha256_hash.hexdigest()

    def save_audit(self, task_id: str, skill_name: str, file_path: str, pixy_evaluation: str):
        """Guarda un registro de auditoría con el hash del código ejecutado."""
        file_hash = self.calculate_file_hash(file_path)
        with self._get_conn() as conn:
            now = datetime.now().isoformat()
            conn.execute('''
                INSERT INTO audits (task_id, skill_name, file_path, file_hash, pixy_evaluation, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (task_id, skill_name, file_path, file_hash, pixy_evaluation, now))
            conn.commit()
        logger.info(f"Auditoría guardada para {skill_name} (Task {task_id}). Hash: {file_hash[:8]}...")
