"""Calcula estadísticas históricas y el pronóstico de llegada de cada embarque activo.

Genera:
  docs/data/estado.json            -> lo que lee la página web
  docs/data/estado_embarques.csv   -> para conectar desde Excel (Power Query)
"""
import csv
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from statistics import median

from comun import (DATOS, DIAS_A_PLANTA, HISTORIAL, PUERTOS, ahora, distancia_mn, en_caja,
                   escribe_json, lee_json)

VEL_DEFECTO = 15.0      # nudos, velocidad comercial típica de portacontenedores
FACTOR_RUTA = 1.12      # la ruta real es más larga que la línea recta
MIN_MUESTRA = 8
DESTINOS_MX = ("MX", "MANZ", "ZLO", "MZO", "LZC", "LAZARO", "MEXICO")


def percentil(xs, p):
    xs = sorted(xs)
    if not xs:
        return None
    k = (len(xs) - 1) * p
    i = int(k)
    return xs[i] if i + 1 >= len(xs) else xs[i] + (xs[i + 1] - xs[i]) * (k - i)


def resumen(xs):
    return {"n": len(xs), "mediana": round(median(xs), 1), "p10": round(percentil(xs, .1), 1),
            "p90": round(percentil(xs, .9), 1)}


def carga_embarques():
    filas = []
    with open(DATOS / "embarques.csv", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            r["etd"], r["eta"] = date.fromisoformat(r["etd"]), date.fromisoformat(r["eta"])
            r["contenedores"] = int(r["contenedores"] or 0)
            r["transito"] = (r["eta"] - r["etd"]).days
            filas.append(r)
    return filas


def estadisticas(filas, hoy):
    hechos = [r for r in filas if r["eta"] < hoy]
    por_pol, por_ruta, por_nav, por_mes = (defaultdict(list) for _ in range(4))
    for r in hechos:
        t = r["transito"]
        por_pol[(r["pol"], r["pod"])].append(t)
        por_ruta[(r["pol"], r["pod"], r["naviera"])].append(t)
        por_nav[r["naviera"]].append(t)
        por_mes[(r["pol"], r["etd"].strftime("%Y-%m"))].append(t)
    est = {
        "origen": {f"{a}>{b}": resumen(v) for (a, b), v in por_pol.items() if len(v) >= 3},
        "ruta_naviera": {f"{a}>{b}>{c}": resumen(v) for (a, b, c), v in por_ruta.items() if len(v) >= 3},
        "naviera": {k: resumen(v) for k, v in por_nav.items() if len(v) >= 5 and k},
        "mensual": {f"{a}|{m}": resumen(v) for (a, m), v in por_mes.items() if len(v) >= 3},
        "total_historico": len(hechos),
    }
    return est


def transito_esperado(est, pol, pod, naviera):
    for clave, tabla in ((f"{pol}>{pod}>{naviera}", "ruta_naviera"), (f"{pol}>{pod}", "origen")):
        s = est[tabla].get(clave)
        if s and s["n"] >= MIN_MUESTRA:
            return s, ("ruta y naviera" if tabla == "ruta_naviera" else "origen")
    todos = [s for k, s in est["origen"].items() if k.endswith(f">{pod}")]
    if todos:
        med = median(s["mediana"] for s in todos)
        return {"n": 0, "mediana": med, "p10": med * .8, "p90": med * 1.35}, "promedio general"
    return {"n": 0, "mediana": 28, "p10": 22, "p90": 38}, "valor por defecto"


def a_fecha(iso):
    try:
        return datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None


def llegadas_detectadas():
    """Primera vez que cada buque se vio fondeado / atracado en cada puerto."""
    llegadas = {}
    ruta = HISTORIAL / "eventos_puerto.csv"
    if not ruta.exists():
        return llegadas
    with open(ruta, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            k = (r["buque"], r["puerto"])
            d = llegadas.setdefault(k, {})
            if r["estado"] in ("fondeado", "atracado") and r["estado"] not in d:
                d[r["estado"]] = r["fecha"]
            d.setdefault("primera", r["fecha"])
    return llegadas


def resumen_congestion():
    ruta = HISTORIAL / "congestion.csv"
    if not ruta.exists():
        return []
    por_puerto = defaultdict(list)
    with open(ruta, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            por_puerto[r["puerto"]].append(r)
    limite = ahora() - timedelta(days=7)
    salida = []
    for p, filas in por_puerto.items():
        ult = filas[-1]
        semana = [int(r["fondeados"]) for r in filas if (a_fecha(r["fecha"]) or ahora()) >= limite]
        salida.append({"puerto": p, "fondeados": int(ult["fondeados"]), "atracados": int(ult["atracados"]),
                       "promedio_7d": round(sum(semana) / len(semana), 1) if semana else None,
                       "fecha": ult["fecha"], "lecturas": len(filas),
                       "lat": PUERTOS[p]["lat"], "lon": PUERTOS[p]["lon"]})
    return sorted(salida, key=lambda r: -r["fondeados"])


def pronostica(g, est, pos, eta_ais, llegada, hoy, momento):
    s, base = transito_esperado(est, g["pol"], g["pod"], g["naviera"])
    hist = g["etd"] + timedelta(days=round(s["mediana"]))
    peor = g["etd"] + timedelta(days=round(s["p90"]))
    pod = PUERTOS.get(g["pod"])
    pol = PUERTOS.get(g["pol"])
    # Sin señal AIS se respeta la ETA del reporte; el histórico solo marca el riesgo.
    r = {"pronostico": g["eta"], "metodo": "ETA del reporte (sin señal AIS)", "confianza": "sin AIS",
         "transito_normal": s["mediana"], "peor_caso": peor.isoformat(), "muestra": s["n"],
         "esperado_historico": hist, "base_historico": base}

    hora_pos = a_fecha(pos["hora"]) if pos else None
    fresca = hora_pos and (momento - hora_pos) < timedelta(hours=48)

    if llegada and (llegada.get("fondeado") or llegada.get("atracado") or llegada.get("primera")):
        f = a_fecha(llegada.get("fondeado") or llegada.get("atracado") or llegada["primera"]).date()
        if f >= g["etd"]:
            r.update(pronostico=f, metodo="Llegó a puerto (AIS)", confianza="real",
                     estado_puerto="atracado" if llegada.get("atracado") else "fondeado")
            return r

    if fresca and pod:
        lat, lon = pos["lat"], pos["lon"]
        if en_caja(lat, lon, pod["caja"]):
            r.update(pronostico=hora_pos.date(), metodo="En zona del puerto destino (AIS)", confianza="alta")
            return r
        if pol and en_caja(lat, lon, pol["caja"]) and hoy > g["etd"]:
            r.update(pronostico=hoy + timedelta(days=round(s["mediana"])),
                     metodo="Sigue en puerto de origen (AIS)", confianza="alta")
            return r
        dist = distancia_mn(lat, lon, pod["lat"], pod["lon"]) * FACTOR_RUTA
        sog = pos.get("sog") or 0
        vel = (sog + VEL_DEFECTO) / 2 if 8 <= sog <= 25 else VEL_DEFECTO
        llegada_pos = (hora_pos + timedelta(hours=dist / vel)).date()
        minimo = g["etd"] + timedelta(days=round(s["p10"]))
        r.update(pronostico=max(llegada_pos, minimo), metodo="Posición y velocidad (AIS)",
                 confianza="alta" if lon < 0 or lon > 150 else "media", distancia_mn=round(dist))
        return r

    if eta_ais and eta_ais.get("eta_ais") and any(x in (eta_ais.get("destino") or "").upper() for x in DESTINOS_MX):
        e = date.fromisoformat(eta_ais["eta_ais"])
        if hoy - timedelta(days=2) <= e <= hoy + timedelta(days=60):
            r.update(pronostico=e, metodo="ETA declarada por el buque (AIS)", confianza="media")
            return r
    return r


def main():
    momento = ahora()
    hoy = momento.astimezone(timezone(timedelta(hours=-6))).date()   # hora de México
    filas = carga_embarques()
    est = estadisticas(filas, hoy)
    posiciones = lee_json(DATOS / "posiciones.json", {})
    etas = lee_json(DATOS / "eta_ais.json", {})
    mapa = lee_json(DATOS / "buques_ais.json", {})
    llegadas = llegadas_detectadas()

    # Un grupo = un buque rumbo a un destino en un mismo viaje (ETAs a menos de 21 días entre sí).
    vigentes = sorted((r for r in filas if r["etd"] <= hoy + timedelta(days=14) and r["eta"] >= hoy - timedelta(days=3)),
                      key=lambda r: (r["buque"], r["pod"], r["eta"]))
    grupos = []
    for r in vigentes:
        g = grupos[-1] if grupos else None
        if not g or g["buque"] != r["buque"] or g["pod"] != r["pod"] or (r["eta"] - g["eta"]).days > 21:
            g = {"buque": r["buque"], "pod": r["pod"], "eta": r["eta"], "filas": []}
            grupos.append(g)
        g["filas"].append(r)
    for g in grupos:
        fs = g.pop("filas")
        moda = lambda xs: Counter(xs).most_common(1)[0][0]
        g["pol"] = moda(r["pol"] for r in fs)
        g["origenes"] = sorted({r["pol"] for r in fs})
        g["naviera"] = moda(r["naviera"] for r in fs)
        g["etd"] = moda(r["etd"] for r in fs if r["pol"] == g["pol"])
        g["eta"] = min(r["eta"] for r in fs)
        g["contenedores"] = sum(r["contenedores"] for r in fs)
        g["embarques"] = len(fs)
        g["imps"] = sorted({r.get("imp") for r in fs if r.get("imp")})
        g["buques_iniciales"] = sorted({r.get("buque_inicial") for r in fs if r.get("buque_inicial") and r.get("buque_inicial") != r["buque"]})
        g["_filas"] = fs

    activos, por_embarque = [], []
    for g in grupos:
        fs = g.pop("_filas")
        pos = posiciones.get(g["buque"])
        p = pronostica(g, est, pos, etas.get(g["buque"]), llegadas.get((g["buque"], g["pod"])), hoy, momento)
        retraso = (p["pronostico"] - g["eta"]).days
        estado = "por zarpar" if g["etd"] > hoy else ("en puerto" if p["confianza"] == "real" else "en tránsito")
        nivel = "verde" if retraso <= 1 else ("amarillo" if retraso <= 4 else "rojo")
        activos.append({
            **{k: (v.isoformat() if isinstance(v, date) else v) for k, v in g.items()},
            "estado": estado, "pronostico": p["pronostico"].isoformat(), "retraso_dias": retraso,
            "nivel": nivel, "fecha_planta": (p["pronostico"] + timedelta(days=DIAS_A_PLANTA)).isoformat(),
            "metodo": p["metodo"], "confianza": p["confianza"], "transito_normal": p["transito_normal"],
            "peor_caso": max(date.fromisoformat(p["peor_caso"]), g["eta"]).isoformat(),
            "fecha_planta_peor": (max(date.fromisoformat(p["peor_caso"]), g["eta"]) + timedelta(days=DIAS_A_PLANTA)).isoformat(),
            "eta_historica": max(p["esperado_historico"], g["eta"]).isoformat(),
            "fecha_planta_historica": (max(p["esperado_historico"], g["eta"]) + timedelta(days=DIAS_A_PLANTA)).isoformat(),
            "estado_puerto": p.get("estado_puerto"),
            "esperado_historico": p["esperado_historico"].isoformat(), "base_historico": p["base_historico"],
            "riesgo_dias": max(0, (p["esperado_historico"] - g["eta"]).days),
            "muestra_historico": p["muestra"],
            "mmsi": (mapa.get(g["buque"]) or {}).get("mmsi"),
            "posicion": pos if pos else None,
            "eta_ais": etas.get(g["buque"]),
        })
        a = activos[-1]
        dias_norm = round(p["transito_normal"])
        dias_p90 = (date.fromisoformat(p["peor_caso"]) - g["etd"]).days
        for r in fs:   # una fila por embarque, con su propio ETD/ETA, para cruzar con el Excel interno
            esp = r["etd"] + timedelta(days=dias_norm)
            peor_r = max(r["etd"] + timedelta(days=dias_p90), r["eta"])
            pron = r["eta"] if p["confianza"] == "sin AIS" else p["pronostico"]
            por_embarque.append({**a, "imp": r.get("imp", ""), "clave": r["clave"], "pol": r["pol"],
                                 "naviera": r["naviera"], "etd": r["etd"].isoformat(), "eta": r["eta"].isoformat(),
                                 "contenedores": r["contenedores"], "embarques": 1,
                                 "pronostico": pron.isoformat(), "retraso_dias": (pron - r["eta"]).days,
                                 "fecha_planta": (pron + timedelta(days=DIAS_A_PLANTA)).isoformat(),
                                 "riesgo_dias": max(0, (esp - r["eta"]).days),
                                 "eta_historica": max(esp, r["eta"]).isoformat(),
                                 "fecha_planta_historica": (max(esp, r["eta"]) + timedelta(days=DIAS_A_PLANTA)).isoformat(),
                                 "peor_caso": peor_r.isoformat(),
                                 "fecha_planta_peor": (peor_r + timedelta(days=DIAS_A_PLANTA)).isoformat()})
    activos.sort(key=lambda a: (-a["retraso_dias"], -a["riesgo_dias"], a["eta"]))

    en_mapa = [a for a in activos if a["posicion"]]
    transito = [a for a in activos if a["estado"] != "por zarpar"]
    retrasados = [a for a in transito if a["retraso_dias"] >= 2]
    kpis = {
        "embarques_activos": sum(a["embarques"] for a in activos),
        "buques_activos": len({a["buque"] for a in activos}),
        "buques_en_mapa": len({a["buque"] for a in en_mapa}),
        "contenedores_transito": sum(a["contenedores"] for a in transito),
        "buques_con_retraso": len({a["buque"] for a in retrasados}),
        "retraso_mediano": median([a["retraso_dias"] for a in retrasados]) if retrasados else 0,
        "buques_con_riesgo": len({a["buque"] for a in activos if a["riesgo_dias"] >= 5}),
        "buques_identificados": sum(1 for b in {a["buque"] for a in activos} if (mapa.get(b) or {}).get("mmsi")),
    }
    escribe_json(DATOS / "estado.json", {
        "generado": momento.isoformat(timespec="seconds"), "hoy": hoy.isoformat(), "kpis": kpis,
        "activos": activos, "congestion": resumen_congestion(), "estadisticas": est,
        "puertos": {p: {"lat": d["lat"], "lon": d["lon"]} for p, d in PUERTOS.items()},
        "ultima_corrida": lee_json(DATOS / "ultima_corrida.json", None),
    })

    campos = ["imp", "clave", "buque", "naviera", "pol", "pod", "etd", "eta", "fecha_planta",
              "riesgo_dias", "eta_historica", "fecha_planta_historica", "peor_caso", "fecha_planta_peor",
              "transito_normal", "pronostico", "retraso_dias", "metodo", "estado", "contenedores"]
    with open(DATOS / "estado_embarques.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=campos, extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(por_embarque, key=lambda r: (r["eta"], r["buque"])))
    print(f"{len(activos)} grupos activos · {kpis}")


if __name__ == "__main__":
    main()
