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
- Comisiones incluidas (--comision), y DESLIZAMIENTO del SL (--deslizamiento):
  el stop es una orden stop-market, se dispara al tocar el nivel y se ejecuta al
  precio que haya, que siempre es peor. Medido en real: 0.029R.
- Lo que sigue SIN modelarse: el spread real del libro. El backtest usa el
  precio de las velas.

USO:
  python tools/backtest.py                       # universo entero, 14 dias
  python tools/backtest.py --dias 21 --paso 5
  python tools/backtest.py NVDAUSDT TSLAUSDT --detalle
"""
import sys, os, argparse, bisect, datetime as dt, statistics as st

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
def simular(futuras, side, entrada, dist, cierre_sesion_et, cal, deslizamiento=0.0):
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
            # DESLIZAMIENTO. El SL es una orden stop-MARKET: se dispara al tocar
            # el nivel y se ejecuta al precio que haya, que siempre es peor.
            # Medido en la operacion real #3 (AAPL, 4-ago-2026): stop en 309.80,
            # ejecutado en 309.87 -> 0.029R de hueco. El backtest asumia que el
            # SL se ejecuta exactamente en su nivel, que no pasa nunca.
            salida_r = (0.0 if be else -1.0) - deslizamiento
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
def buscar_fill(futuras, side, limite, espera):
    """¿Se habria llenado una orden LIMITE a ese precio, y cuando?

    Esta es LA pregunta que decide el sistema. Entrar a limite baja la comision
    de 0.174R a 0.058R por operacion, pero solo si la orden se llena. Y no se
    llena siempre: cuando el precio arranca y no vuelve, te quedas fuera... y
    esas suelen ser justo las ganadoras. A eso se le llama seleccion adversa, y
    es lo que puede convertir un +47% teorico en mucho menos.

    Devuelve el indice de la vela donde se llena, o None si no se lleno en
    `espera` velas (entonces la operacion no existe: no la has cogido).
    """
    for k, v in enumerate(futuras[:espera]):
        if side == "long" and float(v[3]) <= limite:
            return k
        if side == "short" and float(v[2]) >= limite:
            return k
    return None


def recoger(sym, dias, paso, verbose=True, limite_bps=0.0, espera=6, usar_limite=True,
            deslizamiento=0.0):
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

    # Se indexa por timestamp para recortar el futuro con bisect. Antes cada
    # paso hacia [v for v in k5 if int(v[0]) <= lim], o sea recorrer las ~26.000
    # velas de 90 dias unas 79.000 veces: 2.000 millones de operaciones.
    #
    # La VENTANA es exactamente la de produccion: acciones.analizar() pide
    # C.KLINES_LIMIT velas CRUDAS y deja que el filtro RTH las reduzca. Hay que
    # darle lo mismo, porque el tamaño de la ventana cambia los indicadores (la
    # EMA200 calienta distinto, los pivotes salen de otra serie) y calibrar con
    # 3.300 velas para luego operar con 234 seria calibrar otro sistema.
    ts5 = [int(v[0]) for v in k5]          # crudas, como en produccion
    tsh = [int(v[0]) for v in kh]
    k5r = sesion.filtrar_rth(k5, cal)      # solo para simular el resultado
    ts5r = [int(v[0]) for v in k5r]
    grupos = sesion.agrupar_por_sesion(k5, cal)

    # Umbrales al minimo durante la recogida: filtrar viene despues, en memoria.
    # OPERAR_ROJAS incluido, porque si el motor descarta las rojas antes de que
    # las veamos, la herramienta de calibracion ya no puede compararlas y se
    # vuelve ciega justo a la decision que la config acaba de tomar.
    guardado = (C.MIN_SCORE, C.MIN_PROB, C.EXIGIR_CONFLUENCIA, C.THRESHOLD_MODE,
                C.OPERAR_ROJAS)
    C.MIN_SCORE, C.MIN_PROB = 0, 0.0
    C.EXIGIR_CONFLUENCIA, C.THRESHOLD_MODE = False, "FIJO"
    C.OPERAR_ROJAS = True

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
                i5 = bisect.bisect_right(ts5, lim)
                ih = bisect.bisect_right(tsh, lim)
                v5 = k5[max(0, i5 - C.KLINES_LIMIT):i5]
                vh = kh[max(0, ih - C.KLINES_LIMIT_HTF):ih]
                r = acciones.analizar_con_velas(sym, C.CAPITAL_TOTAL, v5, vh, utc=utc)
                if r.get("ens") and r.get("plan") and r["plan"].get("viable"):
                    ens, plan = r["ens"], r["plan"]
                    # SOLO las velas que quedan de HOY. Si se cogen todas las
                    # futuras, una entrada pegada al cierre se queda sin velas
                    # antes de la campana y el precio de salida acaba siendo el
                    # de semanas despues -> perdidas fantasma de -44R.
                    cierre_ms = int(sesion.et_a_utc(cierra)
                                    .replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
                    i5r = bisect.bisect_right(ts5r, lim)
                    futuras = k5r[i5r:bisect.bisect_left(ts5r, cierre_ms)]
                    if futuras:
                        precio = r["m5"]["price"]
                        # --- entrada a LIMITE (o a mercado si usar_limite=False) ---
                        if usar_limite:
                            signo = -1 if ens["side"] == "long" else 1
                            limite = precio * (1 + signo * limite_bps / 10000.0)
                            k_fill = buscar_fill(futuras, ens["side"], limite, espera)
                            if k_fill is None:
                                # no se lleno: la operacion NO existe. Se guarda
                                # igual para poder medir cuantas te pierdes y si
                                # eran mejores o peores que las que si entran.
                                res_perdida = simular(futuras, ens["side"], precio,
                                                      plan["dist"], cierra, cal, deslizamiento)
                                candidatas.append({
                                    "sym": sym, "fecha": fecha, "hora": t.time(),
                                    "min_sesion": (t - abre).total_seconds() / 60.0,
                                    "side": ens["side"], "score": ens["score"],
                                    "prob": ens["prob"], "n_conf": ens["n_conf"],
                                    "setup": ens["best"]["name"],
                                    "aligned": r["aligned"], "sl_pct": plan["sl_pct"],
                                    "atr_pct": r["m5"].get("atr_pct"), "ts": lim,
                                    "lleno": False, **res_perdida})
                                t += dt.timedelta(minutes=paso)
                                continue
                            entrada = limite
                            futuras = futuras[k_fill + 1:]   # simula DESDE la vela siguiente
                            if not futuras:
                                t += dt.timedelta(minutes=paso)
                                continue
                        else:
                            entrada = precio
                        res = simular(futuras, ens["side"], entrada,
                                      plan["dist"], cierra, cal, deslizamiento)
                        candidatas.append({"lleno": True,
                            "sym": sym, "fecha": fecha, "hora": t.time(),
                            "min_sesion": (t - abre).total_seconds() / 60.0,
                            "side": ens["side"], "score": ens["score"],
                            "prob": ens["prob"], "n_conf": ens["n_conf"],
                            "setup": ens["best"]["name"],
                            "aligned": r["aligned"], "sl_pct": plan["sl_pct"],
                            "atr_pct": r["m5"].get("atr_pct"), "ts": lim, **res})
                t += dt.timedelta(minutes=paso)
    finally:
        (C.MIN_SCORE, C.MIN_PROB, C.EXIGIR_CONFLUENCIA, C.THRESHOLD_MODE,
         C.OPERAR_ROJAS) = guardado

    if verbose:
        print(f"  {sym:<12} {len(grupos):>2} sesiones, {len(candidatas):>4} candidatas")
    return candidatas


# ------------------------------------------------------------------
# APLICAR UMBRALES Y CONTAR
# ------------------------------------------------------------------
def coste_r(sl_pct, comision_pct):
    """Comision de ida y vuelta expresada en R.

    El riesgo 1R = notional * SL%. La comision = notional * comision% * 2 (entrar
    y salir; los cierres parciales suman el 100% de la posicion, asi que cuentan
    como una salida). Al dividir, el notional se va:
        coste_R = 2 * comision% / SL%
    Con el taker de Bitget (0.06%) y un SL del 1.25% son 0.096R por operacion:
    casi el 10% de lo que arriesgas, en cada trade. Con 600 operaciones eso
    decide si el sistema gana o no.
    """
    return (2.0 * comision_pct / sl_pct) if sl_pct else 0.0


def aplicar(candidatas, min_score, min_prob, exigir_conf, single_strong,
            verde_score, verde_prob, max_por_activo_dia=99,
            solo_verde=False, comision_pct=0.0):
    """Filtra las candidatas con un juego de umbrales y resuelve solapes:
    una sola operacion viva por activo (la siguiente no entra hasta que la
    anterior cerraria por cooldown)."""
    pasan = []
    for c in candidatas:
        if not c.get("lleno", True):
            continue          # la orden limite no se lleno: no hay operacion
        if c["score"] < min_score or c["prob"] < min_prob:
            continue
        if exigir_conf and c["n_conf"] < 2:
            if not (c["score"] >= single_strong and c["aligned"]):
                continue
        verde = c["score"] >= verde_score and c["prob"] >= verde_prob and c["aligned"]
        if solo_verde and not verde:
            continue
        bruto = c["r_verde"] if verde else c["r_roja"]
        pasan.append({**c, "verde": verde,
                      "r_bruto": bruto,
                      "coste": coste_r(c["sl_pct"], comision_pct),
                      "r": bruto - coste_r(c["sl_pct"], comision_pct),
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
        # TOPE POR SECTOR: seis cortos de semis a la vez no son seis apuestas,
        # son una sola multiplicada por seis (ver config.MAX_POR_GRUPO).
        if sum(1 for _, s in abiertas if C.grupo_de(s) == C.grupo_de(c["sym"]))                 >= C.MAX_POR_GRUPO:
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
    ap.add_argument("--comision", type=float, default=0.06,
                    help="comision por lado en %% (0.06 = taker de Bitget; 0.02 = maker)")
    ap.add_argument("--mercado", action="store_true",
                    help="entrar a mercado (por defecto se simula orden LIMITE)")
    ap.add_argument("--limite-bps", type=float, default=0.0, dest="limite_bps",
                    help="cuanto mejor que el precio de señal se pone el limite, en bps")
    ap.add_argument("--espera", type=int, default=6,
                    help="velas de 5m que se espera a que la limite se llene (6 = 30 min)")
    ap.add_argument("--deslizamiento", type=float, default=0.03,
                    help="cuanto peor se ejecuta el SL, en R (medido real: 0.029)")
    ap.add_argument("--tp-unico", action="store_true", dest="tp_unico",
                    help="cierra el 100%% en TP1 en vez de escalonar 33/33/34")
    ap.add_argument("--tf", default=None,
                    help="marco temporal de escaneo (5m, 15m, 30m, 1h). Subirlo hace "
                         "los movimientos mas grandes y diluye la comision")
    ap.add_argument("--sl-min", type=float, default=None, dest="sl_min",
                    help="sobreescribe C.SL_MIN_PCT: el coste en R es 2*comision/SL, "
                         "asi que ensanchar el stop diluye la comision")
    a = ap.parse_args()

    if a.sl_min is not None:
        C.SL_MIN_PCT = a.sl_min
        print(f"[config] SL_MIN_PCT sobreescrito a {a.sl_min}%")
    if a.tp_unico:
        # Cerrar el 100% en TP1 en vez de escalonar 33/33/34. Es lo que sale
        # solo cuando uno cierra "por si acaso" en cuanto va ganando, y conviene
        # saber cuanto cuesta: con 53% de aciertos, si la ganadora vale 1R y la
        # perdedora -1R, la esperanza es +0.06R y las comisiones se la comen.
        C.TP_R = [1.0]
        C.TP_PARCIAL = [1.0]
        print("[config] cerrando el 100% en TP1 (sin escalera 33/33/34)")
    if a.tf:
        # Subir el marco temporal es la via que ataca la raiz del problema: el
        # coste es 2*comision/SL, y en 15m o 30m los movimientos (y por tanto
        # los stops estructurales) son mayores, asi que la comision pesa menos.
        C.TF_SCAN = a.tf
        print(f"[config] TF_SCAN sobreescrito a {a.tf}")

    symbols = [s.upper() for s in a.symbols] or C.ACTIVOS
    modo = ("orden LIMITE" if not a.mercado else "orden a MERCADO")
    print(f"Bajando y recorriendo {len(symbols)} activos, {a.dias} dias, paso {a.paso}m")
    print(f"Entrada: {modo}"
          + (f" ({a.limite_bps:g} bps, hasta {a.espera} velas de espera)"
             if not a.mercado else "") + "\n")
    todas = []
    for s in symbols:
        todas += recoger(s, a.dias, a.paso, limite_bps=a.limite_bps,
                         espera=a.espera, usar_limite=not a.mercado,
                         deslizamiento=a.deslizamiento)

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

    # --- LLENADO DE LA ORDEN LIMITE: la pregunta que decide el sistema ---
    if not a.mercado:
        # Se compara solo entre candidatas que habrian pasado el filtro VERDE,
        # que son las que de verdad se operan.
        elegibles = [c for c in todas
                     if c["score"] >= C.SEM_VERDE_SCORE and c["prob"] >= C.SEM_VERDE_PROB
                     and c["aligned"]]
        llenas = [c for c in elegibles if c.get("lleno")]
        perdidas = [c for c in elegibles if not c.get("lleno")]
        print(f"\n### LLENADO DE LA ORDEN LIMITE "
              f"({a.limite_bps:g} bps, {a.espera} velas de espera)")
        if elegibles:
            tasa = len(llenas) / len(elegibles) * 100
            print(f"  señales VERDE elegibles : {len(elegibles)}")
            print(f"  se habrian llenado      : {len(llenas)} ({tasa:.1f}%)")
            print(f"  te habrias perdido      : {len(perdidas)} ({100-tasa:.1f}%)")
            if llenas and perdidas:
                r_ll = st.mean([c["r_verde"] for c in llenas])
                r_pe = st.mean([c["r_verde"] for c in perdidas])
                print(f"\n  R medio de las que SI se llenan : {r_ll:+.3f}")
                print(f"  R medio de las que te PIERDES   : {r_pe:+.3f}")
                if r_pe > r_ll:
                    print(f"  -> SELECCION ADVERSA: las que se escapan eran MEJORES")
                    print(f"     ({r_pe - r_ll:+.3f}R de diferencia). Es el coste oculto")
                    print(f"     de entrar a limite, y no aparece en la comision.")
                else:
                    print(f"  -> Sin seleccion adversa: las que se escapan no eran")
                    print(f"     mejores ({r_pe - r_ll:+.3f}R). Entrar a limite sale gratis.")

    # --- barrido combinado: lo que las tablas de una sola variable no ven ---
    # Las tablas anteriores mueven una palanca dejando el resto fijo, asi que no
    # pueden responder "¿y si ademas descarto las ROJAS?". Aqui se cruzan las dos
    # decisiones y se restan las comisiones, que es lo unico que importa.
    print(f"\n### COMBINADO, NETO DE COMISIONES ({a.comision}% por lado, taker de Bitget)")
    print(f"  {'config':<34} {'ops':>5} {'aciertos':>9} {'R bruto':>9} {'comis':>8} "
          f"{'R NETO':>9} {'$':>8} {'t':>6} {'signif':>7}")
    riesgo_op = a.capital * C.MARGEN_OP_PCT / 100 * C.SL_PCT_MARGEN / 100
    mejor = None
    for solo_v in (False, True):
        for ms in (60, 65, 70, 75):
            ops = aplicar(todas, ms, C.MIN_PROB, C.EXIGIR_CONFLUENCIA,
                          C.SINGLE_STRONG_SCORE, C.SEM_VERDE_SCORE, C.SEM_VERDE_PROB,
                          solo_verde=solo_v, comision_pct=a.comision)
            if not ops:
                continue
            bruto = sum(o["r_bruto"] * o["peso"] for o in ops)
            comis = sum(o["coste"] * o["peso"] for o in ops)
            neto = bruto - comis
            acier = sum(1 for o in ops if o["r"] > 0.01) / len(ops) * 100
            # ¿ES DISTINGUIBLE DE CERO? Sin esto es facil leer "+8.28R" como una
            # ventaja cuando puede ser ruido. t = media / error estandar; con
            # |t| < 2 el resultado NO se distingue de cero por mucho que el
            # total parezca bonito.
            netos = [(o["r_bruto"] - o["coste"]) * o["peso"] for o in ops]
            media = st.mean(netos)
            desv = st.stdev(netos) if len(netos) > 1 else 0.0
            t = media / (desv / (len(netos) ** 0.5)) if desv else 0.0
            signif = "SI" if abs(t) >= 2 else "no"
            etiq = f"{'solo VERDE' if solo_v else 'VERDE+ROJA'}, MIN_SCORE {ms}"
            print(f"  {etiq:<34} {len(ops):>5} {acier:>8.1f}% {bruto:>+9.2f} "
                  f"{-comis:>8.2f} {neto:>+9.2f} {neto*riesgo_op:>+8.2f} "
                  f"{t:>+6.2f} {signif:>7}")
            if mejor is None or neto > mejor[0]:
                mejor = (neto, etiq, len(ops))
    if mejor:
        print(f"\n  Mejor combinacion: {mejor[1]}  ->  {mejor[0]:+.2f}R netos "
              f"({mejor[0]*riesgo_op:+.2f}$) con {mejor[2]} operaciones")
        print(f"  Sobre {len(fechas)} sesiones: {mejor[2]/len(fechas):.1f} ops/sesion, "
              f"{mejor[0]*riesgo_op/a.capital*100:+.1f}% de la cuenta")

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
