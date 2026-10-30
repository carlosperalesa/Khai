# Dolphin para Khai: Arquitectura Híbrida (Beta V4)

Este directorio agrupa la documentación y los controladores de infraestructura para los nodos conversacionales privados y acelerados de Khai (**`dolphin3:8b`** y **`qwen2.5-32b-abliterated`**), separados por proveedor de nube:

- **[`gcloud/`](./gcloud/PILOTO_GCLOUD.md) (Implementación activa — Beta V4):**
  - **Arquitectura Opción A:** Un único disco persistente `dolphin-boot-disk` (`pd-standard`, `100 GB`, `auto-delete=no`) en Google Cloud (`gen-lang-client-0149640270`, zona `us-west1-a`) con Ubuntu 24.04 LTS, Ollama, túnel privado WireGuard (`10.200.0.2/24 <-> 10.200.0.1/24` hacia `vm-carlosperales` en Azure), temporizador `systemd` de auto-apagado tras **5 minutos** de inactividad y drivers precompilados NVIDIA L4 (`580-server-open-gcp`).
  - **Modos desde Telegram (`/mode` o `/m`) y CLI ([`gcloud/gcloud-ollama.ps1`](./gcloud/gcloud-ollama.ps1)):**
    - `/mode nano` o `/m n`: Usa **Gemini** y, si la VM Spot está encendida, la elimina de inmediato conservando `dolphin-boot-disk` en reposo ($0 cómputo GCE).
    - `/mode low` o `/m l`: Usa **Dolphin CPU** creando/encendiendo `dolphin-node` como VM Spot CPU (`e2-standard-8`, 8 vCPU / 32 GB RAM, `~$152 CLP/h`, modelo `dolphin3:8b`).
    - `/mode mid` o `/m m`: Usa **Dolphin GPU** creando/encendiendo `dolphin-node` como VM Spot GPU (`g2-standard-4`, 4 vCPU / 16 GB RAM + 1x NVIDIA L4 24 GB VRAM, `~$418 CLP/h`, modelo `dolphin3:8b`).
    - `/mode high` o `/m h`: Usa **Qwen 32B GPU** creando/encendiendo `dolphin-node` como VM Spot GPU (`g2-standard-8`, 8 vCPU / 32 GB RAM + 1x NVIDIA L4 24 GB VRAM, `~$550 CLP/h`, modelo `qwen2.5-32b-abliterated`).
    - `/mode ?` o `/m ?`: Consulta el estado actual (`[Mode = x]`), el estado de la VM y del disco en GCP, si el consumo está cubierto por créditos, el gasto acumulado en CLP y el crédito disponible estimado.
    - **Notificación de transición:** Cada vez que se pasa de un modelo a otro (explícitamente o cuando el nodo se apaga tras 5 minutos de inactividad y vuelve a Gemini), Khai notifica con **`[Mode = nano]`**, **`[Mode = low]`**, **`[Mode = mid]`** o **`[Mode = high]`**.
  - **Alarmas de facturación en `01074F-B289A3-A589D8` (`CLP`):** Dos presupuestos escalonados cada **`$1.000 CLP` (`~1 USD`)** del 10 % al 100 % de `$10.000 CLP`: uno para consumo bruto de créditos (`[CREDITOS] Khai Dolphin Consumo Bruto (cada 1.000 CLP)`) y otro para cobro neto fuera de créditos (`[COBRO REAL] Khai Dolphin Gasto Neto (cada 1.000 CLP)`).

- **[`azure/`](./azure/PILOTO_AZURE.md) (Diseño alternativo y controlador legado en Azure):**
  - Conserva el análisis del piloto en Azure subscription 1 ([`azure/PILOTO_AZURE.md`](./azure/PILOTO_AZURE.md)) y los scripts originales ([`azure/azure-ollama.ps1`](./azure/azure-ollama.ps1) y [`azure/setup-dependencies.ps1`](./azure/setup-dependencies.ps1)).
