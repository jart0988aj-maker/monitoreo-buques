"""Recolector AIS (aisstream.io).

Abre hasta 3 conexiones durante DURACION segundos:
  A) Todo el mundo, solo datos estáticos (nombre, IMO, destino, ETA que reporta el barco)
     -> sirve para descubrir el MMSI de nuestros buques y su ETA declarada.
  B) Todo el mundo, filtrado a los MMSI de nuestros buques -> posiciones.
  C) Cajas de los puertos -> congestión (barcos fondeados / atracados) y posiciones
     de nuestros buques cuando están cerca de puerto.

Requiere la variable de entorno AISSTREAM_API_KEY.
"""
import asyncio
import csv
import difflib
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone

import websockets

from comun import (DATOS, HISTORIAL, PUERTOS, ahora, clave_ais, distancia_mn, en_caja,
                   escribe_json, lee_json)

URL = "wss://stream.aisstream.io/v0/stream"
DURACION = int(os.environ.get("DURACION_SEGUNDOS", "240"))
MAX_MMSI = 50  # límite de aisstream por suscripción


# ---------------------------------------------------------------- objetivos
def buques_objetivo():
    """Buques en tránsito, por zarpar (7 días) o que llegaron hace poco (7 días)."""
    hoy = date.today()
    activos = {}
    with open(DATOS / "embarques.csv", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            etd, eta = date.fromisoformat(r["etd"]), date.fromisoformat(r["eta"])
            if etd <= hoy + timedelta(days=7) and eta >= hoy - timedelta(days=7):
                b = activos.setdefault(r["buque"], {"eta": eta})
                b["eta"] = min(b["eta"], eta)
    return activos


def variantes(nombre):
    """Claves con las que puede aparecer el buque en AIS."""
    c = clave_ais(nombre)
    vs = {c}
    if nombre.endswith(" V"):          # 'EVER FEAT V' (V de viaje) vs 'MSC LYSE V' (nombre real)
        vs.add(clave_ais(nombre[:-2]))
    return vs


# ---------------------------------------------------------------- estado de la corrida
class Corrida:
    def __init__(self, objetivos, mapa):
        self.objetivos = objetivos                 # nombre_excel -> info
        self.mapa = mapa                           # nombre_excel -> {mmsi, imo, nombre_ais}
        self.por_clave = {}
        for n in objetivos:
            for v in variantes(n):
                self.por_clave[v] = n
        self.por_mmsi = {str(d["mmsi"]): n for n, d in mapa.items() if d.get("mmsi")}
        self.nombres_vistos = {}                   # clave_ais -> (mmsi, nombre)
        self.estaticos = {}                        # mmsi -> datos estáticos
        self.posiciones = {}                       # mmsi -> última posición
        self.en_puerto = {}                        # puerto -> {mmsi: (sog, nav, lat, lon)}
        self.tipos = lee_json(DATOS / "tipos_cache.json", {})
        self.mensajes = {"A": 0, "B": 0, "C": 0}
        self.cobertura = {}                        # celda 10°x10° -> mensajes estáticos
        self.errores = []

    def identifica(self, mmsi, nombre, imo=None):
        mmsi = str(mmsi)
        if nombre:
            self.nombres_vistos[clave_ais(nombre)] = (mmsi, nombre.strip())
        if mmsi in self.por_mmsi:
            return self.por_mmsi[mmsi]
        n = self.por_clave.get(clave_ais(nombre or ""))
        if n:
            self.mapa[n] = {"mmsi": mmsi, "imo": imo, "nombre_ais": (nombre or "").strip(),
                            "metodo": "exacto", "desde": ahora().isoformat()}
            self.por_mmsi[mmsi] = n
        return n

    def procesa(self, conexion, raw):
        self.mensajes[conexion] += 1
        msg = json.loads(raw)
        if "error" in msg:
            self.errores.append(f"{conexion}: {msg['error']}")
            return
        tipo = msg.get("MessageType")
        meta = msg.get("MetaData", {})
        cuerpo = msg.get("Message", {}).get(tipo, {})
        mmsi = str(meta.get("MMSI") or cuerpo.get("UserID") or "")
        if not mmsi:
            return
        nombre = (meta.get("ShipName") or "").strip()

        if tipo == "ShipStaticData":
            imo = cuerpo.get("ImoNumber") or None
            self.tipos[mmsi] = cuerpo.get("Type")
            mlat, mlon = meta.get("latitude"), meta.get("longitude")
            if isinstance(mlat, (int, float)) and isinstance(mlon, (int, float)) and (mlat or mlon):
                celda = f"{int(mlat // 10) * 10},{int(mlon // 10) * 10}"
                self.cobertura[celda] = self.cobertura.get(celda, 0) + 1
            n = self.identifica(mmsi, cuerpo.get("Name") or nombre, imo)
            if n and isinstance(mlat, (int, float)) and (mlat or mlon) and mmsi not in self.posiciones:
                # Los datos estáticos también traen la posición del barco: sirve si no hubo PositionReport
                self.posiciones[mmsi] = {"buque": n, "mmsi": mmsi, "lat": round(mlat, 4), "lon": round(mlon, 4),
                                         "sog": None, "cog": None, "nav": None,
                                         "hora": parse_hora(meta.get("time_utc")), "fuente": conexion}
            if n:
                self.estaticos[mmsi] = {
                    "destino": (cuerpo.get("Destination") or "").strip(),
                    "eta_ais": eta_ais_a_fecha(cuerpo.get("Eta") or {}),
                    "imo": imo, "calado": cuerpo.get("MaximumStaticDraught"),
                    "hora": meta.get("time_utc"),
                }
            return

        if tipo != "PositionReport":
            return
        lat, lon = cuerpo.get("Latitude"), cuerpo.get("Longitude")
        if lat is None or lon is None or abs(lat) > 90 or abs(lon) > 180:
            return
        sog, nav = cuerpo.get("Sog"), cuerpo.get("NavigationalStatus")
        n = self.identifica(mmsi, nombre)
        if n:
            self.posiciones[mmsi] = {"buque": n, "mmsi": mmsi, "lat": round(lat, 4), "lon": round(lon, 4),
                                     "sog": sog, "cog": cuerpo.get("Cog"), "nav": nav,
                                     "hora": parse_hora(meta.get("time_utc")), "fuente": conexion}
        if conexion == "C":
            for p, d in PUERTOS.items():
                if en_caja(lat, lon, d["caja"]):
                    self.en_puerto.setdefault(p, {})[mmsi] = (sog, nav, lat, lon, nombre)
                    break


def parse_hora(t):
    if not t:
        return ahora().isoformat(timespec="seconds")
    try:  # '2026-10-31 01:17:45.123456789 +0000 UTC'
        return datetime.strptime(t[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).isoformat()
    except ValueError:
        return ahora().isoformat(timespec="seconds")


def eta_ais_a_fecha(e):
    """La ETA AIS no trae año: se toma el año que la deja más cerca de hoy."""
    try:
        mes, dia = int(e.get("Month", 0)), int(e.get("Day", 0))
        if not (1 <= mes <= 12 and 1 <= dia <= 31):
            return None
        hoy = ahora().date()
        candidatos = []
        for y in (hoy.year - 1, hoy.year, hoy.year + 1):
            try:
                candidatos.append(date(y, mes, dia))
            except ValueError:
                pass
        return min(candidatos, key=lambda d: abs((d - hoy).days)).isoformat()
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- conexiones
async def escucha(conexion, suscripcion, corrida, fin):
    intentos = 0
    while asyncio.get_running_loop().time() < fin and intentos < 3:
        intentos += 1
        try:
            async with websockets.connect(URL, open_timeout=20, ping_interval=20, max_size=2 ** 23) as ws:
                await ws.send(json.dumps(suscripcion))
                while True:
                    resto = fin - asyncio.get_running_loop().time()
                    if resto <= 0:
                        return
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=resto)
                    except asyncio.TimeoutError:
                        return
                    try:
                        corrida.procesa(conexion, raw)
                    except Exception as e:  # un mensaje raro no debe tumbar la corrida
                        corrida.errores.append(f"{conexion} mensaje: {e!r}"[:200])
        except Exception as e:
            corrida.errores.append(f"{conexion} conexión (intento {intentos}): {e!r}"[:300])
            await asyncio.sleep(5)


async def recolectar(llave, corrida):
    fin = asyncio.get_running_loop().time() + DURACION
    mundo = [[[-90, -180], [90, 180]]]
    tareas = [escucha("A", {"APIKey": llave, "BoundingBoxes": mundo,
                            "FilterMessageTypes": ["ShipStaticData"]}, corrida, fin),
              escucha("C", {"APIKey": llave, "BoundingBoxes": [d["caja"] for d in PUERTOS.values()],
                            "FilterMessageTypes": ["PositionReport"]}, corrida, fin)]
    # Prioridad: los que llegan antes
    conocidos = sorted((n for n in corrida.objetivos if corrida.mapa.get(n, {}).get("mmsi")),
                       key=lambda n: corrida.objetivos[n]["eta"])
    mmsis = [str(corrida.mapa[n]["mmsi"]) for n in conocidos][:MAX_MMSI]
    if mmsis:
        tareas.append(escucha("B", {"APIKey": llave, "BoundingBoxes": mundo, "FiltersShipMMSI": mmsis,
                                    "FilterMessageTypes": ["PositionReport", "ShipStaticData"]},
                              corrida, fin))
    await asyncio.gather(*tareas)


# ---------------------------------------------------------------- guardado
def coincidencias_aproximadas(corrida):
    """Para buques sin MMSI: busca nombres AIS muy parecidos (errores de captura)."""
    claves = list(corrida.nombres_vistos)
    for n in corrida.objetivos:
        if corrida.mapa.get(n, {}).get("mmsi"):
            continue
        for v in variantes(n):
            m = difflib.get_close_matches(v, claves, n=1, cutoff=0.9)
            if m:
                mmsi, nombre = corrida.nombres_vistos[m[0]]
                if mmsi not in corrida.por_mmsi:
                    corrida.mapa[n] = {"mmsi": mmsi, "nombre_ais": nombre, "metodo": "aproximado",
                                       "desde": ahora().isoformat()}
                    corrida.por_mmsi[mmsi] = n
                break


def congestion(corrida):
    filas = []
    for p in PUERTOS:
        barcos = corrida.en_puerto.get(p, {})
        fondeados = atracados = navegando = 0
        for mmsi, (sog, nav, lat, lon, _) in barcos.items():
            t = corrida.tipos.get(mmsi)
            if not (isinstance(t, int) and 70 <= t <= 79):   # solo buques de carga
                continue
            if nav == 5:
                atracados += 1
            elif nav == 1 or (sog is not None and sog < 0.5 and
                              distancia_mn(lat, lon, PUERTOS[p]["lat"], PUERTOS[p]["lon"]) > 3):
                fondeados += 1
            elif sog is not None and sog < 0.5:
                atracados += 1
            else:
                navegando += 1
        filas.append({"puerto": p, "fondeados": fondeados, "atracados": atracados,
                      "navegando": navegando, "vistos": len(barcos)})
    return filas


def agrega_csv(ruta, filas, campos):
    ruta.parent.mkdir(parents=True, exist_ok=True)
    nuevo = not ruta.exists()
    with open(ruta, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=campos, extrasaction="ignore")
        if nuevo:
            w.writeheader()
        w.writerows(filas)


def guarda(corrida, inicio):
    coincidencias_aproximadas(corrida)
    marca = ahora().isoformat(timespec="seconds")

    escribe_json(DATOS / "buques_ais.json", corrida.mapa)

    # Tipos: se conservan solo los de barcos vistos en puertos (para no crecer sin límite)
    en_puertos = {m for d in corrida.en_puerto.values() for m in d}
    previos = lee_json(DATOS / "tipos_cache.json", {})
    previos.update({m: corrida.tipos[m] for m in en_puertos if m in corrida.tipos})
    escribe_json(DATOS / "tipos_cache.json", previos)

    posiciones = lee_json(DATOS / "posiciones.json", {})
    for p in corrida.posiciones.values():
        posiciones[p["buque"]] = p
    escribe_json(DATOS / "posiciones.json", posiciones)

    etas = lee_json(DATOS / "eta_ais.json", {})
    for mmsi, e in corrida.estaticos.items():
        n = corrida.por_mmsi.get(mmsi)
        if not n:
            continue
        r = etas.setdefault(n, {"primer_eta_ais": e["eta_ais"], "primer_destino": e["destino"],
                                "primer_visto": marca})
        r.update({"eta_ais": e["eta_ais"], "destino": e["destino"], "imo": e["imo"],
                  "calado": e["calado"], "ultimo_visto": marca})
    escribe_json(DATOS / "eta_ais.json", etas)

    mes = ahora().strftime("%Y-%m")
    agrega_csv(HISTORIAL / f"posiciones_{mes}.csv", list(corrida.posiciones.values()),
               ["hora", "buque", "mmsi", "lat", "lon", "sog", "cog", "nav", "fuente"])

    cong = congestion(corrida)
    for c in cong:
        c["fecha"] = marca
    agrega_csv(HISTORIAL / "congestion.csv", cong,
               ["fecha", "puerto", "fondeados", "atracados", "navegando", "vistos"])

    # Eventos de nuestros buques dentro de un puerto (sirve para medir llegadas reales y esperas)
    eventos = []
    for p, barcos in corrida.en_puerto.items():
        for mmsi, (sog, nav, lat, lon, _) in barcos.items():
            n = corrida.por_mmsi.get(mmsi)
            if n:
                estado = "atracado" if nav == 5 else ("fondeado" if nav == 1 or (sog or 0) < 0.5 else "navegando")
                eventos.append({"fecha": marca, "buque": n, "mmsi": mmsi, "puerto": p, "estado": estado})
    agrega_csv(HISTORIAL / "eventos_puerto.csv", eventos, ["fecha", "buque", "mmsi", "puerto", "estado"])

    resumen = {"inicio": inicio, "fin": marca, "duracion_s": DURACION, "mensajes": corrida.mensajes,
               "buques_objetivo": len(corrida.objetivos),
               "con_mmsi": sum(1 for n in corrida.objetivos if corrida.mapa.get(n, {}).get("mmsi")),
               "posiciones_nuevas": len(corrida.posiciones), "errores": corrida.errores[:20],
               "cobertura": dict(sorted(corrida.cobertura.items(), key=lambda x: -x[1])[:40])}
    escribe_json(DATOS / "ultima_corrida.json", resumen)
    print(json.dumps(resumen, ensure_ascii=False, indent=1))


def main():
    llave = os.environ.get("AISSTREAM_API_KEY", "").strip()
    if not llave:
        sys.exit("Falta AISSTREAM_API_KEY")
    inicio = ahora().isoformat(timespec="seconds")
    corrida = Corrida(buques_objetivo(), lee_json(DATOS / "buques_ais.json", {}))
    print(f"Escuchando {DURACION}s · {len(corrida.objetivos)} buques objetivo")
    asyncio.run(recolectar(llave, corrida))
    guarda(corrida, inicio)
    if sum(corrida.mensajes.values()) == 0:
        sys.exit("No se recibió ningún mensaje de aisstream. Revisa la llave.")


if __name__ == "__main__":
    main()
