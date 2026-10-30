# ADR 0005: capacidades, proveedores y decisiones transparentes

Estado: aceptado.

## DecisiÃ³n

Representar solicitudes como capacidad, proveedor opcional y objetivo. Registrar proveedores en metadatos de skills y resolverlos por proveedor explÃ­cito, predeterminado aprobado o pregunta. Separar tools de plataforma, skills reutilizables y proveedores. Registrar preferencias mediante recibos auditables y prohibir sustituciones silenciosas.

## Consecuencias

Agregar un proveedor no modifica Mind ni Coordinator. Una skill instalada no se vuelve predeterminada. Pixy conserva autoridad sobre secretos, permisos, costos, efectos y preferencias persistentes.
