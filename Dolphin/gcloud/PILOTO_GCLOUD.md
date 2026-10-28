# Diseño y registro del piloto Dolphin en Google Cloud (Opción A: Disco único + VM Spot efímera)

Fecha de corte: 28 de octubre de 2026 (Beta V4).

## 1. Topología activa

```text
Telegram --HTTPS webhook (8443)--> Khai + web (vm-carlosperales, Azure for Students, West US)
                                      |  GENERAL_CHAT: Ollama (http://10.200.0.2:11434)
                                      |  Control VM Spot (/on low, /on high, /off): HTTPS API GCE
                                      |  Clasificación, tareas y fallback: Gemini
                                      v
                        Túnel privado WireGuard (UDP 51820)
                        10.200.0.1 (Azure) <-> 10.200.0.2 (GCP)
                                      |
                                      v
             Nodo efímero Dolphin (Google Cloud, gen-lang-client-0149640270, us-west1-a)
             Disco único persistente: dolphin-boot-disk (pd-standard, 32 GB, auto-delete=no)
             - /on o /on low  -> Spot e2-standard-8 (8 vCPU, 32 GB RAM, CPU)
             - /on high       -> Spot g2-standard-4 (4 vCPU, 16 GB RAM + 1x NVIDIA L4 24 GB VRAM)
             - /off o 5m idle -> Elimina/apaga instancia; cobro de cómputo = $0 CLP
```

## 2. Recursos aprovisionados y verificados

| Recurso | Valor / Configuración | Estado |
| --- | --- | --- |
| Proyecto GCP | `gen-lang-client-0149640270` (`Khai`) | Activo |
| Cuenta de facturación | `01074F-B289A3-A589D8` (`My Billing Account`, moneda `CLP`) | Vinculada |
| Zona | `us-west1-a` (Oregón; soporta `e2-standard-8` y `g2-standard-4`) | Activa (`22,5 ms` RTT a Azure `West US`) |
| Disco único persistente | `dolphin-boot-disk` (`pd-standard`, `32 GB`, `auto-delete=no`) | `READY` (`11 GB` usados, `20 GB` libres) |
| Contenido del disco | Ubuntu 24.04 LTS, Ollama, `dolphin3:8b`, WireGuard (`10.200.0.2/24`), `dolphin-autoshutdown.timer` (5 min) y drivers precompilados NVIDIA L4 (`580-server-open-gcp`) | Instalado y verificado |
| Cuotas verificadas | `E2_CPUS = 24`, `CPUS_ALL_REGIONS = 32`, `PREEMPTIBLE_NVIDIA_L4_GPUS = 1`, `GPUS_ALL_REGIONS = 1` | Aprobadas y activas |
| Service Account | `khai-dolphin-controller@gen-lang-client-0149640270.iam.gserviceaccount.com` | Roles `compute.instanceAdmin.v1` + `iam.serviceAccountUser` |
| CLI en host Azure | `google-cloud-cli 591.0.0` instalado en `vm-carlosperales` | Operativo |

## 3. Costos en Pesos Chilenos (CLP) y Alarmas de Facturación

Tipo de cambio de referencia: `1 USD = $950 CLP`.

| Modo | Máquina GCE (Spot) | Hardware | Costo por hora (CLP) | Sesión de 2 min (CLP) |
| --- | --- | --- | ---: | ---: |
| `/on low` | `e2-standard-8` | 8 vCPU, 32 GB RAM (CPU) | `~$152 CLP/h` | `~$5 CLP` |
| `/on high` | `g2-standard-4` | 4 vCPU, 16 GB RAM + 1x NVIDIA L4 (24 GB VRAM) | `~$390 - $446 CLP/h` | `~$14 CLP` |
| Apagado (`/off`) | Sin instancia VM | Solo disco `dolphin-boot-disk` (32 GB `pd-standard`) | `$0 CLP/h` en cómputo | Disco: `$0 - $1.216 CLP/mes` (30 GB en Free Tier) |

### Presupuestos y alarmas activas en `01074F-B289A3-A589D8`

Se crearon dos presupuestos mensuales de `$10.000 CLP` con **10 umbrales cada uno (`10 %`, `20 %`, `30 %`, `40 %`, `50 %`, `60 %`, `70 %`, `80 %`, `90 %`, `100 %`)**, lo que dispara una notificación cada **`$1.000 CLP` (`~1 USD`)**:

1. **`[CREDITOS] Khai Dolphin Consumo Bruto (cada 1.000 CLP)`** (`EXCLUDE_ALL_CREDITS`): notifica cada `$1.000 CLP` de uso bruto cubierto por tus créditos.
2. **`[COBRO REAL] Khai Dolphin Gasto Neto (cada 1.000 CLP)`** (`INCLUDE_ALL_CREDITS`): notifica cada `$1.000 CLP` únicamente si los créditos se agotan y existe facturación neta a pagar.

Además, los comandos `/on`, `/off` y `/dolphin` en Telegram informan en cada respuesta si el consumo está cubierto por créditos, el acumulado en CLP y el saldo estimado de créditos disponible.

