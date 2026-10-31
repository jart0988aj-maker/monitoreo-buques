"""Configuración y utilidades compartidas."""
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
DATOS = RAIZ / "docs" / "data"
HISTORIAL = DATOS / "historial"

# Puertos: coordenadas del puerto y caja geográfica [[lat_min, lon_min], [lat_max, lon_max]]
# que incluye muelles y zona de fondeo.
PUERTOS = {
    "MANZANILLO":   {"lat": 19.06, "lon": -104.30, "caja": [[18.90, -104.55], [19.20, -104.15]], "pais": "MX"},
    "LAZARO CARDENAS": {"lat": 17.93, "lon": -102.18, "caja": [[17.80, -102.35], [18.00, -102.05]], "pais": "MX"},
    "SHANGHAI":     {"lat": 30.63, "lon": 122.07, "caja": [[30.35, 121.60], [31.55, 122.80]], "pais": "CN"},
    "NINGBO":       {"lat": 29.93, "lon": 121.88, "caja": [[29.60, 121.70], [30.20, 122.60]], "pais": "CN"},
    "YANTIAN":      {"lat": 22.57, "lon": 114.27, "caja": [[22.10, 113.70], [22.65, 114.60]], "pais": "CN"},
    "QINGDAO":      {"lat": 36.02, "lon": 120.20, "caja": [[35.70, 119.95], [36.25, 120.75]], "pais": "CN"},
    "LIANYUNGANG":  {"lat": 34.74, "lon": 119.45, "caja": [[34.55, 119.25], [34.90, 119.85]], "pais": "CN"},
    "BUSAN":        {"lat": 35.08, "lon": 128.83, "caja": [[34.85, 128.65], [35.20, 129.25]], "pais": "KR"},
    "KEELUNG":      {"lat": 25.15, "lon": 121.75, "caja": [[25.08, 121.62], [25.30, 121.90]], "pais": "TW"},
    "KAOHSIUNG":    {"lat": 22.56, "lon": 120.30, "caja": [[22.40, 120.10], [22.75, 120.40]], "pais": "TW"},
    "HO CHI MINH":  {"lat": 10.45, "lon": 107.05, "caja": [[10.20, 106.70], [10.80, 107.30]], "pais": "VN"},
    "HAIPHONG":     {"lat": 20.75, "lon": 106.85, "caja": [[20.55, 106.65], [20.95, 107.15]], "pais": "VN"},
    "SINGAPORE":    {"lat": 1.26,  "lon": 103.80, "caja": [[1.10, 103.55], [1.40, 104.20]], "pais": "SG"},
}
# Sinónimos que aparecen en el Excel
ALIAS_PUERTO = {
    "VUNG TAU": "HO CHI MINH", "CAI MEP": "HO CHI MINH", "HOCHIMINH": "HO CHI MINH",
    "SHEKOU": "YANTIAN", "SHENZHEN": "YANTIAN", "NANSHA": "YANTIAN",
    "LAZARO CARDENAS": "LAZARO CARDENAS", "LZC": "LAZARO CARDENAS",
}
DIAS_A_PLANTA = 7


def ahora():
    return datetime.now(timezone.utc)


def normaliza_buque(nombre):
    """'EVER LINKING 0805-072E ' -> 'EVER LINKING'."""
    if nombre is None:
        return ""
    s = str(nombre).upper().strip()
    if s in ("", "NAN", "NONE", "PENDIENTE", "TBD", "TBA"):
        return ""
    s = re.sub(r"[^A-Z0-9 \-]", " ", s)
    s = re.sub(r"\s+V\.?\s*\d[0-9A-Z\-]*$", "", s)        # " V. 123E"
    s = re.sub(r"\s+[0-9][0-9A-Z\-/ ]*$", "", s)           # número de viaje al final
    s = re.sub(r"\s+[A-Z]{1,2}\d{3,}[A-Z]?$", "", s)     # código de viaje tipo FW603E / E014
    s = re.sub(r"^(VESSEL|MV|M/V|BUQUE)\s+", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    if s in NO_BUQUES or s in PUERTOS or s.isdigit() or len(s) < 3:
        return ""
    return s


NO_BUQUES = {"FDR", "FEDER", "FEEDER", "DUMMY BARGE", "TBN", "TBA", "WAN HAI", "EVER", "MSC", "ONE"}


def clave_ais(nombre):
    """Clave para comparar nombres AIS con el Excel (sin espacios ni guiones)."""
    return re.sub(r"[^A-Z0-9]", "", normaliza_buque(nombre))


def normaliza_puerto(p):
    s = re.sub(r"\s+", " ", str(p or "").upper().strip())
    return ALIAS_PUERTO.get(s, s)


def distancia_mn(lat1, lon1, lat2, lon2):
    """Distancia ortodrómica en millas náuticas."""
    r = 3440.065
    f1, f2 = math.radians(lat1), math.radians(lat2)
    df = f2 - f1
    dl = math.radians(lon2 - lon1)
    a = math.sin(df / 2) ** 2 + math.cos(f1) * math.cos(f2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def en_caja(lat, lon, caja):
    (la1, lo1), (la2, lo2) = caja
    return la1 <= lat <= la2 and lo1 <= lon <= lo2


def lee_json(ruta, defecto):
    try:
        return json.loads(Path(ruta).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return defecto


def escribe_json(ruta, datos):
    Path(ruta).parent.mkdir(parents=True, exist_ok=True)
    Path(ruta).write_text(json.dumps(datos, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
