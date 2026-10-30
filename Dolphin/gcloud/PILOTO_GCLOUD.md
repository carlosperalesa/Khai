# Diseño y registro del piloto Dolphin en Google Cloud (Opción A: Disco único + VM Spot efímera)

Fecha de corte: 30 de octubre de 2026 (Beta V4).

## 1. Topología activa

```text
Telegram --HTTPS webhook (8443)--> Khai + web (vm-carlosperales, Azure for Students, West US)
                                      |  GENERAL_CHAT (low/mid/high): Ollama (http://10.200.0.2:11434)
                                      |  Control VM Spot (/m nano, /m low, /m mid, /m high, /m ?): HTTPS API GCE
                                      |  Clasificación, tareas, /m nano y fallback: Gemini
                                      v
                        Túnel privado WireGuard (UDP 51820)
                        10.200.0.1 (Azure) <-> 10.200.0.2 (GCP)
                                      |
                                      v
             Nodo efímero Dolphin/Qwen (Google Cloud, gen-lang-client-0149640270, us-west1-a)
             Disco único persistente: dolphin-boot-disk (pd-standard, 100 GB, auto-delete=no)
             - /m nano (o 5m idle)-> Gemini; elimina instancia Spot; cobro de cómputo = $0 CLP
             - /m low             -> Dolphin CPU Spot e2-standard-8 (8 vCPU, 32 GB RAM, dolphin3:8b)
             - /m mid             -> Dolphin GPU Spot g2-standard-4 (4 vCPU, 16 GB RAM + 1x L4 24 GB, dolphin3:8b)
             - /m high            -> Qwen 32B GPU Spot g2-standard-8 (8 vCPU, 32 GB RAM + 1x L4 24 GB, qwen2.5-32b-abliterated)
             - /m ?               -> Consulta estado actual ([Mode = x]) y saldo de créditos CLP
```

## 2. Recursos aprovisionados y verificados

| Recurso | Valor / Configuración | Estado |
| --- | --- | --- |
| Proyecto GCP | `gen-lang-client-0149640270` (`Khai`) | Activo |
| Cuenta de facturación | `01074F-B289A3-A589D8` (`My Billing Account`, moneda `CLP`) | Vinculada |
| Zona | `us-west1-a` (Oregón; soporta `e2-standard-8`, `g2-standard-4` y `g2-standard-8`) | Activa (`22,5 ms` RTT a Azure `West US`) |
| Disco único persistente | `dolphin-boot-disk` (`pd-standard`, `100 GB`, `auto-delete=no`) | `READY` (~25 GB ocupados con ambos modelos, ~75 GB libres) |
| Contenido del disco | Ubuntu 24.04 LTS, Ollama, `dolphin3:8b`, `qwen2.5-32b-abliterated`, WireGuard (`10.200.0.2/24`), `dolphin-autoshutdown.timer` (5 min) y drivers precompilados NVIDIA L4 (`580-server-open-gcp`) | Instalado y verificado |
| Cuotas verificadas | `E2_CPUS = 24`, `CPUS_ALL_REGIONS = 32`, `PREEMPTIBLE_NVIDIA_L4_GPUS = 1`, `GPUS_ALL_REGIONS = 1` | Aprobadas y activas |
| Service Account | `khai-dolphin-controller@gen-lang-client-0149640270.iam.gserviceaccount.com` | Roles `compute.instanceAdmin.v1` + `iam.serviceAccountUser` |
| CLI en host Azure | `google-cloud-cli 591.0.0` instalado en `vm-carlosperales` | Operativo |

## 3. Costos en Pesos Chilenos (CLP) y Alarmas de Facturación

Tipo de cambio de referencia: `1 USD = $950 CLP`.

| Modo | Máquina GCE (Spot) | Hardware | Modelo Ollama | Costo por hora (CLP) | Sesión de 2 min (CLP) |
| --- | --- | --- | --- | ---: | ---: |
| `/m nano` (`Gemini`) | Sin instancia VM | Solo disco `dolphin-boot-disk` (100 GB `pd-standard`) | Gemini API (Google Cloud) | `$0 CLP/h` en cómputo | Disco: `$0 - $3.800 CLP/mes` (30 GB en Free Tier) |
| `/m low` (`Dolphin CPU`) | `e2-standard-8` | 8 vCPU, 32 GB RAM (CPU) | `dolphin3:8b` | `~$152 CLP/h` | `~$5 CLP` |
| `/m mid` (`Dolphin GPU`) | `g2-standard-4` | 4 vCPU, 16 GB RAM + 1x NVIDIA L4 (24 GB VRAM) | `dolphin3:8b` | `~$418 CLP/h` | `~$14 CLP` |
| `/m high` (`Qwen 32B GPU`) | `g2-standard-8` | 8 vCPU, 32 GB RAM + 1x NVIDIA L4 (24 GB VRAM) | `qwen2.5-32b-abliterated` | `~$550 CLP/h` | `~$18 CLP` |

### Presupuestos y alarmas activas en `01074F-B289A3-A589D8`

Se crearon dos presupuestos mensuales de `$10.000 CLP` con **10 umbrales cada uno (`10 %`, `20 %`, `30 %`, `40 %`, `50 %`, `60 %`, `70 %`, `80 %`, `90 %`, `100 %`)**, lo que dispara una notificación cada **`$1.000 CLP` (`~1 USD`)**:

1. **`[CREDITOS] Khai Dolphin Consumo Bruto (cada 1.000 CLP)`** (`EXCLUDE_ALL_CREDITS`): notifica cada `$1.000 CLP` de uso bruto cubierto por tus créditos.
2. **`[COBRO REAL] Khai Dolphin Gasto Neto (cada 1.000 CLP)`** (`INCLUDE_ALL_CREDITS`): notifica cada `$1.000 CLP` únicamente si los créditos se agotan y existe facturación neta a pagar.

Además, los comandos `/mode` (`/m`) con `nano`, `low`, `mid`, `high` y `?` en Telegram informan en cada respuesta `[Mode = x]`, si el consumo está cubierto por créditos, el acumulado en CLP y el saldo estimado de créditos disponible. Cuando el nodo se apaga por inactividad (5 min) y la conversación pasa automáticamente a Gemini, el siguiente mensaje incluye el aviso `[Mode = nano]`.
