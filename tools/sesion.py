# -*- coding: utf-8 -*-
"""
SESION.PY - Reloj de mercado. La pieza que NO existia en el bot de cripto.

Por que hace falta
------------------
El perpetuo de una accion en Bitget cotiza 24/7, pero la accion de verdad solo
se mueve de 9:30 a 16:00 ET. Medido en NVDA (velas 1h reales, 31-jul-2026):

    09:30-10:30 ET  $3.40M   rango 1.67%   <- sesion
    15:00-16:00 ET  $1.26M   rango 1.79%   <- sesion
    19:00-20:00 ET  $  27K   rango 0.07%   <- fuera de sesion
    sabado entero   $10-50K  rango 0.10%   <- fuera de sesion

O sea: fuera de sesion el precio es una linea plana con volumen residual. Si
metes esas velas en ta.compute() pasan tres cosas, todas malas:

  1. el ATR se aplana  -> los SL salen absurdamente pegados al precio
  2. el RSI se clava en 50 -> ninguna divergencia real
  3. BREAKOUT dispara falso -> lee 12h de linea plana nocturna como
     "consolidacion" y canta rotura con el primer tick de la apertura

Por eso TODO lo que entra al analisis pasa antes por filtrar_rth().

Sin dependencias: `zoneinfo` no sirve en Windows (no trae la base IANA), asi
que la conversion a hora del Este y el DST van implementados a mano.
"""
import datetime as dt

# ------------------------------------------------------------------
# CALENDARIOS
# ------------------------------------------------------------------
# Cada activo del universo se mapea a uno de estos (lo hace config.CALENDARIO_DE).
CALENDARIOS = {
    # Acciones y ETFs de EE.UU. -> NYSE/NASDAQ, sesion regular (RTH).
    "US_EQUITY": {
        "nombre":   "Acciones US (NYSE/NASDAQ)",
        "dias":     (0, 1, 2, 3, 4),      # lunes=0 ... viernes=4
        "abre":     (9, 30),
        "cierra":   (16, 0),
        "festivos": True,                 # respeta los festivos de NYSE
        "medias":   True,                 # respeta las medias sesiones (cierre 13:00)
        "orb":      True,                 # tiene apertura con rango de apertura
    },
    # Materias primas (XAU, XAG, CL, NATGAS). El subyacente cotiza casi 24/5,
    # pero la liquidez de verdad va de la apertura de Londres al cierre de NY.
    # Fuera de esa ventana el perp de Bitget se mueve por goteo.
    "COMMODITY": {
        "nombre":   "Materias primas (ventana Londres-NY)",
        "dias":     (0, 1, 2, 3, 4),
        "abre":     (3, 0),
        "cierra":   (17, 0),
        "festivos": True,                 # en festivo US la liquidez tambien muere
        "medias":   False,
        "orb":      False,                # no hay "campana" de apertura
    },
}

DEFECTO = "US_EQUITY"


# ------------------------------------------------------------------
# HORA DEL ESTE (ET) CON DST, A MANO
# ------------------------------------------------------------------
def _nth_dow(anio, mes, dow, n):
    """Fecha del n-esimo dia de la semana `dow` (lunes=0) del mes. n=-1 = ultimo."""
    if n > 0:
        d = dt.date(anio, mes, 1)
        adelanto = (dow - d.weekday()) % 7
        return d + dt.timedelta(days=adelanto + 7 * (n - 1))
    # ultimo del mes
    if mes == 12:
        d = dt.date(anio, 12, 31)
    else:
        d = dt.date(anio, mes + 1, 1) - dt.timedelta(days=1)
    return d - dt.timedelta(days=(d.weekday() - dow) % 7)


def es_horario_verano(utc):
    """DST de EE.UU. (regla vigente desde 2007): empieza el 2o domingo de marzo
    a las 02:00 locales (07:00 UTC) y acaba el 1er domingo de noviembre a las
    02:00 locales (06:00 UTC)."""
    ini = dt.datetime.combine(_nth_dow(utc.year, 3, 6, 2), dt.time(7, 0))
    fin = dt.datetime.combine(_nth_dow(utc.year, 11, 6, 1), dt.time(6, 0))
    return ini <= utc.replace(tzinfo=None) < fin


def offset_et(utc):
    """-4 en verano (EDT), -5 en invierno (EST)."""
    return -4 if es_horario_verano(utc) else -5


def ahora_utc():
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def a_et(utc=None):
    """UTC (naive) -> hora del Este (naive). Es la hora que ve el mercado."""
    utc = utc or ahora_utc()
    return utc + dt.timedelta(hours=offset_et(utc))


def et_a_utc(et):
    """Hora del Este -> UTC. Aproxima el offset con la propia fecha ET, que es
    exacto salvo en la hora del cambio de horario (irrelevante: el mercado esta
    cerrado a las 2 de la madrugada)."""
    return et - dt.timedelta(hours=offset_et(et))


def ts_a_et(ms):
    """Timestamp de vela (ms, UTC) -> hora del Este."""
    return a_et(dt.datetime.utcfromtimestamp(int(ms) / 1000.0))


# ------------------------------------------------------------------
# FESTIVOS Y MEDIAS SESIONES DE NYSE (calculados, no listados)
# ------------------------------------------------------------------
def _pascua(anio):
    """Domingo de Pascua (algoritmo de Gauss/Butcher, calendario gregoriano)."""
    a = anio % 19
    b, c = divmod(anio, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    mes, dia = divmod(h + l - 7 * m + 114, 31)
    return dt.date(anio, mes, dia + 1)


def _observado(fecha):
    """Regla NYSE: si el festivo cae en sabado se observa el viernes anterior;
    si cae en domingo, el lunes siguiente."""
    if fecha.weekday() == 5:
        return fecha - dt.timedelta(days=1)
    if fecha.weekday() == 6:
        return fecha + dt.timedelta(days=1)
    return fecha


def festivos_nyse(anio):
    """Los 10 festivos anuales de NYSE, calculados para cualquier año."""
    f = set()
    # Año Nuevo: si cae en sabado NO se adelanta al viernes (el mercado abre)
    an = dt.date(anio, 1, 1)
    f.add(an + dt.timedelta(days=1) if an.weekday() == 6 else an)
    f.add(_nth_dow(anio, 1, 0, 3))              # MLK: 3er lunes de enero
    f.add(_nth_dow(anio, 2, 0, 3))              # Presidents: 3er lunes de febrero
    f.add(_pascua(anio) - dt.timedelta(days=2)) # Viernes Santo
    f.add(_nth_dow(anio, 5, 0, -1))             # Memorial: ultimo lunes de mayo
    f.add(_observado(dt.date(anio, 6, 19)))     # Juneteenth
    f.add(_observado(dt.date(anio, 7, 4)))      # Independencia
    f.add(_nth_dow(anio, 9, 0, 1))              # Labor Day: 1er lunes de septiembre
    f.add(_nth_dow(anio, 11, 3, 4))             # Accion de Gracias: 4o jueves
    f.add(_observado(dt.date(anio, 12, 25)))    # Navidad
    return {d for d in f if d.weekday() < 5}


def medias_sesiones(anio):
    """Dias de cierre anticipado a las 13:00 ET."""
    fest = festivos_nyse(anio)
    m = set()
    jul3 = dt.date(anio, 7, 3)                            # vispera de Independencia
    if jul3.weekday() < 5 and jul3 not in fest:
        m.add(jul3)
    m.add(_nth_dow(anio, 11, 3, 4) + dt.timedelta(days=1))  # viernes tras Accion de Gracias
    dic24 = dt.date(anio, 12, 24)                          # Nochebuena
    if dic24.weekday() < 5 and dic24 not in fest:
        m.add(dic24)
    return {d for d in m if d.weekday() < 5 and d not in fest}


_cache_cal = {}

def _calendario_anual(anio):
    if anio not in _cache_cal:
        _cache_cal[anio] = (festivos_nyse(anio), medias_sesiones(anio))
    return _cache_cal[anio]


def es_festivo(fecha):
    return fecha in _calendario_anual(fecha.year)[0]


def es_media_sesion(fecha):
    return fecha in _calendario_anual(fecha.year)[1]


# ------------------------------------------------------------------
# ESTADO DE LA SESION
# ------------------------------------------------------------------
def _cal(cal):
    return CALENDARIOS.get(cal or DEFECTO, CALENDARIOS[DEFECTO])


def horario_del_dia(fecha, cal=DEFECTO):
    """(apertura_et, cierre_et) de esa fecha, o None si el mercado no abre."""
    c = _cal(cal)
    if fecha.weekday() not in c["dias"]:
        return None
    if c["festivos"] and es_festivo(fecha):
        return None
    abre = dt.datetime.combine(fecha, dt.time(*c["abre"]))
    if c["medias"] and es_media_sesion(fecha):
        cierra = dt.datetime.combine(fecha, dt.time(13, 0))
    else:
        cierra = dt.datetime.combine(fecha, dt.time(*c["cierra"]))
    return abre, cierra


def en_sesion(et, cal=DEFECTO):
    """True si esa hora ET cae dentro de la sesion."""
    h = horario_del_dia(et.date(), cal)
    return bool(h) and h[0] <= et < h[1]


def estado(cal=DEFECTO, utc=None, orb_min=15, aviso_cierre_min=30):
    """Foto del mercado ahora mismo. Es la puerta que usa el escaneo.

    fase:
      cerrado        -> no se escanea nada
      apertura       -> primeros `orb_min` minutos: se forma el rango, NO se entra
      sesion         -> ventana normal de operativa
      cierre_cercano -> ultimos `aviso_cierre_min`: no se abre nada nuevo, se cierra
    """
    utc = utc or ahora_utc()
    et = a_et(utc)
    h = horario_del_dia(et.date(), cal)
    if not h:
        motivo = ("fin de semana" if et.weekday() >= 5
                  else "festivo NYSE" if es_festivo(et.date()) else "fuera de calendario")
        return {"abierto": False, "fase": "cerrado", "motivo": motivo, "et": et,
                "cal": cal, "media_sesion": False,
                "desde_apertura": None, "para_cierre": None}
    abre, cierra = h
    media = _cal(cal)["medias"] and es_media_sesion(et.date())
    if et < abre:
        return {"abierto": False, "fase": "cerrado", "et": et, "cal": cal,
                "motivo": f"pre-mercado (abre {abre.strftime('%H:%M')} ET)",
                "media_sesion": media, "desde_apertura": None,
                "para_cierre": None, "abre": abre, "cierra": cierra}
    if et >= cierra:
        return {"abierto": False, "fase": "cerrado", "et": et, "cal": cal,
                "motivo": f"post-mercado (cerro {cierra.strftime('%H:%M')} ET)",
                "media_sesion": media, "desde_apertura": None,
                "para_cierre": None, "abre": abre, "cierra": cierra}

    desde = (et - abre).total_seconds() / 60.0
    para = (cierra - et).total_seconds() / 60.0
    if _cal(cal)["orb"] and desde < orb_min:
        fase = "apertura"
    elif para <= aviso_cierre_min:
        fase = "cierre_cercano"
    else:
        fase = "sesion"
    return {"abierto": True, "fase": fase, "et": et, "cal": cal,
            "motivo": "", "media_sesion": media,
            "desde_apertura": desde, "para_cierre": para,
            "abre": abre, "cierra": cierra}


# ------------------------------------------------------------------
# FILTRADO DE VELAS
# ------------------------------------------------------------------
def filtrar_rth(velas, cal=DEFECTO):
    """Deja SOLO las velas que abrieron dentro de sesion. Es lo primero que se
    hace con cualquier serie antes de calcular un indicador."""
    return [v for v in velas if en_sesion(ts_a_et(v[0]), cal)]


def agrupar_por_sesion(velas, cal=DEFECTO):
    """[(fecha, [velas])] ordenado por fecha. Cada grupo es un dia de mercado."""
    dias = {}
    for v in velas:
        et = ts_a_et(v[0])
        if en_sesion(et, cal):
            dias.setdefault(et.date(), []).append(v)
    return [(d, dias[d]) for d in sorted(dias)]


def velas_de_hoy(velas, cal=DEFECTO, utc=None):
    """Velas de la sesion en curso (o de la ultima sesion con datos)."""
    grupos = agrupar_por_sesion(velas, cal)
    if not grupos:
        return []
    hoy = a_et(utc or ahora_utc()).date()
    for fecha, vs in reversed(grupos):
        if fecha == hoy:
            return vs
    return grupos[-1][1]


def rango_apertura(velas, cal=DEFECTO, minutos=15, utc=None):
    """Rango de apertura (ORB): maximo y minimo de los primeros `minutos` de la
    sesion de hoy. Es la base de la estrategia ORB.

    Devuelve None si aun no ha pasado la ventana completa (no se opera un rango
    a medio formar)."""
    h = horario_del_dia(a_et(utc or ahora_utc()).date(), cal)
    if not h:
        return None
    abre = h[0]
    fin = abre + dt.timedelta(minutes=minutos)
    dentro = [v for v in velas if abre <= ts_a_et(v[0]) < fin]
    if not dentro:
        return None
    # exige la ventana completa: si el rango sigue formandose, no hay ORB
    if a_et(utc or ahora_utc()) < fin:
        return None
    return {"alto": max(float(v[2]) for v in dentro),
            "bajo": min(float(v[3]) for v in dentro),
            "n": len(dentro), "abre": abre, "fin": fin}


def gap_apertura(velas, cal=DEFECTO, utc=None):
    """Gap de apertura: primer precio de hoy contra el ultimo cierre de la
    sesion anterior. En acciones es informacion de primera: un gap grande marca
    el sesgo del dia y suele dejar un hueco que el precio intenta rellenar."""
    grupos = agrupar_por_sesion(velas, cal)
    if len(grupos) < 2:
        return None
    hoy = a_et(utc or ahora_utc()).date()
    if grupos[-1][0] != hoy:
        return None
    cierre_ayer = float(grupos[-2][1][-1][4])
    apertura_hoy = float(grupos[-1][1][0][1])
    if not cierre_ayer:
        return None
    pct = (apertura_hoy - cierre_ayer) / cierre_ayer * 100.0
    return {"cierre_previo": cierre_ayer, "apertura": apertura_hoy, "pct": pct,
            "direccion": "alcista" if pct > 0 else "bajista",
            "cerrado": (min(float(v[3]) for v in grupos[-1][1]) <= cierre_ayer
                        <= max(float(v[2]) for v in grupos[-1][1]))}


if __name__ == "__main__":
    import sys
    cal = sys.argv[1] if len(sys.argv) > 1 else DEFECTO
    utc = ahora_utc()
    e = estado(cal, utc)
    print(f"UTC  : {utc.strftime('%a %Y-%m-%d %H:%M')}")
    print(f"ET   : {e['et'].strftime('%a %Y-%m-%d %H:%M')} "
          f"({'EDT verano' if es_horario_verano(utc) else 'EST invierno'})")
    print(f"Cal  : {CALENDARIOS[cal]['nombre']}")
    print(f"Fase : {e['fase']}" + (f"  ({e['motivo']})" if e["motivo"] else ""))
    if e["abierto"]:
        print(f"       lleva {e['desde_apertura']:.0f} min abierto, "
              f"cierra en {e['para_cierre']:.0f} min")
    anio = utc.year
    print(f"\nFestivos NYSE {anio}:")
    for d in sorted(festivos_nyse(anio)):
        print(f"  {d} {d.strftime('%a')}")
    print(f"Medias sesiones {anio} (cierre 13:00 ET):")
    for d in sorted(medias_sesiones(anio)):
        print(f"  {d} {d.strftime('%a')}")
