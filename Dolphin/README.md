# Dolphin para Khai: Arquitectura Híbrida (Beta V4)

Este directorio agrupa la documentación y los controladores de infraestructura para el nodo conversacional **Dolphin (`dolphin3:8b`)** de Khai, separados por proveedor de nube:

- **[`gcloud/`](./gcloud/PILOTO_GCLOUD.md) (Implementación activa — Beta V4):**
  - **Arquitectura Opción A:** Un único disco persistente `dolphin-boot-disk` (`pd-standard`, `32 GB`, `auto-delete=no`) en Google Cloud (`gen-lang-client-0149640270`, zona `us-west1-a`) con Ubuntu 24.04 LTS, Ollama, `dolphin3:8b`, túnel privado WireGuard (`10.200.0.2/24 <-> 10.200.0.1/24` hacia `vm-carlosperales` en Azure), temporizador `systemd` de auto-apagado tras **5 minutos** de inactividad y drivers precompilados NVIDIA L4 (`580-server-open-gcp`).
  - **Modos desde Telegram (`/mode` o `/m`) y CLI ([`gcloud/gcloud-ollama.ps1`](./gcloud/gcloud-ollama.ps1)):**
    - `/mode low` o `/m low`: Usa **Gemini** y, si la VM Spot de Dolphin está encendida, la elimina de inmediato conservando `dolphin-boot-disk` en reposo.
    - `/mode mid` o `/m mid`: Usa **Dolphin low** creando/encendiendo `dolphin-node` como VM Spot CPU (`e2-standard-8`, 8 vCPU / 32 GB RAM, `~$152 CLP/h`).
    - `/mode high` o `/m high`: Usa **Dolphin high** creando/encendiendo `dolphin-node` como VM Spot GPU (`g2-standard-4`, 4 vCPU / 16 GB RAM + 1x NVIDIA L4 24 GB VRAM, `~$390-$446 CLP/h`).
    - `/mode ?` o `/m ?`: Consulta el estado actual (`[Mode = x]`), el estado de la VM y del disco en GCP, si el consumo está cubierto por créditos, el gasto acumulado en CLP y el crédito disponible estimado.
    - **Notificación de transición:** Cada vez que se pasa de un modelo a otro (explícitamente o cuando Dolphin se apaga tras 5 minutos de inactividad y vuelve a Gemini), Khai notifica con **`[Mode = low]`**, **`[Mode = mid]`** o **`[Mode = high]`**.
  - **Alarmas de facturación en `01074F-B289A3-A589D8` (`CLP`):** Dos presupuestos escalonados cada **`$1.000 CLP` (`~1 USD`)** del 10 % al 100 % de `$10.000 CLP`: uno para consumo bruto de créditos (`[CREDITOS] Khai Dolphin Consumo Bruto (cada 1.000 CLP)`) y otro para cobro neto fuera de créditos (`[COBRO REAL] Khai Dolphin Gasto Neto (cada 1.000 CLP)`).

- **[`azure/`](./azure/PILOTO_AZURE.md) (Diseño alternativo y controlador legado en Azure):**
  - Conserva el análisis del piloto en Azure subscription 1 ([`azure/PILOTO_AZURE.md`](./azure/PILOTO_AZURE.md)) y los scripts originales ([`azure/azure-ollama.ps1`](./azure/azure-ollama.ps1) y [`azure/setup-dependencies.ps1`](./azure/setup-dependencies.ps1)).
