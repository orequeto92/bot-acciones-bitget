# -*- coding: utf-8 -*-
"""
REPLAY.PY - Rebobina el motor a un momento concreto de una sesion pasada.

Para que sirve
--------------
El mercado de acciones esta abierto 6.5h al dia, 5 dias a la semana. Eso deja
127 de las 168 horas de la semana en las que NO se puede probar nada en vivo. Y
justo lo mas delicado del bot (ORB, gap, fases de sesion) solo existe con el
mercado abierto.

Este modulo coge las velas historicas, las CORTA en un instante pasado y hace
creer al motor que "ahora" es ese instante. Asi se puede:
  - ver que habria dicho el bot el viernes a las 10:30 ET
  - comprobar que el ORB y el gap se calculan bien
  - calibrar umbrales sin esperar a la apertura

USO:
  python tools/replay.py NVDAUSDT                    # recorre la ultima sesion entera
  python tools/replay.py NVDAUSDT --hora 10:30       # un momento concreto
  python tools/replay.py NVDAUSDT --dia 2026-07-31   # una sesion concreta
  python tools/replay.py --todos --hora 10:30        # todo el universo a esa hora
"""
import sys, os, argparse, datetime as dt

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import bitget, sesion, acciones
import config as C

_cache = {}

def _velas(sym):
    if sym not in _cache:
        _cache[sym] = (bitget.klines(sym, C.TF_SCAN, C.KLINES_LIMIT),
                       bitget.klines(sym, C.TF_HTF, C.KLINES_LIMIT_HTF))
    return _cache[sym]


def _corta(velas, utc):
    """Solo las velas que YA habian cerrado en ese instante. Sin esto el motor
    veria el futuro y cualquier resultado seria mentira."""
    lim = int(utc.replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
    return [v for v in velas if int(v[0]) <= lim]


def momento(sym, utc, capital):
    """Corre el motor como si fuesen las `utc`."""
    k5, kh = _velas(sym)
    return acciones.analizar_con_velas(sym, capital, _corta(k5, utc), _corta(kh, utc), utc=utc)


def sesiones_disponibles(sym):
    k5, _ = _velas(sym)
    return [f for f, _ in sesion.agrupar_por_sesion(k5, C.calendario_de(sym))]


def recorrer(sym, fecha, capital, paso_min=15, verbose=True):
    """Recorre una sesion entera de 15 en 15 minutos y lista lo que habria dicho."""
    cal = C.calendario_de(sym)
    h = sesion.horario_del_dia(fecha, cal)
    if not h:
        print(f"  {fecha} no fue dia de mercado."); return []
    abre, cierra = h
    hallazgos = []
    t = abre + dt.timedelta(minutes=C.ORB_MINUTOS)
    while t < cierra:
        utc = sesion.et_a_utc(t)
        r = momento(sym, utc, capital)
        etiqueta = "-"
        if r.get("error"):          etiqueta = f"error: {r['error']}"
        elif r.get("cerrado"):      etiqueta = "cerrado"
        elif r.get("sin_setup"):    etiqueta = "sin setup"
        elif r.get("descartada"):
            e = r["ens"]
            etiqueta = (f"descartada {e['side']:5} score {e['score']:>3} "
                        f"({r.get('motivo') or 'umbral'})")
        else:
            e = r["ens"]
            etiqueta = (f"*** SEÑAL {e['side'].upper():5} score {e['score']:>3} "
                        f"{'VERDE' if r['verde'] else 'ROJA'} setup {e['best']['name']}")
            hallazgos.append((t, r))
        if verbose:
            print(f"  {t.strftime('%H:%M')} ET  {etiqueta}")
        t += dt.timedelta(minutes=paso_min)
    return hallazgos


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("symbols", nargs="*", default=[])
    ap.add_argument("--todos", action="store_true", help="todo config.ACTIVOS")
    ap.add_argument("--dia", help="fecha de la sesion (YYYY-MM-DD); por defecto la ultima")
    ap.add_argument("--hora", help="hora ET concreta (HH:MM); si falta, recorre la sesion")
    ap.add_argument("--paso", type=int, default=15, help="minutos entre pasos al recorrer")
    ap.add_argument("--capital", type=float, default=C.CAPITAL_TOTAL)
    a = ap.parse_args()

    symbols = C.ACTIVOS if a.todos else ([s.upper() for s in a.symbols] or ["NVDAUSDT"])

    for sym in symbols:
        disponibles = sesiones_disponibles(sym)
        if not disponibles:
            print(f"\n### {sym}: sin sesiones en el historial descargado."); continue
        fecha = dt.date.fromisoformat(a.dia) if a.dia else disponibles[-1]
        if fecha not in disponibles:
            print(f"\n### {sym}: {fecha} no esta en el historial "
                  f"(disponibles: {', '.join(str(d) for d in disponibles)})")
            continue

        print(f"\n### {sym} — sesion del {fecha} ({fecha.strftime('%A')})")
        if a.hora:
            hh, mm = (int(x) for x in a.hora.split(":"))
            t = dt.datetime.combine(fecha, dt.time(hh, mm))
            r = momento(sym, sesion.et_a_utc(t), a.capital)
            if r.get("error"):        print(f"  {t.strftime('%H:%M')} ET  error: {r['error']}")
            elif r.get("cerrado"):    print(f"  {t.strftime('%H:%M')} ET  mercado cerrado")
            elif r.get("sin_setup"):
                ctx = r.get("ctx", {})
                print(f"  {t.strftime('%H:%M')} ET  sin setup "
                      f"(ORB {'si' if ctx.get('orb') else 'no'} | "
                      f"gap {'si' if ctx.get('gap') else 'no'})")
            elif r.get("descartada"):
                e = r["ens"]
                print(f"  {t.strftime('%H:%M')} ET  descartada: {e['side']} score {e['score']} "
                      f"({r.get('motivo') or 'umbral ' + str(r.get('min_score'))})")
                print(f"     disparo: " + " | ".join(
                    f"{d['name']}:{d['score']}" for d in e["confirmantes"]))
            else:
                print("\n" + acciones.formatear(r))
        else:
            hallazgos = recorrer(sym, fecha, a.capital, a.paso)
            if hallazgos:
                print(f"\n  --- {len(hallazgos)} señal(es) en la sesion; detalle de la primera ---")
                print("\n" + acciones.formatear(hallazgos[0][1]))


if __name__ == "__main__":
    main()
