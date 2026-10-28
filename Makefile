.PHONY: help start stop restart logs stats setup update test clean clean-all docker-up docker-down docker-logs backupdb restoredb backuplogs restorelogs


# Variables
KHAI_CMD=python -m app.voice.voice
PYTHON_CMD=python
ENV_FILE=.env

help: ## Muestra este mensaje de ayuda
	@echo "Opciones disponibles:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

start: ## Inicia el sistema Khai
	@echo "Iniciando sistema..."
	$(KHAI_CMD)

stop: ## Detiene el sistema Khai
	@echo "Deteniendo sistema..."
	$(KHAI_CMD) stop

restart: stop start ## Reinicia el sistema

logs: ## Muestra los últimos 50 registros de logs en vivo
	@echo "Mostrando últimos logs..."
	tail -n 50 -f data/logs/*.log

stats: ## Muestra estado del sistema
	@docker exec khai_core python -c "from app.bootstrap import ModuleManager; m = ModuleManager(); print(m.heart.check_integrity())"

health: ## Alias para stats
	@docker exec khai_core python -c "from app.bootstrap import ModuleManager; m = ModuleManager(); print(m.heart.check_integrity())"

setup: ## Configura el entorno inicial (carpetas y BD)
	@echo "Creando directorios y configurando BD..."
	$(PYTHON_CMD) -m scripts.migrate_architecture
	@echo "Setup completado."

update: ## Actualiza el repositorio e instala nuevas dependencias
	@echo "Actualizando sistema..."
	git pull origin main
	pip install -r requirements.txt
	@echo "Actualización completada."

test: ## Pruebas locales; Docker, Telegram y LLM simulados
	$(PYTHON_CMD) -B -m unittest discover -s tests -v

migrate: ## Migración offline con copia SQLite consistente; detener servicios primero
	$(PYTHON_CMD) -m scripts.migrate_architecture

clean: ## Limpia archivos temporales, logs > 7 días y caché (multiplataforma)
	$(PYTHON_CMD) -m scripts.clean

clean-all: ## Limpia temporales, caché y elimina el entorno virtual .venv
	$(PYTHON_CMD) -m scripts.clean --include-venv


docker-up: ## Levanta el sistema completo usando Docker Compose (en segundo plano)
	@echo "Levantando contenedor Docker..."
	docker compose up -d --build

docker-down: ## Detiene y elimina el contenedor de Docker
	@echo "Deteniendo contenedor..."
	docker compose down

docker-logs: ## Muestra los logs del contenedor Docker en vivo
	docker compose logs -f --tail=50 bot

backupdb: ## Respalda offline datos y bases SQLite mediante backup consistente
	$(PYTHON_CMD) -m scripts.backup_data backup

restoredb: ## Restaura a carpeta nueva: make restoredb BACKUP=... DEST=...
	$(PYTHON_CMD) -m scripts.backup_data restore --source "$(BACKUP)" --output "$(DEST)"

backuplogs: ## Comprime las carpetas de logs para sincronización
	@echo "Realizando backup de los logs..."
	@mkdir -p data/logs data/agent_logs
	tar -czf data/logs_backup.tar.gz -C data logs agent_logs

restorelogs: ## Descomprime las carpetas de logs
	@echo "Restaurando logs..."
	@mkdir -p data
	tar -xzf data/logs_backup.tar.gz -C data


