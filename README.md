# Monitoreo de buques

Seguimiento de los buques de importación Asia → México con datos AIS gratuitos de
[aisstream.io](https://aisstream.io), y pronóstico de retrasos con base en el histórico de embarques.

**Página:** https://jart0988aj-maker.github.io/monitoreo-buques/

## Cómo funciona

1. Cada 30 minutos GitHub Actions corre `scripts/recolectar.py`: escucha 4 minutos el flujo AIS y guarda
   posición de nuestros buques, la ETA que declara cada buque y la congestión (barcos fondeados) en los puertos.
2. `scripts/pronosticar.py` calcula el pronóstico de llegada de cada buque y lo compara con la ETA del reporte:
   - **Retraso (AIS):** con posición real → distancia restante ÷ velocidad; o la ETA que transmite el propio buque.
   - **Riesgo histórico:** cuando la ETA del reporte es más corta de lo que esa ruta y naviera suelen tardar.
   - **Fecha a planta:** llegada + 7 días.
3. La página (`docs/index.html`) lee `docs/data/estado.json`.

## Privacidad

El repositorio es público. Solo se publican buque, naviera, puertos, fechas y número de contenedores.
Proveedor, booking, factura, IMP y planta **nunca** se suben. Los archivos Excel están en `.gitignore`.

## Actualizar los embarques

```
python scripts/preparar_embarques.py "Reporte_KPIs.xlsx"
```
Genera `docs/data/embarques.csv`; al subirlo, la página se recalcula sola.

## Conectar con el Excel interno

`docs/data/estado_embarques.csv` trae una fila por embarque con la columna `clave` = `BUQUE|POL|ETD`.
En Excel: Datos → Desde la web → pega la URL del CSV, y cruza con BUSCARX.

## Archivos de datos

| Archivo | Contenido |
|---|---|
| `docs/data/embarques.csv` | Embarques (datos mínimos) |
| `docs/data/estado.json` | Pronósticos, congestión y estadísticas para la página |
| `docs/data/estado_embarques.csv` | Pronóstico por embarque, para Excel |
| `docs/data/buques_ais.json` | Buque → MMSI/IMO identificado en AIS |
| `docs/data/historial/` | Posiciones, congestión y eventos en puerto (se acumulan) |
