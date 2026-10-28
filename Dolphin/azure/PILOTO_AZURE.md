# Diseño y registro del piloto Dolphin en Azure

Fecha de corte: 28 de octubre de 2026. Este documento separa lo observado y
decidido de lo que todavía no se ha desplegado.

**Punto exacto de reanudación:** la última acción fue documentar este estado.
Carlos detuvo la sesión antes de resolver la cuota. Al volver, la primera
decisión es solicitar 8 vCPU Basv2 en West US o evaluar otra familia/región.
Después decidir la salida a Internet y recién entonces adaptar el controlador
y crear recursos. Las verificaciones previas pueden haber caducado: consultar
de nuevo Git, sesión Azure, cuotas, SKU y precios antes de cualquier mutación.
No hay VM Dolphin, peering, configuración remota, commit ni push de este piloto.

## Objetivo y topología

```text
Telegram --HTTPS webhook--> Khai + web (Azure for Students, West US)
                              |  GENERAL_CHAT: Ollama
                              |  clasificación, tareas y fallback: Gemini
                              v
                     peering privado entre VNets
                              |
                              v
               Dolphin/Ollama (Azure subscription 1, West US)
               TCP 11434 privado, sin Docker; /off desasigna VM
```

Khai recibe Telegram por webhook HTTPS público en el puerto 8443 y responde
por HTTPS saliente. Dolphin no necesita HTTPS público para ese flujo; su Ollama
debe ser accesible únicamente desde Khai por red privada. Ambas suscripciones
pertenecen al mismo tenant. El HTTP interno hacia Ollama no debe transportar
Secrets. Khai conserva el punto de entrada público mientras use webhook.

## Recursos observados y decisión de ubicación

| Recurso | Azure for Students | Azure subscription 1 |
| --- | --- | --- |
| ID | `05d7c0d1-fc1f-4ec0-b5b7-93be3c91cfa4` | `ad27ec57-ac98-4f30-89fa-a795fe5d2501` |
| VM | `vm-carlosperales`, RG `RG-CARLOSPERALES`, West US | Dolphin aún no creada |
| Tamaño y disco | B2pts_v2 ARM64, SO Standard SSD 64 GiB | Elegidos B8as_v2 y SO Standard HDD 32 GiB |
| Red | VNet `vm-carlosperalesVNET` (`10.0.0.0/16`), subred `vm-carlosperalesSubnet` (`10.0.0.0/24`); IP privada `10.0.0.4`, pública `20.237.211.250` | VNet nueva pendiente |
| Cuota West US | No aplica a Dolphin | Regional 10 vCPU; Basv2 0 vCPU |

En la VM de Students, `df -h /` mostró 61 GiB, 11 GiB usados y 51 GiB libres;
`/opt/khai` ocupa unos 8,1 MiB. También aloja la web del propietario. Por eso
Khai permanece allí: seguirá disponible cuando Dolphin esté desasignado. El
crédito Students no paga los recursos creados en subscription 1.

`Microsoft.Compute`, `Microsoft.Network` y `Microsoft.Storage` se registraron
en subscription 1. No se crearon allí RG, VM, disco, IP ni VNet.

## Costos y capacidad

Precios públicos consultados en Azure Retail Prices API para Linux, West US,
bajo demanda; pueden diferir de la factura y no incluyen red ni impuestos:

| Componente | Precio consultado | Condición de cobro |
| --- | ---: | --- |
| B8as_v2, 8 vCPU/32 GiB | US$0,36/h | VM asignada; `deallocate` detiene cómputo |
| Standard HDD S4 LRS, 32 GiB | US$1,54/mes | Disco existente, aun con VM apagada |
| Standard HDD S6 LRS, 64 GiB | US$3,01/mes | Si se amplía el disco |
| Standard SSD E4, 32 GiB | US$2,40/mes | Referencia de rendimiento |
| Standard SSD E6, 64 GiB | US$4,80/mes | Referencia de rendimiento |

El HDD de 64 GiB ahorra US$1,79/mes respecto del SSD de 64 GiB de esta
consulta. `dolphin3:8b` ocupa aproximadamente 4,9 GiB como descarga, pero hay
que sumar Ubuntu, Ollama, archivos temporales y actualizaciones. Verificar
espacio libre real después de instalar. La B8as_v2 usa créditos CPU: medir
latencia de inferencia sostenida antes de asumir rendimiento estable.

También se cobran por separado, según configuración y uso, IP pública, peering
y tráfico. El costo fijo del disco continúa mientras la VM esté desasignada.

## Bloqueos, alternativas y riesgos

1. **Cuota:** la cuota total de 10 vCPU no sustituye la cuota Basv2 de 0.
   Solicitar 8 vCPU Basv2 en West US conserva la VM elegida. D8as_v4 ofrece
   8 vCPU/32 GiB y su familia muestra cuota 10 allí, pero falta comprobar
   precio y capacidad efectiva. Otra región requiere revisar cuota, latencia
   y red. No crear B8as_v2 antes de resolverlo.
2. **Salida a Internet:** las VNets nuevas son privadas por defecto. Para
   instalar Ollama, descargar el modelo y actualizarlo se necesita salida
   explícita: IP pública asociada a la VM con entrada denegada por NSG, NAT
   Gateway con cargo fijo, o IP pública temporal que habrá que volver a poner
   para actualizar. Ninguna opción requiere exponer TCP 11434 públicamente.
3. **Peering:** elegir una VNet que no se solape con `10.0.0.0/16` y conectar
   ambas suscripciones. Permitir TCP 11434 solo desde Khai, verificar NSG,
   rutas y firewall local. Separar acceso administrativo del acceso al modelo.
4. **Control Telegram:** faltan diseño e implementación de `/on` y `/off`,
   autorización de usuarios, identidad Azure de alcance mínimo, reportes de
   estado/falla y protección ante apagado fallido. Se debe comprobar que
   `/off` desasigne la VM, no solo la detenga dentro de Ubuntu.
5. **LLM y Secrets:** distinguir conversación de clasificación, tareas y
   fallback. Actualizar las credenciales/APIs de Gemini en Secrets con valores
   proporcionados por el propietario; nunca almacenarlas en este documento.

## Código local y diferencias con el diseño

- `app/mind/mind.py` deriva `GENERAL_CHAT` al gateway con propósito
  `conversation` después de clasificar, con contexto conversacional acotado.
- `app/voice/gateway/llm_gateway.py` usa `KHAI_DOLPHIN_URL`, exige IP privada
  explícita, HTTP y puerto 11434, no sigue redirects y llama a `/api/generate`.
  Modelo predeterminado `dolphin3:8b`; timeout predeterminado 8 s, máximo 30 s.
  Si falla, vuelve al proveedor existente, normalmente Gemini.
- `tests/test_dolphin_pilot.py` pasó 2/2 localmente; no prueba conectividad
  entre Azure ni experiencia de usuario en Telegram.
- `scripts/azure-ollama.ps1` y `Makefile` son heredados para VS Code: crean por
  defecto Canada Central, B8als_v2, disco de 64 GiB e IP pública; `on/off`
  controlan un túnel SSH del PC. **No ejecutar `create` para este piloto** sin
  adaptarlos.
- Azure CLI 2.91.0 está en `Dolphin/.venv`; funciona `az.bat`. El mensaje de
  `setup-dependencies.ps1` que indica `az.cmd login` está equivocado.

## Orden de implantación tras decidir cuota y salida

1. Adaptar y revisar el controlador para subscription 1, West US, HDD 32 GiB
   y red privada. Confirmar SKU, imagen y cuota antes de crear recursos.
2. Crear VM, disco, VNet, NSG y peering interactivamente. Instalar Ollama sin
   Docker, descargar el modelo y medir espacio, latencia y costo real.
3. Configurar endpoint privado en Khai y probar conexión solo desde Students.
   Integrar `/on` y `/off` con autorización e identidad acotada.
4. Actualizar Gemini Secrets, probar conversación y fallback desde Telegram,
   cerrar validaciones, preparar commit/push y desplegar Khai sin afectar la
   web existente. Registrar resultados y ajustar capacidad solo con evidencia.

## Referencias

- [Cuotas por familia](https://learn.microsoft.com/en-us/azure/quotas/per-vm-quota-requests)
- [Basv2 y créditos CPU](https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/general-purpose/basv2-series)
- [D8as_v4](https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/general-purpose/dasv4-series)
- [Peering entre suscripciones](https://learn.microsoft.com/en-us/azure/virtual-network/create-peering-different-subscriptions)
- [Salida predeterminada](https://learn.microsoft.com/en-us/azure/virtual-network/ip-services/default-outbound-access)
- [Estados y facturación](https://learn.microsoft.com/en-us/azure/virtual-machines/states-billing)
- [Discos administrados](https://learn.microsoft.com/en-us/azure/virtual-machines/disks-types)
- [Precios minoristas Azure](https://learn.microsoft.com/en-us/rest/api/cost-management/retail-prices/azure-retail-prices)
- [Modelo Dolphin3 en Ollama](https://ollama.com/library/dolphin3)
