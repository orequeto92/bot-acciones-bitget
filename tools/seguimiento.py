# -*- coding: utf-8 -*-
"""
SEGUIMIENTO.PY - Vigila las operaciones ABIERTAS en 1m y avisa por Telegram
cuando tocan TP1 / TP2 / TP3 / SL, y recuerda mover el SL a BREAK-EVEN tras TP1.

El escaneo (acciones.py) DETECTA entradas en 5m; esto CUIDA la operación
despues, minuto a minuto.

NO ejecuta nada en el exchange: solo lee precio y te avisa (tu operas a mano en Bitget).

AVISO PROPIO DE ACCIONES: ademas de TP/SL vigila el CIERRE DE SESION. Una
posicion abierta al sonar la campana se queda toda la noche en un mercado sin
volumen y amanece con el gap del dia siguiente ya hecho, imposible de gestionar.
Por eso avisa CERRAR_ANTES_MIN minutos antes del cierre y da la posicion por
terminada.

FLUJO:
  1) acciones.py --registrar -> guarda cada señal emitida en datos/posiciones.json
  2) seguimiento.py          -> una pasada: revisa niveles y avisa lo nuevo
     seguimiento.py --loop    -> se queda corriendo y revisa cada 60s
     seguimiento.py --telegram-> ademas manda los avisos a Telegram

Las posiciones que llegan a TP3, SL o fin de sesion se marcan 'cerrada' y se archivan.
"""
import sys, os, json, time, argparse

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import bitget, telegram, sesion
import config as C

POS_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "datos", "posiciones.json")


# ----------------------------- persistencia -----------------------------
def _cargar():
    try:
        return json.load(open(POS_PATH, encoding="utf-8"))
    except Exception:
        return {"posiciones": [], "historial": []}

def _guardar(d):
    os.makedirs(os.path.dirname(POS_PATH), exist_ok=True)
    json.dump(d, open(POS_PATH, "w", encoding="utf-8"), indent=2, ensure_ascii=False)


# ----------------------------- registro -----------------------------
def registrar(resultados):
    """Anade señales emitidas (dicts 'r' de portero.py) como posiciones activas.
    Evita duplicar si ya hay una activa del mismo simbolo."""
    d = _cargar()
    activos = {p["sym"] for p in d["posiciones"] if p["estado"] == "activa"}
    nuevas = 0
    for r in resultados:
        sym = r["sym"]
        if sym in activos:
            continue
        ens, plan, m5 = r["ens"], r["plan"], r["m5"]
        d["posiciones"].append({
            "id": f"{sym}-{int(time.time())}",
            "sym": sym, "side": ens["side"],
            "entrada": m5["price"], "sl": plan["sl"], "sl_ini": plan["sl"],
            "tps": [{"r": t["r"], "precio": t["precio"]} for t in plan["tps"]],
            "verde": r["verde"], "setup": ens["best"]["name"],
            "estado": "activa",
            "hitos": {"tp1": False, "tp2": False, "tp3": False, "sl": False, "be": False},
            "abierta_ts": int(time.time()),
        })
        activos.add(sym); nuevas += 1
    if nuevas:
        _guardar(d)
    return nuevas


# ----------------------------- seguimiento -----------------------------
def _rango_reciente(sym, velas=5):
    """max high / min low / ultimo cierre de las ultimas velas de 1m EN SESION.

    EL FILTRO RTH ES OBLIGATORIO AQUI TAMBIEN. Sin el, despues del cierre esta
    funcion lee precios de post-mercado y dispara TP/SL con ticks que no
    existieron para ti. Caso real (AVGO, 5-ago-2026): el minimo EN SESION fue
    417.75 y el SL estaba en 417.70 -> no salto, la sesion cerro en 418.75
    (-0.87R). Pero a las 16:00, ya fuera de mercado, el precio bajo a 417.05 y
    el bot canto "SL tocado, -1R". Registro falso, y si hubiese habido posicion
    abierta el aviso mandaba cerrar a un precio inexistente.

    Devuelve None si no hay velas en sesion (mercado cerrado).
    """
    cal = C.calendario_de(sym)
    k = sesion.filtrar_rth(bitget.klines(sym, C.TF_FOLLOW, 60), cal)[-velas:]
    if not k:
        return None
    return (max(float(c[2]) for c in k),
            min(float(c[3]) for c in k),
            float(k[-1][4]))

def _emoji(sym, side):
    return f"{'🟢' if side == 'long' else '🔴'} {sym} {side.upper()}"

def seguir(enviar_tg=False, verbose=True):
    """Una pasada de seguimiento sobre todas las posiciones activas."""
    d = _cargar()
    avisos = []
    for p in d["posiciones"]:
        if p["estado"] != "activa":
            continue
        est_sym = sesion.estado(C.calendario_de(p["sym"]))
        try:
            rango = _rango_reciente(p["sym"])
        except Exception as e:
            if verbose: print(f"  · {p['sym']}: error datos ({e})")
            continue
        if rango is None or not est_sym["abierto"]:
            # Mercado cerrado: NO se evaluan TP/SL (los precios de post-mercado
            # no son operables). Se salta al bloque de cierre de sesion.
            price = float(p["entrada"])
            if p["estado"] == "activa" and not C.PERMITIR_OVERNIGHT:
                p["estado"] = "cerrada"; p["cierre"] = "FUERA_SESION"
                avisos.append(
                    f"⚠️ {_emoji(p['sym'], p['side'])} — el mercado YA CERRO y la posicion "
                    f"seguia abierta ({est_sym['motivo']}). Cierrala en cuanto puedas: "
                    f"fuera de sesion el spread se dispara y mañana abre con gap.")
            continue
        hi, lo, price = rango
        side = p["side"]
        tps = p["tps"]
        # helper: ¿el precio alcanzo un nivel a favor?
        def tp_tocado(nivel):
            return hi >= nivel if side == "long" else lo <= nivel
        def sl_tocado(nivel):
            return lo <= nivel if side == "long" else hi >= nivel

        # --- TP1 ---
        if not p["hitos"]["tp1"] and len(tps) >= 1 and tp_tocado(tps[0]["precio"]):
            p["hitos"]["tp1"] = True
            if C.MOVER_BE_TRAS_TP1:
                p["hitos"]["be"] = True
                p["sl"] = p["entrada"]          # break-even
            avisos.append(f"🎯 {_emoji(p['sym'], side)} — TP1 alcanzado ({tps[0]['precio']:g}). "
                          f"Cierra {int(C.TP_PARCIAL[0]*100)}% y MUEVE EL SL A BREAK-EVEN ({p['entrada']:g}).")
        # --- TP2 ---
        if not p["hitos"]["tp2"] and len(tps) >= 2 and tp_tocado(tps[1]["precio"]):
            p["hitos"]["tp2"] = True
            avisos.append(f"🎯 {_emoji(p['sym'], side)} — TP2 alcanzado ({tps[1]['precio']:g}). "
                          f"Cierra otro {int(C.TP_PARCIAL[1]*100)}%.")
        # --- TP3 (cierre) ---
        if not p["hitos"]["tp3"] and len(tps) >= 3 and tp_tocado(tps[2]["precio"]):
            p["hitos"]["tp3"] = True
            p["estado"] = "cerrada"; p["cierre"] = "TP3"
            avisos.append(f"🏁 {_emoji(p['sym'], side)} — TP3 alcanzado ({tps[2]['precio']:g}). "
                          f"Cierra el resto ({int(C.TP_PARCIAL[2]*100)}%). Operacion COMPLETA.")
        # --- SL / break-even ---
        if p["estado"] == "activa" and not p["hitos"]["sl"] and sl_tocado(p["sl"]):
            p["hitos"]["sl"] = True
            p["estado"] = "cerrada"
            if p["hitos"]["be"]:
                p["cierre"] = "BE"
                avisos.append(f"⚖️ {_emoji(p['sym'], side)} — SL en break-even tocado ({p['sl']:g}). "
                              f"Operacion cerrada SIN perdida (ya cobraste parciales).")
            else:
                p["cierre"] = "SL"
                avisos.append(f"🛑 {_emoji(p['sym'], side)} — SL tocado ({p['sl']:g}). "
                              f"Operacion cerrada en perdida (-1R).")

        # --- CIERRE DE SESION (esto no existe en cripto) ---
        if p["estado"] == "activa" and not C.PERMITIR_OVERNIGHT:
            if est_sym["para_cierre"] is not None \
                    and est_sym["para_cierre"] <= C.CERRAR_ANTES_MIN:
                p["estado"] = "cerrada"; p["cierre"] = "FIN_SESION"
                avisos.append(
                    f"⏰ {_emoji(p['sym'], side)} — quedan {est_sym['para_cierre']:.0f} min para el "
                    f"cierre ({est_sym['cierra'].strftime('%H:%M')} ET). CIERRA A MERCADO ahora "
                    f"(precio {price:g}). No se duerme la posicion.")

    # archiva las cerradas (marca la hora de cierre -> arranca el cooldown)
    cerradas = [p for p in d["posiciones"] if p["estado"] == "cerrada"]
    for p in cerradas:
        p.setdefault("cerrada_ts", int(time.time()))
    d["posiciones"] = [p for p in d["posiciones"] if p["estado"] == "activa"]
    d["historial"] = (d.get("historial", []) + cerradas)[-200:]
    _guardar(d)

    activas = len(d["posiciones"])
    if verbose:
        if avisos:
            for a in avisos:
                print("  " + a)
        else:
            print(f"  · sin novedades ({activas} posicion(es) activa(s))")
    if enviar_tg and avisos and telegram.configurado():
        for a in avisos:
            telegram.enviar(a)
    return avisos, activas


# ----------------------------- CLI -----------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true", help="revisa cada 60s en bucle (host 24/7)")
    ap.add_argument("--telegram", action="store_true", help="manda los avisos a Telegram")
    ap.add_argument("--intervalo", type=int, default=60, help="segundos entre revisiones en --loop")
    a = ap.parse_args()

    if not a.loop:
        print("Seguimiento 1m — una pasada:")
        seguir(enviar_tg=a.telegram)
        return
    print(f"Seguimiento 1m en bucle (cada {a.intervalo}s). Ctrl+C para parar.")
    while True:
        try:
            seguir(enviar_tg=a.telegram)
        except Exception as e:
            print(f"  [seguimiento] error en la pasada: {e}")
        time.sleep(a.intervalo)


if __name__ == "__main__":
    main()
