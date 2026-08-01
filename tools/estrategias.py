# -*- coding: utf-8 -*-
"""
ESTRATEGIAS.PY - Las 9 estrategias del Bot de Acciones.

Cada estrategia recibe un contexto y devuelve None (no dispara) o un dict:
    {"name","side","score","prob","reasons"}
  - side  : "long" | "short"
  - score : 0-100 (calidad bruta del setup)
  - prob  : 0-1   (prob. estimada de exito, heuristica)
  - reasons: lista corta de motivos (para el campo "Motivo" de la señal)

El contexto ctx trae:
    ctx["m5"]  = ta.compute() del TF de escaneo (5m), SOLO velas de sesion
    ctx["mh"]  = ta.compute() del TF superior (1h)  -> para "HTF aligned"
    ctx["h"], ctx["l"], ctx["c"], ctx["v"] = listas OHLCV del 5m (en sesion)
    ctx["price"]  = ultimo cierre
    ctx["sesion"] = sesion.estado(): fase, minutos desde apertura / para cierre
    ctx["orb"]    = sesion.rango_apertura(): alto/bajo de los primeros minutos
    ctx["gap"]    = sesion.gap_apertura(): % contra el cierre de ayer

Las dos primeras (ORB y GAP_FADE) son propias de acciones y no existian en el
motor de cripto: nacen de que el mercado ABRE, cosa que el cripto no hace. Las
otras siete vienen del Bot Portero y funcionan igual, pero alimentadas con
velas ya filtradas a horario de sesion.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config as C

# --------------------------- helpers ---------------------------
def _rango(h, l, look=50):
    """Maximo/minimo del rango reciente (para premium/discount y EQH/EQL)."""
    hh = max(h[-look:]); ll = min(l[-look:])
    return hh, ll

def _pos_en_rango(price, hh, ll):
    """0 = suelo (discount extremo), 1 = techo (premium extremo)."""
    if hh <= ll:
        return 0.5
    return (price - ll) / (hh - ll)

def _htf_aligned(side, mh):
    """+1 si el sesgo del TF superior acompaña la direccion."""
    b = (mh or {}).get("bias", "neutral")
    if side == "long" and b == "alcista":
        return True
    if side == "short" and b == "bajista":
        return True
    return False

def _prob(score, extra=0.0):
    """Mapea score(0-100) -> prob(0-1) con techo realista (~0.62, como en el bot).
    Con la calibracion selectiva, un disparo suelto (~45) queda en prob ~0.43 y
    solo la confluencia + HTF lo llevan a 0.50-0.58 (rango de las señales reales)."""
    p = 0.28 + (score / 100.0) * 0.30 + extra
    return max(0.33, min(0.62, p))


# --------------------------- 0a) ORB (rango de apertura) ---------------------------
def orb(ctx):
    """OPENING RANGE BREAKOUT. La estrategia de intradia en acciones.

    Los primeros minutos de sesion son una subasta: se cruzan todas las ordenes
    que se acumularon durante la noche y el precio va y viene sin direccion. En
    vez de adivinar, se deja que se forme el rango (alto/bajo de los primeros
    ORB_MINUTOS) y se opera su ROTURA, que es cuando aparece el ganador del dia.

    Filtros que la hacen selectiva:
      - rango demasiado estrecho (<0.15%) -> la accion esta muerta, cualquier
        tick lo rompe y son todo roturas falsas
      - rotura ya muy extendida (>1 rango) -> llegamos tarde, el movimiento ya se hizo
      - caducidad: pasadas ORB_VALIDEZ_MIN el rango de apertura ya no manda
    """
    o = ctx.get("orb")
    ses = ctx.get("sesion") or {}
    if not o:
        return None
    desde = ses.get("desde_apertura")
    if desde is None or desde > C.ORB_VALIDEZ_MIN:
        return None

    price = ctx["price"]
    alto, bajo = o["alto"], o["bajo"]
    rango = alto - bajo
    if rango <= 0:
        return None
    rango_pct = rango / price * 100.0
    if rango_pct < 0.15:
        return None                       # apertura plana: roturas falsas garantizadas

    m5 = ctx["m5"]
    reasons, side, score = [], None, 0
    if price > alto:
        side = "long"; ext = (price - alto) / rango
        reasons.append(f"Rotura ORB al alza ({alto:g})")
    elif price < bajo:
        side = "short"; ext = (bajo - price) / rango
        reasons.append(f"Rotura ORB a la baja ({bajo:g})")
    else:
        return None                       # dentro del rango: no hay nada que hacer

    if ext > 1.0:
        return None                       # ya se fue un rango entero: tarde
    score = 54
    reasons.append(f"Rango apertura {rango_pct:.2f}% en {o['n']} velas")
    if ext > 0.5:
        score -= 8; reasons.append("Entrada algo extendida")

    # El volumen es LO que separa una rotura real de una trampa.
    if m5.get("vol_spike"):
        score += 16; reasons.append("Pico de volumen en la rotura")
    else:
        score -= 10; reasons.append("Rotura sin volumen")

    # Un gap en la misma direccion refuerza el sesgo del dia.
    g = ctx.get("gap")
    if g and abs(g["pct"]) >= C.GAP_MIN_PCT:
        if (side == "long" and g["pct"] > 0) or (side == "short" and g["pct"] < 0):
            score += 8; reasons.append(f"Gap {g['pct']:+.2f}% a favor")
        else:
            score -= 6; reasons.append(f"Gap {g['pct']:+.2f}% en contra")

    # Cuanto antes se rompe el rango, mas limpio suele ser el dia.
    if desde <= 60:
        score += 6; reasons.append("Rotura temprana")

    score = max(0, min(score, 100))
    return {"name": "ORB", "side": side, "score": score,
            "prob": _prob(score, 0.02), "reasons": reasons}


# --------------------------- 0b) GAP_FADE ---------------------------
def gap_fade(ctx):
    """El gap de apertura: hueco entre el cierre de ayer y la apertura de hoy.

    Dos comportamientos opuestos segun tamaño, y hay que distinguirlos:
      - gap MODERADO (0.5-2%): tiende a RELLENARSE. El precio vuelve a buscar el
        cierre de ayer, que actua de iman. Se opera en contra del gap.
      - gap GRANDE (>2%): hay una noticia detras. No se rellena, CONTINUA.
        Ponerse en contra de un gap de noticia es de las formas mas rapidas de
        perder. Se opera a favor.

    Solo vale en la primera hora y media: despues el gap deja de ser referencia.
    """
    g = ctx.get("gap")
    ses = ctx.get("sesion") or {}
    if not g:
        return None
    pct = g["pct"]
    if abs(pct) < C.GAP_MIN_PCT:
        return None                       # no es gap, es ruido
    if g["cerrado"]:
        return None                       # ya se relleno: la operacion ya paso
    desde = ses.get("desde_apertura")
    if desde is None or desde > 90:
        return None

    m5 = ctx["m5"]
    rsi = m5.get("rsi")
    grande = abs(pct) >= C.GAP_GRANDE_PCT
    reasons, score = [], 0

    if grande:
        side = "long" if pct > 0 else "short"
        score = 48
        reasons.append(f"Gap {pct:+.2f}% (grande, probable noticia) -> continuacion")
        # que el momentum acompañe; si no, es un gap que se esta desinflando
        if m5.get("macd_flip") == ("up" if side == "long" else "down"):
            score += 10; reasons.append("Momentum confirma la continuacion")
        if rsi is not None:
            if side == "long" and rsi < 45: score -= 12; reasons.append(f"RSI {rsi:.0f} no acompaña")
            if side == "short" and rsi > 55: score -= 12; reasons.append(f"RSI {rsi:.0f} no acompaña")
    else:
        side = "short" if pct > 0 else "long"
        score = 46
        reasons.append(f"Gap {pct:+.2f}% sin rellenar -> busca cierre previo {g['cierre_previo']:g}")
        # el fade necesita agotamiento, no fuerza
        if rsi is not None:
            if side == "short" and rsi > 62: score += 14; reasons.append(f"RSI {rsi:.0f} sobrecomprado")
            elif side == "long" and rsi < 38: score += 14; reasons.append(f"RSI {rsi:.0f} sobrevendido")
            else: score -= 8; reasons.append(f"RSI {rsi:.0f} sin agotamiento")
        # divergencia a favor del giro
        clave = "BAJISTA" if side == "short" else "ALCISTA"
        if any(d.startswith(clave) for d in m5.get("divergences", [])):
            score += 12; reasons.append("Divergencia confirma el giro")

    score = max(0, min(score, 100))
    if score < 40:
        return None
    return {"name": "GAP_FADE", "side": side, "score": score,
            "prob": _prob(score), "reasons": reasons}


# --------------------------- 1) SMC_REVERSAL ---------------------------
def smc_reversal(ctx):
    m5, price = ctx["m5"], ctx["price"]
    hh, ll = _rango(ctx["h"], ctx["l"])
    pos = _pos_en_rango(price, hh, ll)
    rsi = m5.get("rsi")
    reasons, side, score = [], None, 0

    if pos >= 0.78:                       # zona PREMIUM extrema -> buscar giro corto
        side = "short"; score = 46; reasons.append("Cerca de PREMIUM")
        if rsi and rsi > 62: score += 14; reasons.append(f"RSI {rsi:.0f} sobreextendido")
    elif pos <= 0.22:                     # zona DISCOUNT extrema -> buscar giro largo
        side = "long"; score = 46; reasons.append("Cerca de DISCOUNT")
        if rsi and rsi < 38: score += 14; reasons.append(f"RSI {rsi:.0f} sobreextendido")
    else:
        return None

    # confluencia FVG opuesto (imbalance que empuja al giro)
    for t, lo, hi in m5.get("fvgs", []):
        if side == "short" and t == "bajista": score += 10; reasons.append("FVG bajista cercano"); break
        if side == "long"  and t == "alcista": score += 10; reasons.append("FVG alcista cercano"); break
    # divergencia a favor
    for d in m5.get("divergences", []):
        if side == "short" and d.startswith("BAJISTA"): score += 12; reasons.append("Divergencia bajista"); break
        if side == "long"  and d.startswith("ALCISTA"): score += 12; reasons.append("Divergencia alcista"); break

    score = min(score, 100)
    return {"name": "SMC_REVERSAL", "side": side, "score": score,
            "prob": _prob(score), "reasons": reasons}


# --------------------------- 2) BREAKOUT ---------------------------
def breakout(ctx):
    m5, price = ctx["m5"], ctx["price"]
    h, l = ctx["h"], ctx["l"]
    # rango previo excluyendo la vela actual
    hh_prev = max(h[-30:-1]); ll_prev = min(l[-30:-1])
    reasons, side, score = [], None, 0

    if price > hh_prev:
        side = "long"; score = 42; reasons.append("Rotura de maximos de rango")
    elif price < ll_prev:
        side = "short"; score = 42; reasons.append("Rotura de minimos de rango")
    else:
        return None

    if m5.get("vol_spike"):
        score += 22; reasons.append("Pico de volumen (>2x prom20)")
    else:
        score -= 12; reasons.append("Sin confirmacion de volumen")
    # rotura a favor del sesgo estructural
    tr = m5.get("trend")
    if side == "long" and tr in ("ALCISTA", "EXPANSION"): score += 10; reasons.append("Estructura acompaña")
    if side == "short" and tr in ("BAJISTA", "EXPANSION"): score += 10; reasons.append("Estructura acompaña")

    score = max(0, min(score, 100))
    if score < 40:
        return None
    return {"name": "BREAKOUT", "side": side, "score": score,
            "prob": _prob(score), "reasons": reasons}


# --------------------------- 3) TREND_PULLBACK ---------------------------
def trend_pullback(ctx):
    m5, price = ctx["m5"], ctx["price"]
    e50 = m5.get("ema50"); rsi = m5.get("rsi"); bias = m5.get("bias")
    if not e50 or rsi is None:
        return None
    dist = abs(price - e50) / price * 100.0     # % de distancia a la EMA50
    cerca = dist <= 0.6                          # "pullback" = tocando la EMA50
    reasons, side, score = [], None, 0

    if bias == "alcista" and cerca and 40 <= rsi <= 56:
        side = "long"; score = 52
        reasons += ["Tendencia alcista", "Pullback a EMA50", f"RSI {rsi:.0f} neutral"]
    elif bias == "bajista" and cerca and 44 <= rsi <= 60:
        side = "short"; score = 52
        reasons += ["Tendencia bajista", "Pullback a EMA50", f"RSI {rsi:.0f} neutral"]
    else:
        return None

    # EMA13 a favor refina la entrada
    e13 = m5.get("ema13")
    if e13:
        if side == "long" and price >= e13: score += 8; reasons.append("Recupera EMA13")
        if side == "short" and price <= e13: score += 8; reasons.append("Pierde EMA13")

    score = min(score, 100)
    return {"name": "TREND_PULLBACK", "side": side, "score": score,
            "prob": _prob(score), "reasons": reasons}


# --------------------------- 4) RSI_DIVERGENCE (multi-timeframe) ---------------------------
def rsi_divergence(ctx):
    """Divergencia RSI en el TF de escaneo, con CONFIRMACION en el TF superior
    (como en el video: mira divergencia en 15m Y en 1h a la vez)."""
    m5, mh = ctx["m5"], ctx.get("mh", {})
    divs = m5.get("divergences", [])
    if not divs:
        return None
    d = divs[0]
    side = "long" if d.startswith("ALCISTA") else "short"
    score = 46
    reasons = [f"Divergencia {'alcista' if side=='long' else 'bajista'} RSI"]
    rsi = m5.get("rsi")
    if rsi is not None:
        if side == "long" and rsi < 45: score += 12; reasons.append(f"RSI {rsi:.0f} saliendo de sobreventa")
        if side == "short" and rsi > 55: score += 12; reasons.append(f"RSI {rsi:.0f} saliendo de sobrecompra")
    # confirmacion en el TF superior (HTF): divergencia en la misma direccion
    clave = "ALCISTA" if side == "long" else "BAJISTA"
    if any(x.startswith(clave) for x in mh.get("divergences", [])):
        score += 16; reasons.append("Confirmada en HTF (1h)")
    score = min(score, 100)
    return {"name": "RSI_DIVERGENCE", "side": side, "score": score,
            "prob": _prob(score), "reasons": reasons}


# --------------------------- 5) VP_MEAN_REVERT ---------------------------
def vp_mean_revert(ctx):
    """Mean-reversion: precio muy alejado de su 'valor' (EMA50 como proxy de VPOC)
    con RSI en extremo -> revierte a la media."""
    m5, price = ctx["m5"], ctx["price"]
    e50 = m5.get("ema50"); rsi = m5.get("rsi"); atr = m5.get("atr")
    if not e50 or rsi is None or not atr:
        return None
    desv_atr = (price - e50) / atr          # cuantos ATR fuera de la media
    reasons, side, score = [], None, 0

    if desv_atr >= 2.5 and rsi >= 70:
        side = "short"; score = 45
        reasons += [f"{desv_atr:.1f} ATR sobre la media", f"RSI {rsi:.0f} sobrecompra"]
    elif desv_atr <= -2.5 and rsi <= 30:
        side = "long"; score = 45
        reasons += [f"{abs(desv_atr):.1f} ATR bajo la media", f"RSI {rsi:.0f} sobreventa"]
    else:
        return None

    # rango (no tendencia fuerte) favorece la reversion a la media
    if m5.get("trend") in ("RANGO", "CONTRACCION"):
        score += 14; reasons.append("Mercado en rango")
    score = min(score, 100)
    return {"name": "VP_MEAN_REVERT", "side": side, "score": score,
            "prob": _prob(score), "reasons": reasons}


# --------------------------- 6) LIQUIDITY_GRAB ---------------------------
def liquidity_grab(ctx):
    """Barrido de liquidez: la mecha de la ultima vela perfora un maximo (EQH)
    o minimo (EQL) previo y el cuerpo cierra de vuelta dentro -> giro."""
    h, l, c = ctx["h"], ctx["l"], ctx["c"]
    m5 = ctx["m5"]
    if len(c) < 25:
        return None
    hh_prev = max(h[-25:-1]); ll_prev = min(l[-25:-1])
    hi, lo, cl = h[-1], l[-1], c[-1]
    reasons, side, score = [], None, 0

    grab_high = hi > hh_prev and cl < hh_prev      # barrio EQH y cerro debajo
    grab_low  = lo < ll_prev and cl > ll_prev      # barrio EQL y cerro encima

    if grab_high:
        side = "short"; score = 50
        reasons += ["Barrido de EQH (liquidez arriba)", "Cierre de vuelta dentro"]
    elif grab_low:
        side = "long"; score = 50
        reasons += ["Barrido de EQL (liquidez abajo)", "Cierre de vuelta dentro"]
    else:
        return None

    rsi = m5.get("rsi")
    if rsi is not None:
        reasons.append(f"RSI {rsi:.1f}")
        if side == "short" and rsi > 55: score += 10
        if side == "long" and rsi < 45: score += 10
    # mecha de barrido pronunciada = mas fiable
    rango_vela = (hi - lo) or 1e-9
    mecha = (hi - cl) / rango_vela if side == "short" else (cl - lo) / rango_vela
    if mecha > 0.5:
        score += 12; reasons.append("Mecha de rechazo larga")
    score = min(score, 100)
    return {"name": "LIQUIDITY_GRAB", "side": side, "score": score,
            "prob": _prob(score), "reasons": reasons}


# --------------------------- 7) MOMENTUM_SHIFT ---------------------------
def momentum_shift(ctx):
    """Giro de momentum: el histograma MACD cambia de color (cruza el 0).
    Reproduce lo del video ('esa barra roja, la siguiente verde, ya la sigo').
    Confirma con el momentum del TF superior (histograma 1h del mismo signo)."""
    m5, mh = ctx["m5"], ctx.get("mh", {})
    flip = m5.get("macd_flip")
    if not flip:
        return None
    side = "long" if flip == "up" else "short"
    score = 48
    reasons = ["Histograma MACD " + ("rojo→verde" if side == "long" else "verde→rojo")]

    # momentum del HTF a favor (histograma 1h del mismo signo)
    hh = mh.get("macd_hist")
    if hh is not None:
        if side == "long" and hh > 0: score += 14; reasons.append("Momentum 1h alcista")
        elif side == "short" and hh < 0: score += 14; reasons.append("Momentum 1h bajista")
        else: score -= 6; reasons.append("Momentum 1h en contra")
    # a favor del sesgo estructural del 5m
    bias = m5.get("bias")
    if side == "long" and bias == "alcista": score += 8; reasons.append("Sesgo 5m alcista")
    if side == "short" and bias == "bajista": score += 8; reasons.append("Sesgo 5m bajista")
    # RSI no en extremo contrario (evita entrar tarde)
    rsi = m5.get("rsi")
    if rsi is not None:
        if side == "long" and rsi > 72: return None
        if side == "short" and rsi < 28: return None
    score = max(0, min(score, 100))
    return {"name": "MOMENTUM_SHIFT", "side": side, "score": score,
            "prob": _prob(score), "reasons": reasons}


# --------------------------- registro ---------------------------
# ORB y GAP_FADE van primero: son las que mandan en acciones.
TODAS = [orb, gap_fade, smc_reversal, breakout, trend_pullback, rsi_divergence,
         vp_mean_revert, liquidity_grab, momentum_shift]

def evaluar_todas(ctx):
    """Corre las 9 y devuelve la lista de las que dispararon."""
    out = []
    for fn in TODAS:
        try:
            r = fn(ctx)
            if r:
                out.append(r)
        except Exception as e:
            out.append({"name": fn.__name__, "side": None, "score": 0, "prob": 0,
                        "reasons": [f"error: {e}"]})
    return [r for r in out if r.get("side")]
