"""Convierte el reporte de embarques (Excel) a un CSV con SOLO datos mínimos.

Uso:  python scripts/preparar_embarques.py "Reporte_KPIs.xlsx"

Columnas que se publican: clave, buque, naviera, pol, pod, etd, eta, contenedores, tipo_ctr.
NO se publican proveedor, booking, factura, IMP, planta, forwarder ni observaciones.
El Excel nunca se sube al repositorio (.gitignore).
"""
import sys

import pandas as pd

from comun import DATOS, normaliza_buque, normaliza_puerto

COLUMNAS = {
    "BUQUE INCIAL": "buque", "BUQUE INICIAL": "buque", "BUQUE": "buque",
    "NAVIERA": "naviera", "POL": "pol", "POD": "pod", "ETD": "etd", "ETA": "eta",
    "CTN QTY": "contenedores", "CTR TYPE": "tipo_ctr",
}


def preparar(ruta_excel):
    df = pd.read_excel(ruta_excel)
    df.columns = [str(c).strip().upper() for c in df.columns]
    df = df.rename(columns={c: n for c, n in COLUMNAS.items() if c in df.columns})
    faltan = {"buque", "pol", "pod", "etd", "eta"} - set(df.columns)
    if faltan:
        sys.exit(f"Faltan columnas en el Excel: {faltan}")
    for c in ("naviera", "contenedores", "tipo_ctr"):
        if c not in df.columns:
            df[c] = ""

    out = pd.DataFrame({
        "buque": df["buque"].map(normaliza_buque),
        "naviera": df["naviera"].fillna("").astype(str).str.upper().str.strip().replace({"NAN": ""}),
        "pol": df["pol"].map(normaliza_puerto),
        "pod": df["pod"].map(normaliza_puerto),
        "etd": pd.to_datetime(df["etd"], errors="coerce").dt.date,
        "eta": pd.to_datetime(df["eta"], errors="coerce").dt.date,
        "contenedores": pd.to_numeric(df["contenedores"], errors="coerce").fillna(0).astype(int),
        "tipo_ctr": df["tipo_ctr"].fillna("").astype(str).str.replace("\n", " ").str.strip(),
    })
    out["naviera"] = out["naviera"].replace({"HAPAG-LLOYD": "HAPAG LLOYD", "PENDIENTE": ""})
    # 'EVER FOND V' y 'EVER FOND' son el mismo buque (la V era de "viaje")
    nombres = set(out.buque)
    out["buque"] = out.buque.map(lambda n: n[:-2] if n.endswith(" V") and n[:-2] in nombres else n)
    total = len(out)
    out = out[(out.buque != "") & out.etd.notna() & out.eta.notna()]
    # Fechas imposibles (p. ej. ETD con año equivocado): se descartan
    dias = (pd.to_datetime(out.eta) - pd.to_datetime(out.etd)).dt.days
    out = out[(dias >= 3) & (dias <= 90)]
    out["clave"] = out.buque + "|" + out.pol + "|" + out.etd.astype(str)
    out = out[["clave", "buque", "naviera", "pol", "pod", "etd", "eta", "contenedores", "tipo_ctr"]]
    out = out.sort_values(["etd", "buque"])
    DATOS.mkdir(parents=True, exist_ok=True)
    out.to_csv(DATOS / "embarques.csv", index=False)
    print(f"{len(out)} embarques publicados de {total} filas ({total - len(out)} sin buque/fechas válidas).")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    preparar(sys.argv[1])
