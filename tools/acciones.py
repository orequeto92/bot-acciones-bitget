# -*- coding: utf-8 -*-
"""
ACCIONES.PY - Motor del Bot de Acciones Tokenizadas (Bitget USDT-M / Stock).

Flujo por activo:
  1) baja klines 5m + 1h de Bitget
  2) FILTRA a horario de sesion (lo que no hacia el bot de cripto y lo rompia todo)
  3) calcula metricas con ta.compute() sobre las velas limpias
  4) calcula rango de apertura (ORB) y gap del dia
  5) corre las 9 estrategias
  6) ENSEMBLE: agrupa por direccion, bonus por confluencia, elige la mejor
  7) filtra por MIN_SCORE / MIN_PROB (umbral dinamico recalibrado a acciones)
  8) SEMAFORO: VERDE (riesgo x1) o ROJA (riesgo x0.30)
  9) GESTION DE DINERO: SL estructural, lev respetando el tope REAL de Bitget,
     TP1/TP2/TP3, y comprobacion de que la posicion supera minimos del exchange

NO EJECUTA ordenes. Solo propone. Tu las colocas a mano en Bitget.

USO:
  python tools/acciones.py                 # escanea config.ACTIVOS
  python tools/acciones.py NVDAUSDT TSLAUSDT
  python tools/acciones.py --capital 100   # cambia el capital TOTAL de la cuenta
  python tools/acciones.py --fuera-de-sesion   # analiza con el mercado cerrado (pruebas)
"""
import sys, os, time, argparse

try:                       # consola Windows: fuerza UTF-8 para los emojis
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import ta, bitget, sesion, estrategias, telegram, seguimiento, ajustes
import config as C


# ----------------------------- cooldown -----------------------------
# COOLDOWN basado en el CICLO DE VIDA de la señal:
#   - se bloquea un activo mientras tenga una posicion ABIERTA
#   - el cooldown arranca cuando la señal CIERRA (SL/TP3/BE), no cuando se emite
def _mapa_bloqueos():
    pos = seguimiento._cargar()
    abiertas = {p["sym"] for p in pos.get("posiciones", []) if p.get("estado") == "activa"}
    ult_cierre = {}
    for p in pos.get("historial", []):
        s, ts = p.get("sym"), p.get("cerrada_ts", 0)
        if s and ts > ult_cierre.get(s, 0):
            ult_cierre[s] = ts
    return abiertas, ult_cierre


def _motivo_bloqueo(sym, abiertas, ult_cierre, ahora):
    if sym in abiertas:
        return "posicion abierta"
    t = ult_cierre.get(sym, 0)
    if t and (ahora - t) < C.COOLDOWN_H * 3600:
        restan = (C.COOLDOWN_H * 3600 - (ahora - t)) / 3600
        return f"cooldown {restan:.1f}h tras cierre"
    return None


# ----------------------------- ensemble -----------------------------
def _ensemble(disparos):
    """De las estrategias que dispararon, agrupa por direccion y devuelve la
    mejor con score combinado + bonus de confluencia."""
    if not disparos:
        return None
    por_lado = {"long": [], "short": []}
    for d in disparos:
        por_lado[d["side"]].append(d)
    mejor_lado, mejores = None, []
    for lado, lst in por_lado.items():
        if lst and (mejor_lado is None or max(x["score"] for x in lst) > max(x["score"] for x in mejores)):
            mejor_lado, mejores = lado, lst
    mejores.sort(key=lambda x: x["score"], reverse=True)
    best = mejores[0]
    n_conf = len(mejores)
    score = min(100, best["score"] + C.ENSEMBLE_BONUS * (n_conf - 1))
    prob = min(0.62, max(x["prob"] for x in mejores) + 0.01 * (n_conf - 1))
    return {"side": mejor_lado, "best": best, "confirmantes": mejores,
            "n_conf": n_conf, "score": round(score), "prob": round(prob, 2)}


# ----------------------------- gestion de dinero -----------------------------
def _lev_tope(sym):
    """El menor entre NUESTRO tope de seguridad y el tope REAL del contrato.

    Bitget fija el maxLever muy distinto segun la accion (NVDA 100x, PLTR 20x,
    TENCENT 5x). Pedir mas del tope hace que el exchange rechace la orden, asi
    que la señal tiene que venir ya con un lev colocable."""
    try:
        real = bitget.max_lev(sym)
    except Exception:
        real = C.lev_max_propio(sym)
    return max(1, min(C.lev_max_propio(sym), real))


def _plan_dinero(sym, side, price, atr, capital, roja, h=None, l=None):
    """SL estructural (swing reciente + colchon ATR), apalancamiento, TPs y
    comprobacion de minimos del exchange."""
    buffer = atr * C.SL_BUFFER_ATR
    look = C.SL_LOOKBACK
    if side == "long":
        swing = min(l[-look:]) if l else price - 2 * atr
        sl = min(swing - buffer, price - C.SL_MIN_ATR * atr)
        dist = price - sl
    else:
        swing = max(h[-look:]) if h else price + 2 * atr
        sl = max(swing + buffer, price + C.SL_MIN_ATR * atr)
        dist = sl - price
    # suelo: por debajo de esto el stop vive dentro del spread
    dist = max(dist, price * C.SL_MIN_PCT / 100.0)
    sl = price - dist if side == "long" else price + dist
    sl_pct = dist / price * 100.0

    # TECHO: si el swing esta demasiado lejos NO se recorta el stop, se rechaza
    # el setup. Un stop recortado queda por delante de la estructura, o sea en
    # zona de ruido: salta y el swing sigue intacto. Es la peor de las opciones.
    if sl_pct > C.SL_MAX_PCT:
        return {"sl": sl, "sl_pct": sl_pct, "lev": 0, "lev_ideal": 0,
                "lev_tope": _lev_tope(sym), "tps": [], "margen": 0.0,
                "margen_base": 0.0, "notional": 0.0, "qty": 0.0, "riesgo_usd": 0.0,
                "riesgo_pct_cuenta": 0.0, "dist": dist, "viable": False,
                "motivo_no": f"SL estructural {sl_pct:.2f}% > techo {C.SL_MAX_PCT}%"}

    # lev ideal para que el SL sea exactamente el 10% del margen (=1R)
    lev_ideal = C.SL_PCT_MARGEN / sl_pct
    lev = max(1, min(round(lev_ideal), _lev_tope(sym)))

    tp_r = list(C.TP_R)
    if roja:
        tp_r = [r for r in tp_r if r <= C.TP_CAP_ROJA_R]
    tps = []
    for r in tp_r:
        tp = price + dist * r if side == "long" else price - dist * r
        tps.append({"r": r, "precio": tp, "pct": abs(tp - price) / price * 100.0})

    factor = C.RIESGO_ROJA if roja else C.RIESGO_VERDE
    margen_base = C.margen_por_op(capital) * factor
    riesgo_objetivo = margen_base * C.SL_PCT_MARGEN / 100.0     # 1R en $

    # Si el tope de apalancamiento nos deja cortos, subimos MARGEN para mantener
    # el riesgo en 1R (hasta el doble del margen base). Si aun asi no llega, se
    # acepta arriesgar menos: nunca al reves.
    margen = margen_base
    if lev < lev_ideal:
        necesario = riesgo_objetivo / (lev * sl_pct / 100.0)
        margen = min(necesario, margen_base * 2)
    notional = margen * lev
    riesgo_usd = notional * sl_pct / 100.0

    # minimos del exchange: por debajo de esto Bitget no acepta la orden
    try:
        min_notional = bitget.min_notional(sym)
        min_qty = bitget.min_qty(sym)
    except Exception:
        min_notional, min_qty = 5.0, 0.01
    qty = notional / price if price else 0.0
    viable, motivo_no = True, ""
    if notional < min_notional:
        viable, motivo_no = False, f"posicion {notional:.2f}$ < minimo {min_notional:g}$"
    elif qty < min_qty:
        viable, motivo_no = False, (f"cantidad {qty:.4f} < minima {min_qty:g} "
                                    f"(hacen falta {min_qty*price:.2f}$)")

    return {"sl": sl, "sl_pct": sl_pct, "lev": lev, "lev_ideal": lev_ideal,
            "lev_tope": _lev_tope(sym), "tps": tps, "margen": margen,
            "margen_base": margen_base, "notional": notional, "qty": qty,
            "riesgo_usd": riesgo_usd,
            "riesgo_pct_cuenta": (riesgo_usd / capital * 100.0) if capital else 0.0,
            "dist": dist, "viable": viable, "motivo_no": motivo_no}


def _umbral_dinamico(atr_pct):
    """Exige mas score cuando hay mucha volatilidad.

    Cortes recalibrados a acciones: una accion en 5m ya filtrada a sesion tiene
    un ATR% de ~0.20-0.45 (NVDA: 0.289). Con los cortes de cripto (0.8-3.0) toda
    accion caeria siempre en 'volatilidad baja' y el umbral se relajaria siempre."""
    if C.THRESHOLD_MODE != "DYNAMIC" or atr_pct is None:
        return C.MIN_SCORE
    if atr_pct > C.ATR_PCT_ALTO:
        return C.MIN_SCORE + 6
    if atr_pct < C.ATR_PCT_BAJO:
        return C.MIN_SCORE - 4
    return C.MIN_SCORE


# ----------------------------- analisis de un activo -----------------------------
def analizar_con_velas(sym, capital, k5, kh, utc=None, ignorar_sesion=False):
    """Version PURA (sin descarga): recibe las velas crudas y hace todo lo demas,
    incluido el filtrado a sesion. Asi el analisis es reproducible en pruebas."""
    cal = C.calendario_de(sym)
    est = sesion.estado(cal, utc, orb_min=C.ORB_MINUTOS,
                        aviso_cierre_min=C.NO_ABRIR_ULTIMOS_MIN)

    if not est["abierto"] and not ignorar_sesion:
        return {"sym": sym, "cerrado": True, "sesion": est}

    # --- EL PASO CRITICO: fuera las velas de fuera de sesion ---
    k5r = sesion.filtrar_rth(k5 or [], cal)
    khr = sesion.filtrar_rth(kh or [], cal)
    if len(k5r) < 60:
        return {"sym": sym, "error": f"pocas velas en sesion ({len(k5r)})"}

    m5 = ta.compute(sym, C.TF_SCAN, k5r)
    mh = ta.compute(sym, C.TF_HTF, khr) if len(khr) >= 30 else {}
    ctx = {"m5": m5, "mh": mh, "price": m5["price"],
           "h": [float(c[2]) for c in k5r], "l": [float(c[3]) for c in k5r],
           "c": [float(c[4]) for c in k5r], "v": [float(c[5]) for c in k5r],
           "sesion": est,
           "orb": sesion.rango_apertura(k5r, cal, C.ORB_MINUTOS, utc) if est["abierto"] else None,
           "gap": sesion.gap_apertura(k5r, cal, utc)}

    disparos = estrategias.evaluar_todas(ctx)
    ens = _ensemble(disparos)
    if not ens:
        return {"sym": sym, "sin_setup": True, "m5": m5, "sesion": est, "ctx": ctx}

    # --- HTF: premia alinearse con el 1h, penaliza ir en contra ---
    aligned = estrategias._htf_aligned(ens["side"], mh)
    htf_bias = (mh or {}).get("bias", "neutral")
    opposed = ((ens["side"] == "long" and htf_bias == "bajista") or
               (ens["side"] == "short" and htf_bias == "alcista"))
    if aligned:
        ens["score"] = min(100, ens["score"] + C.HTF_BONUS)
    elif opposed:
        ens["score"] = max(0, ens["score"] - C.HTF_PENALTY)
    ens["prob"] = min(0.62, round(estrategias._prob(ens["score"]) + 0.01 * (ens["n_conf"] - 1), 2))

    # --- puerta de selectividad ---
    if C.EXIGIR_CONFLUENCIA and ens["n_conf"] < 2:
        if not (ens["best"]["score"] >= C.SINGLE_STRONG_SCORE and aligned):
            return {"sym": sym, "descartada": True, "ens": ens, "sesion": est,
                    "min_score": C.SINGLE_STRONG_SCORE, "motivo": "sin confluencia", "m5": m5}

    min_score = _umbral_dinamico(m5.get("atr_pct"))
    if ens["score"] < min_score or ens["prob"] < C.MIN_PROB:
        return {"sym": sym, "descartada": True, "ens": ens, "sesion": est,
                "min_score": min_score, "m5": m5}

    # --- puerta de sesion: no se abre nada pegado al cierre ---
    if est["fase"] == "cierre_cercano" and not ignorar_sesion:
        return {"sym": sym, "descartada": True, "ens": ens, "sesion": est,
                "min_score": min_score, "m5": m5,
                "motivo": f"quedan {est['para_cierre']:.0f} min para el cierre"}

    verde = ens["score"] >= C.SEM_VERDE_SCORE and ens["prob"] >= C.SEM_VERDE_PROB and aligned
    roja = not verde
    plan = _plan_dinero(sym, ens["side"], m5["price"], m5["atr"] or m5["price"] * 0.003,
                        capital, roja, h=ctx["h"], l=ctx["l"])

    if not plan["viable"]:
        return {"sym": sym, "descartada": True, "ens": ens, "sesion": est,
                "min_score": min_score, "m5": m5,
                "motivo": f"no operable: {plan['motivo_no']}"}

    return {"sym": sym, "ens": ens, "m5": m5, "mh": mh, "aligned": aligned,
            "verde": verde, "plan": plan, "capital": capital, "sesion": est,
            "orb": ctx["orb"], "gap": ctx["gap"]}


def analizar(sym, capital, ignorar_sesion=False):
    """Wrapper: trae las velas de Bitget y delega en analizar_con_velas()."""
    try:
        k5 = bitget.klines(sym, C.TF_SCAN, C.KLINES_LIMIT)
        kh = bitget.klines(sym, C.TF_HTF, C.KLINES_LIMIT_HTF)
    except Exception as e:
        return {"sym": sym, "error": str(e)}
    return analizar_con_velas(sym, capital, k5, kh, ignorar_sesion=ignorar_sesion)


def _parciales_para(n_tps):
    """Reparte el cierre en los TP disponibles, sumando siempre 100%."""
    base = list(C.TP_PARCIAL)
    if n_tps >= len(base):
        return base[:n_tps]
    partes = base[:n_tps - 1]
    partes.append(round(1.0 - sum(partes), 4))
    return partes


# ----------------------------- formato de señal -----------------------------
def formatear(r):
    ens, m5, plan = r["ens"], r["m5"], r["plan"]
    sym, side = r["sym"], ens["side"].upper()
    emoji = "🟢" if r["verde"] else "🔴"
    sem = "🟢 VERDE" if r["verde"] else "🔴 ROJA"
    rx = C.RIESGO_VERDE if r["verde"] else C.RIESGO_ROJA
    price = m5["price"]
    best = ens["best"]
    est = r["sesion"]
    dec = 4 if price < 10 else 2

    L = []
    L.append(f"{emoji} {sym} | {side} (Bitget USDT-M / Stock)")
    L.append("")
    L.append(f"Entrada: {price:.{dec}f}")
    L.append(f"SL: {plan['sl']:.{dec}f} | SL%: {plan['sl_pct']:.2f}%")
    L.append(f"Lev sugerido: {plan['lev']}x (tope del contrato {plan['lev_tope']}x)")
    L.append(f"Margen a usar: {plan['margen']:.2f}$ (de {r['capital']:.0f}$ cuenta) | "
             f"Posicion: {plan['notional']:.2f}$ | Cantidad: {plan['qty']:.4f}")
    L.append("")
    parciales = _parciales_para(len(plan["tps"]))
    for i, (tp, pct_cierre) in enumerate(zip(plan["tps"], parciales), 1):
        L.append(f"TP{i}: {tp['precio']:.{dec}f} | {tp['pct']:.2f}% | "
                 f"RR {tp['r']:.2f} | cierra {int(pct_cierre*100)}%")
    L.append("")
    L.append(f"Score: {ens['score']}/100 | Prob: {ens['prob']:.2f}")
    L.append(f"Setup: {best['name']}")
    L.append(f"Grupo: {C.grupo_de(sym)}")
    L.append(f"Semaforo: {sem} | Riesgo: x{rx:.2f} | "
             f"Arriesgas {plan['riesgo_usd']:.2f}$ ({plan['riesgo_pct_cuenta']:.1f}% cuenta)")
    L.append("")
    # contexto de sesion: lo que NO tenia el bot de cripto
    if est["abierto"]:
        L.append(f"Sesion: {est['et'].strftime('%H:%M')} ET | lleva {est['desde_apertura']:.0f} min "
                 f"| cierra en {est['para_cierre']:.0f} min"
                 + ("  ⚠️ MEDIA SESION" if est.get("media_sesion") else ""))
    else:
        L.append(f"⚠️ Sesion: MERCADO CERRADO ({est['motivo']}) — analisis fuera de sesion, "
                 f"NO operable: sin ORB ni gap vivos y el precio no se mueve.")
    if r.get("orb"):
        o = r["orb"]
        L.append(f"Rango apertura ({C.ORB_MINUTOS}m): {o['bajo']:.{dec}f} - {o['alto']:.{dec}f}")
    if r.get("gap"):
        g = r["gap"]
        L.append(f"Gap de hoy: {g['pct']:+.2f}% ({g['direccion']}) | cierre previo "
                 f"{g['cierre_previo']:.{dec}f}" + (" | ya rellenado" if g["cerrado"] else ""))
    L.append("")
    partes = " | ".join(f"{d['name']}:{d['prob']:.2f}/{d['score']}" for d in ens["confirmantes"])
    aligned_txt = f" HTF aligned +{C.HTF_BONUS}" if r["aligned"] else ""
    L.append(f"Motivo: [ENSEMBLE {ens['n_conf']}x] {partes} || Best={best['name']}: "
             f"{' + '.join(best['reasons'])}{aligned_txt}")
    if not C.PERMITIR_OVERNIGHT and est.get("cierre"):
        L.append(f"⏰ CERRAR antes de las {est['cierre'].strftime('%H:%M')} ET "
                 f"(no se duerme la posicion: el gap de mañana no se puede gestionar)")
    return "\n".join(L)


def cabecera(utc=None):
    est = sesion.estado("US_EQUITY", utc, orb_min=C.ORB_MINUTOS,
                        aviso_cierre_min=C.NO_ABRIR_ULTIMOS_MIN)
    if est["abierto"]:
        linea = (f"🔔 Mercado ABIERTO | {est['et'].strftime('%a %H:%M')} ET | "
                 f"fase: {est['fase']} | cierra en {est['para_cierre']:.0f} min")
    else:
        linea = (f"🌙 Mercado CERRADO | {est['et'].strftime('%a %H:%M')} ET | {est['motivo']}")
    return (f"🤖 {C.BOT_NOMBRE} iniciado.\n"
            f"{linea}\n"
            f"Activos: {len(C.ACTIVOS)} ({len(C.ACCIONES_US)} acciones US + "
            f"{len(C.MATERIAS_PRIMAS)} materias primas)\n"
            f"Estrategias: {', '.join(C.ESTRATEGIAS)}\n"
            f"Live source: {C.LIVE_SOURCE} | Ejecucion: {C.EXEC_EXCHANGE} (manual)\n"
            f"Escaneo: {C.TF_SCAN} (solo velas en sesion) | HTF: {C.TF_HTF}\n"
            f"MIN_SCORE={C.MIN_SCORE} | MIN_PROB={C.MIN_PROB} | ORB={C.ORB_MINUTOS}m\n"
            f"Multi: max {C.MAX_TRADES_SIMULTANEOS} trades | cooldown {C.COOLDOWN_H}h | "
            f"overnight: {'SI' if C.PERMITIR_OVERNIGHT else 'NO'}")


# ----------------------------- escaneo -----------------------------
def escanear(activos, capital, usar_cooldown=True, verbose=True, ignorar_sesion=False):
    abiertas, ult_cierre = _mapa_bloqueos()
    ahora = time.time()
    senales, descartes, cerrados = [], 0, 0
    for sym in activos:
        if usar_cooldown:
            bloqueo = _motivo_bloqueo(sym, abiertas, ult_cierre, ahora)
            if bloqueo:
                if verbose: print(f"  · {sym:<12} en cooldown ({bloqueo})")
                continue
        r = analizar(sym, capital, ignorar_sesion=ignorar_sesion)
        if verbose:
            print(f"  · {sym:<12} ", end="")
        if r.get("cerrado"):
            cerrados += 1
            if verbose: print(f"mercado cerrado ({r['sesion']['motivo']})")
            continue
        if r.get("error"):
            if verbose: print(f"error: {r['error']}")
            continue
        if r.get("sin_setup"):
            if verbose: print("sin setup")
            continue
        if r.get("descartada"):
            descartes += 1
            if verbose:
                mot = r.get("motivo") or f"score {r['ens']['score']}<{r['min_score']} o prob baja"
                print(f"descartada ({mot})")
            continue
        senales.append(r)
        if verbose:
            print(f"SEÑAL {r['ens']['side'].upper()} score {r['ens']['score']} "
                  f"{'VERDE' if r['verde'] else 'ROJA'}")
        time.sleep(0.12)   # cortesia con la API
    senales.sort(key=lambda r: (r["verde"], r["ens"]["score"]), reverse=True)
    return senales[:C.MAX_TRADES_SIMULTANEOS], senales, descartes, cerrados


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("symbols", nargs="*", help="simbolos concretos (ej: NVDAUSDT)")
    ap.add_argument("--capital", type=float, default=None,
                    help="capital TOTAL de la cuenta en $ (por defecto: el de Telegram o config)")
    ap.add_argument("--no-cooldown", action="store_true")
    ap.add_argument("--todas", action="store_true", help="muestra todas las señales")
    ap.add_argument("--telegram", action="store_true")
    ap.add_argument("--registrar", action="store_true",
                    help="registra las señales para el seguimiento 1m")
    ap.add_argument("--force", action="store_true", help="escanea aunque el bot este en PAUSA")
    ap.add_argument("--fuera-de-sesion", action="store_true", dest="fuera",
                    help="analiza con el mercado cerrado (SOLO pruebas: sin ORB ni gap vivos)")
    a = ap.parse_args()

    capital = a.capital if a.capital is not None else ajustes.capital(C.CAPITAL_TOTAL)

    if not ajustes.get("activo", True) and not a.force and not a.symbols:
        print("⏸️  Bot en PAUSA (/activar en Telegram, o --force). No se escanea.")
        return

    activos = [s.upper() for s in a.symbols] if a.symbols else C.ACTIVOS

    print(cabecera())
    print(f"\nEscaneando {len(activos)} activos (cuenta total ${capital:g}; "
          f"margen {C.MARGEN_OP_PCT:.0f}%/op = ${C.margen_por_op(capital):.2f}; "
          f"riesgo 1R = ${C.margen_por_op(capital)*C.SL_PCT_MARGEN/100:.2f})...\n")
    top, todas, descartes, cerrados = escanear(
        activos, capital, usar_cooldown=not a.no_cooldown, ignorar_sesion=a.fuera)

    emitir = todas if a.todas else top
    print("\n" + "=" * 62)
    print(f"  {len(todas)} señal(es) validas | mostrando {len(emitir)} | "
          f"{descartes} descartadas" + (f" | {cerrados} con mercado cerrado" if cerrados else ""))
    print("=" * 62)
    for r in emitir:
        print("\n" + formatear(r))
    if not emitir:
        if cerrados == len(activos):
            print("\n  Mercado cerrado. El bot solo opera en sesion "
                  "(9:30-16:00 ET de lunes a viernes).")
        else:
            print("\n  Sin señales que superen el umbral ahora mismo.")

    # Si se pidio Telegram y el envio falla, se sale con error. Callarse una
    # señal y devolver "success" deja el bot mudo sin que te enteres.
    fallo_envio = False
    if a.telegram:
        if not telegram.configurado():
            print("\n  [telegram] SIN CREDENCIALES: no se envia nada.")
            fallo_envio = True
        elif emitir:
            ok = sum(1 for r in emitir if telegram.enviar(formatear(r)))
            print(f"\n  [telegram] enviadas {ok}/{len(emitir)} señales.")
            if ok < len(emitir):
                print(f"  [telegram] FALLARON {len(emitir)-ok} envios.")
                fallo_envio = True
        else:
            print("\n  [telegram] sin señales que enviar.")

    if a.registrar and emitir:
        n = seguimiento.registrar(emitir)
        print(f"\n  [seguimiento] {n} posicion(es) registrada(s) para vigilar.")

    return 1 if fallo_envio else 0


if __name__ == "__main__":
    sys.exit(main())
