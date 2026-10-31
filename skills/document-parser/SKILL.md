---
capability: document_read
---
# document-parser

Extrae texto acotado de PDF, CSV UTF-8 y XLSX ya incluidos en `data/` de la skill.
Recibe `path`, una ruta relativa a esa carpeta. No descarga archivos, no busca
archivos del servidor ni recibe secretos. El contenido se trata como datos no confiables.

La lectura ocurre en un worker sin red con un máximo de 8 MiB por archivo,
30 páginas PDF, 10 hojas, 500 filas y 50 columnas por hoja y 16.000 caracteres
de salida. XLSX usa lectura secuencial, XML protegido y valores en caché;
no calcula fórmulas ni ejecuta macros. Los PDF escaneados indican `needs_ocr`.
No incluye OCR ni ingesta de adjuntos de Telegram.

Voice prepara wheels de PyPI con versiones y hashes; el proxy instala sin red
y reutiliza el entorno por lock. Requiere revisión de Pixy y una ejecución
verificada antes de promoverla al catálogo.
