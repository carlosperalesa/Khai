# ADR-0001: Separar aprendizaje, reparación y evolución

- Estado: aceptado
- Fecha: 2026-10-26

## Decisión

Khai trata aprendizaje, reparación y evolución como finalidades distintas, con criterios de aceptación y autoridades independientes.

- Aprender significa resolver posteriormente una situación equivalente con menos trabajo, sin perder calidad.
- Reparar significa satisfacer la postcondición original mediante un intento identificado y verificado.
- Evolucionar significa mejorar un mecanismo contra una línea base y casos reservados, con promoción y reversión auditadas.

Una reparación puede producir evidencia para aprender, pero no demuestra aprendizaje hasta su reutilización. Una mejora de un caso no autoriza modificar el mecanismo general.

## Consecuencias

- Los estados de objetivo, caso de recuperación y experimento no son intercambiables.
- `COMPLETED`, `RECOVERED` y entrega confirmada representan hechos diferentes.
- Dreams propone; no activa reglas ni despliega cambios por sí mismo.

