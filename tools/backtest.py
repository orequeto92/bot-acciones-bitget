# -*- coding: utf-8 -*-
"""
BACKTEST.PY - Calibracion de umbrales sobre sesiones reales.

Que hace
--------
1. Baja semanas de historial (paginado + cache en disco).
2. Recorre cada sesion paso a paso, cortando el futuro en cada instante.
3. Recoge TODAS las señales candidatas con los umbrales AL MINIMO (para no
   perder ninguna) y anota de cada una: score, prob, confluencia, estrategia,
   alineacion con el 1h, hora de la sesion y el plan de SL/TP.
4. Simula el resultado de cada candidata con las velas siguientes.
5. Barre umbrales EN MEMORIA sobre ese conjunto: asi se puede comparar
   MIN_SCORE 55 vs 70, o confluencia si/no, sin volver a recorrer nada.

Reglas de la simulacion (deliberadamente pesimistas)
---------------------------------------------------
- Si una vela contiene a la vez el TP y el SL, se asume que toco el SL PRIMERO.
  Con velas de 5m no se sabe el orden real; suponer lo bueno infla resultados.
- Tras TP1 el SL sube a break-even (igual que en vivo).
- La posicion se cierra al final de la sesion pase lo que pase (no hay overnight).
- Solo una posicion por activo a la vez, y cooldown despues de cerrar.
- No incluye comisiones ni slippage. El resultado real sera algo peor.

USO:
  python tools/backtest.py                       # universo entero, 14 dias
  python tools/backtest.py --dias 21 --paso 5
  python tools/backtest.py NVDAUSDT TSLAUSDT --detalle
"""
import sys, os, argparse, datetime as dt, statistics as st

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import bitget, sesion, acciones, estrategias
import config as C

CACHE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "datos", "cache")


# ------------------------------------------------------------------
# SIMULACION DEL RESULTADO
# ------------------------------------------------------------------
def simular(futuras, side, entrada, dist, cierre_sesion_et, cal):
    """Recorre las velas posteriores y devuelve el resultado en R.

    Calcula a la vez el resultado con esquema VERDE (3 TPs 33/33/34) y ROJA
    (2 TPs 33/67, tope 1.7R), porque solo se diferencian en el reparto: asi el
    barrido de umbrales puede aplicar el que toque sin resimular nada.
    """
    tp_r = C.TP_R                                   # [1.0, 1.7, 2.5]
    niveles = [entrada + dist * r if side == "long" else entrada - dist * r
               for r in tp_r]
    sl = entrada - dist if side == "long" else entrada + dist

    alcanzados = 0          # cuantos TPs se tocaron, en orden
    sl_tocado = False
    be = False
    mfe = 0.0               # maxima excursion a favor, en R
    mae = 0.0               # maxima excursion en contra, en R
    salida_r = None         # R al que se cerro el resto (None = sigue vivo)
    ts_salida = int(futuras[-1][0]) if futuras else 0

    for v in futuras:
        et = sesion.ts_a_et(v[0])
        if et >= cierre_sesion_et:
            break
        hi, lo = float(v[2]), float(v[3])
        if side == "long":
            mfe = max(mfe, (hi - entrada) / dist)
            mae = min(mae, (lo - entrada) / dist)
            golpe_sl = lo <= sl
            sig_tp = niveles[alcanzados] if alcanzados < len(niveles) else None
            golpe_tp = sig_tp is not None and hi >= sig_tp
        else:
            mfe = max(mfe, (entrada - lo) / dist)
            mae = min(mae, (entrada - hi) / dist)
            golpe_sl = hi >= sl
            sig_tp = niveles[alcanzados] if alcanzados < len(niveles) else None
            golpe_tp = sig_tp is not None and lo <= sig_tp

        # PESIMISTA: si en la misma vela caben SL y TP, gana el SL
        if golpe_sl:
            sl_tocado = True
            salida_r = 0.0 if be else -1.0
            ts_salida = int(v[0])
            break
        if golpe_tp:
            alcanzados += 1
            if alcanzados == 1:                     # tras TP1, SL a break-even
                be = True
                sl = entrada
            if alcanzados == len(niveles):
                salida_r = tp_r[-1]
                ts_salida = int(v[0])
                break

    # si no cerro por SL ni por TP3, se cierra al final de la sesion
    if salida_r is None:
        ultimo = float(futuras[-1][4]) if futuras else entrada
        salida_r = ((ultimo - entrada) if side == "long" else (entrada - ultimo)) / dist
        evento = "CIERRE_SESION"
    elif sl_tocado:
        evento = "BE" if be else "SL"
    else:
        evento = "TP3"

    def reparto(parciales, rs):
        """R total repartiendo la posicion entre los TPs alcanzados; lo que
        queda vivo sale al precio de salida."""
        total, restante = 0.0, 1.0
        for i, (p, r) in enumerate(zip(parciales, rs)):
            if i < alcanzados:
                total += p * r
                restante -= p
        return total + restante * salida_r

    r_verde = reparto(C.TP_PARCIAL, tp_r)
    tp_r_roja = [r for r in tp_r if r <= C.TP_CAP_ROJA_R]
    par_roja = acciones._parciales_para(len(tp_r_roja))
    alc_roja = min(alcanzados, len(tp_r_roja))
    total, restante = 0.0, 1.0
    for i, (p, r) in enumerate(zip(par_roja, tp_r_roja)):
        if i < alc_roja:
            total += p * r
            restante -= p
    r_roja = total + restante * salida_r

    return {"r_verde": r_verde, "r_roja": r_roja, "evento": evento,
            "tps": alcanzados, "mfe": mfe, "mae": mae, "ts_salida": ts_salida}


# ------------------------------------------------------------------
# RECOGIDA DE CANDIDATAS
# ------------------------------------------------------------------
def recoger(sym, dias, paso, verbose=True):
    """Recorre el historial del activo y devuelve todas las candidatas con su
    resultado simulado. Los umbrales se ponen al minimo a proposito: filtrar
    viene despues, en memoria."""
    cal = C.calendario_de(sym)
    try:
        k5 = bitget.historia(sym, C.TF_SCAN, dias, CACHE)
        # el 1h necesita mucho mas calendario: filtrado a sesion solo deja 6
        # velas por dia, y la EMA50 del sesgo necesita al menos 50
        kh = bitget.historia(sym, C.TF_HTF, max(dias * 4, 60), CACHE)
    except Exception as e:
        if verbose: print(f"  {sym}: error bajando historial ({e})")
        return []
    if len(k5) < 500:
        if verbose: print(f"  {sym}: historial insuficiente ({len(k5)} velas)")
        return []

    k5r = sesion.filtrar_rth(k5, cal)
    grupos = sesion.agrupar_por_sesion(k5, cal)
    idx5 = {int(v[0]): i for i, v in enumerate(k5)}

    # umbrales al minimo durante la recogida
    guardado = (C.MIN_SCORE, C.MIN_PROB, C.EXIGIR_CONFLUENCIA, C.THRESHOLD_MODE)
    C.MIN_SCORE, C.MIN_PROB = 0, 0.0
    C.EXIGIR_CONFLUENCIA, C.THRESHOLD_MODE = False, "FIJO"

    candidatas = []
    try:
        for fecha, velas_sesion in grupos:
            h = sesion.horario_del_dia(fecha, cal)
            if not h:
                continue
            abre, cierra = h
            t = abre + dt.timedelta(minutes=C.ORB_MINUTOS)
            fin = cierra - dt.timedelta(minutes=C.NO_ABRIR_ULTIMOS_MIN)
            while t < fin:
                utc = sesion.et_a_utc(t)
                lim = int(utc.replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
                v5 = [v for v in k5 if int(v[0]) <= lim]
                vh = [v for v in kh if int(v[0]) <= lim]
                r = acciones.analizar_con_velas(sym, C.CAPITAL_TOTAL, v5, vh, utc=utc)
                if r.get("ens") and r.get("plan") and r["plan"].get("viable"):
                    ens, plan = r["ens"], r["plan"]
                    # SOLO las velas que quedan de HOY. Si se cogen todas las
                    # futuras, una entrada pegada al cierre se queda sin velas
                    # antes de la campana y el precio de salida acaba siendo el
                    # de semanas despues -> perdidas fantasma de -44R.
                    cierre_ms = int(sesion.et_a_utc(cierra)
                                    .replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
                    futuras = [v for v in k5r if lim < int(v[0]) < cierre_ms]
                    if futuras:
                        res = simular(futuras, ens["side"], r["m5"]["price"],
                                      plan["dist"], cierra, cal)
                        candidatas.append({
                            "sym": sym, "fecha": fecha, "hora": t.time(),
                            "min_sesion": (t - abre).total_seconds() / 60.0,
                            "side": ens["side"], "score": ens["score"],
                            "prob": ens["prob"], "n_conf": ens["n_conf"],
                            "setup": ens["best"]["name"],
                            "aligned": r["aligned"], "sl_pct": plan["sl_pct"],
                            "atr_pct": r["m5"].get("atr_pct"), "ts": lim, **res})
                t += dt.timedelta(minutes=paso)
    finally:
        C.MIN_SCORE, C.MIN_PROB, C.EXIGIR_CONFLUENCIA, C.THRESHOLD_MODE = guardado

    if verbose:
        print(f"  {sym:<12} {len(grupos):>2} sesiones, {len(candidatas):>4} candidatas")
    return candidatas


# ------------------------------------------------------------------
# APLICAR UMBRALES Y CONTAR
# ------------------------------------------------------------------
def aplicar(candidatas, min_score, min_prob, exigir_conf, single_strong,
            verde_score, verde_prob, max_por_activo_dia=99):
    """Filtra las candidatas con un juego de umbrales y resuelve solapes:
    una sola operacion viva por activo (la siguiente no entra hasta que la
    anterior cerraria por cooldown)."""
    pasan = []
    for c in candidatas:
        if c["score"] < min_score or c["prob"] < min_prob:
            continue
        if exigir_conf and c["n_conf"] < 2:
            if not (c["score"] >= single_strong and c["aligned"]):
                continue
        verde = c["score"] >= verde_score and c["prob"] >= verde_prob and c["aligned"]
        pasan.append({**c, "verde": verde,
                      "r": c["r_verde"] if verde else c["r_roja"],
                      "peso": 1.0 if verde else C.RIESGO_ROJA})
    # --- resolver solapes como lo haria el bot en vivo ---
    # Se recorre en orden CRONOLOGICO global (no por activo) porque el limite de
    # MAX_TRADES_SIMULTANEOS es de cartera, no de activo. Sin esto salian 59
    # operaciones por sesion cuando en vivo caben 2 a la vez: el agregado no
    # tenia nada que ver con lo que se podria operar de verdad.
    pasan.sort(key=lambda c: c["ts"])
    final, ultimo, por_dia = [], {}, {}
    abiertas = []          # [(ts_cierre_estimado, sym)]
    for c in pasan:
        t = c["ts"]
        abiertas = [(fin, s) for fin, s in abiertas if fin > t]
        if len(abiertas) >= C.MAX_TRADES_SIMULTANEOS:
            continue
        if any(s == c["sym"] for _, s in abiertas):
            continue
        if c["sym"] in ultimo and t - ultimo[c["sym"]] < C.COOLDOWN_H * 3600_000:
            continue
        dk = (c["sym"], c["fecha"])
        if por_dia.get(dk, 0) >= max_por_activo_dia:
            continue
        ultimo[c["sym"]] = t
        por_dia[dk] = por_dia.get(dk, 0) + 1
        final.append(c)
        abiertas.append((c.get("ts_salida") or t, c["sym"]))
    return final


def resumen(ops):
    """Estadisticas de un conjunto de operaciones ya filtrado."""
    if not ops:
        return None
    rs = [o["r"] for o in ops]
    # R ponderado por riesgo: una ROJA arriesga 0.30 de una VERDE
    r_pond = [o["r"] * o["peso"] for o in ops]
    ganadoras = [r for r in rs if r > 0.01]
    perdedoras = [r for r in rs if r < -0.01]
    return {
        "n": len(ops),
        "aciertos": len(ganadoras) / len(ops) * 100,
        "r_medio": st.mean(rs),
        "r_total_pond": sum(r_pond),
        "r_mediano": st.median(rs),
        "mejor": max(rs), "peor": min(rs),
        "verdes": sum(1 for o in ops if o["verde"]),
        "sl": sum(1 for o in ops if o["evento"] == "SL"),
        "be": sum(1 for o in ops if o["evento"] == "BE"),
        "tp3": sum(1 for o in ops if o["evento"] == "TP3"),
        "cierre": sum(1 for o in ops if o["evento"] == "CIERRE_SESION"),
        "ganancia_media": st.mean(ganadoras) if ganadoras else 0,
        "perdida_media": st.mean(perdedoras) if perdedoras else 0,
    }


def _fila(nombre, s, capital=None):
    if not s:
        return f"  {nombre:<26} sin operaciones"
    dinero = ""
    if capital:
        riesgo = capital * C.MARGEN_OP_PCT / 100 * C.SL_PCT_MARGEN / 100
        dinero = f" | {s['r_total_pond']*riesgo:>+7.2f}$"
    return (f"  {nombre:<26} {s['n']:>4} ops | {s['aciertos']:>5.1f}% aciertos | "
            f"R medio {s['r_medio']:>+6.3f} | R total {s['r_total_pond']:>+7.2f}{dinero}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("symbols", nargs="*")
    ap.add_argument("--dias", type=int, default=14)
    ap.add_argument("--paso", type=int, default=5, help="minutos entre evaluaciones")
    ap.add_argument("--capital", type=float, default=C.CAPITAL_TOTAL)
    ap.add_argument("--detalle", action="store_true", help="lista cada operacion")
    a = ap.parse_args()

    symbols = [s.upper() for s in a.symbols] or C.ACTIVOS
    print(f"Bajando y recorriendo {len(symbols)} activos, {a.dias} dias, paso {a.paso}m...\n")
    todas = []
    for s in symbols:
        todas += recoger(s, a.dias, a.paso)

    if not todas:
        print("\nSin candidatas. ¿Historial suficiente?")
        return

    fechas = sorted({c["fecha"] for c in todas})
    print(f"\n{'='*78}")
    print(f"  {len(todas)} candidatas brutas | {len(fechas)} sesiones "
          f"({fechas[0]} a {fechas[-1]}) | {len(symbols)} activos")
    print(f"{'='*78}")

    # --- configuracion actual ---
    actual = aplicar(todas, C.MIN_SCORE, C.MIN_PROB, C.EXIGIR_CONFLUENCIA,
                     C.SINGLE_STRONG_SCORE, C.SEM_VERDE_SCORE, C.SEM_VERDE_PROB)
    s = resumen(actual)
    print(f"\n### CONFIGURACION ACTUAL "
          f"(MIN_SCORE={C.MIN_SCORE}, confluencia={C.EXIGIR_CONFLUENCIA}, "
          f"VERDE>={C.SEM_VERDE_SCORE})")
    print(_fila("total", s, a.capital))
    if s:
        print(f"  {'':<26} VERDE {s['verdes']} / ROJA {s['n']-s['verdes']} | "
              f"SL {s['sl']} · BE {s['be']} · TP3 {s['tp3']} · cierre sesion {s['cierre']}")
        print(f"  {'':<26} ganancia media {s['ganancia_media']:+.2f}R | "
              f"perdida media {s['perdida_media']:+.2f}R | "
              f"mejor {s['mejor']:+.2f}R | peor {s['peor']:+.2f}R")
        print(f"  {'':<26} {s['n']/len(fechas):.1f} operaciones por sesion")

    # --- barrido de MIN_SCORE ---
    print(f"\n### BARRIDO DE MIN_SCORE (resto igual)")
    for ms in (50, 55, 60, 65, 70, 75):
        ops = aplicar(todas, ms, C.MIN_PROB, C.EXIGIR_CONFLUENCIA,
                      C.SINGLE_STRONG_SCORE, C.SEM_VERDE_SCORE, C.SEM_VERDE_PROB)
        print(_fila(f"MIN_SCORE = {ms}", resumen(ops), a.capital))

    # --- confluencia si / no ---
    print(f"\n### EXIGIR CONFLUENCIA")
    for exigir in (True, False):
        ops = aplicar(todas, C.MIN_SCORE, C.MIN_PROB, exigir,
                      C.SINGLE_STRONG_SCORE, C.SEM_VERDE_SCORE, C.SEM_VERDE_PROB)
        print(_fila(f"confluencia = {exigir}", resumen(ops), a.capital))

    # --- corte del semaforo ---
    print(f"\n### CORTE DEL SEMAFORO (VERDE arriesga x1, ROJA x0.30)")
    for vs in (66, 70, 72, 76, 80):
        ops = aplicar(todas, C.MIN_SCORE, C.MIN_PROB, C.EXIGIR_CONFLUENCIA,
                      C.SINGLE_STRONG_SCORE, vs, C.SEM_VERDE_PROB)
        print(_fila(f"SEM_VERDE_SCORE = {vs}", resumen(ops), a.capital))

    # --- por estrategia ---
    print(f"\n### POR ESTRATEGIA (con la configuracion actual)")
    for setup in sorted({o["setup"] for o in actual}):
        print(_fila(setup, resumen([o for o in actual if o["setup"] == setup])))

    # --- verde vs roja ---
    print(f"\n### SEMAFORO")
    for etiqueta, cond in (("VERDE", True), ("ROJA", False)):
        print(_fila(etiqueta, resumen([o for o in actual if o["verde"] == cond])))

    # --- por momento de la sesion ---
    print(f"\n### POR MOMENTO DE LA SESION")
    tramos = [("apertura (15-60m)", 15, 60), ("media (60-180m)", 60, 180),
              ("tarde (180-300m)", 180, 300), ("cierre (300m+)", 300, 999)]
    for nombre, lo, hi in tramos:
        print(_fila(nombre, resumen([o for o in actual if lo <= o["min_sesion"] < hi])))

    # --- alineacion con el 1h ---
    print(f"\n### ALINEACION CON EL 1h")
    for etiqueta, cond in (("alineada con 1h", True), ("no alineada", False)):
        print(_fila(etiqueta, resumen([o for o in actual if o["aligned"] == cond])))

    if a.detalle:
        print(f"\n### OPERACIONES ({len(actual)})")
        for o in sorted(actual, key=lambda x: x["ts"]):
            print(f"  {o['fecha']} {str(o['hora'])[:5]} {o['sym']:<12} "
                  f"{o['side']:<5} score {o['score']:>3} "
                  f"{'VERDE' if o['verde'] else 'ROJA ':<5} {o['setup']:<15} "
                  f"SL {o['sl_pct']:.2f}% -> {o['evento']:<13} {o['r']:>+6.2f}R")

    print(f"\nNota: sin comisiones ni slippage; si una vela contiene TP y SL se "
          f"asume SL. El resultado real sera algo peor.")


if __name__ == "__main__":
    main()
