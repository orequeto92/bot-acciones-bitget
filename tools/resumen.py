# -*- coding: utf-8 -*-
"""
RESUMEN.PY - Que habria hecho CADA señal emitida, se operase o no.

Por que hace falta
------------------
El backtest dice lo que el sistema haria en el pasado. Las operaciones reales
dicen lo que hizo Brahian. Falta la tercera pata: que habrian hecho TODAS las
señales que el bot emitio en vivo, incluidas las que no se operaron.

Eso es la muestra fuera de muestra de verdad, y es la unica que puede confirmar
o desmentir el backtest, porque:
  - no la elige nadie a posteriori (el log solo crece, no se edita)
  - no depende de si a Brahian le dio tiempo, o le dio miedo, o estaba dormido
  - separa el merito del sistema del merito (o el error) de la ejecucion

Lee datos/senales.jsonl (lo escribe servicio.py con cada señal) y simula el
resultado con las velas reales, aplicando las mismas reglas del backtest:
escalera 33/33/34, break-even tras TP1, cierre al final de la sesion, y si una
vela contiene TP y SL se asume SL.

USO:
  python tools/resumen.py               # la sesion de hoy
  python tools/resumen.py --dia 2026-08-04
  python tools/resumen.py --todas       # todo el historico
  python tools/resumen.py --telegram    # ademas lo manda al chat
"""
import sys, os, json, argparse, datetime as dt

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import bitget, sesion, telegram
import config as C

LOG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "datos", "senales.jsonl")


def cargar(dia=None, todas=False):
    if not os.path.exists(LOG):
        return []
    filas = []
    with open(LOG, encoding="utf-8") as f:
        for linea in f:
            linea = linea.strip()
            if not linea:
                continue
            try:
                filas.append(json.loads(linea))
            except ValueError:
                continue
    if todas:
        return filas
    objetivo = dia or sesion.a_et().strftime("%Y-%m-%d")
    return [x for x in filas if x["et"].startswith(objetivo)]


def evaluar(s):
    """Simula la señal con las velas reales de su sesion."""
    cal = C.calendario_de(s["sym"])
    ent, sl = s["entrada"], s["sl"]
    dist = abs(sl - ent)
    if not dist:
        return None
    side = s["side"]
    fecha = dt.date.fromisoformat(s["et"][:10])
    h = sesion.horario_del_dia(fecha, cal)
    if not h:
        return None
    cierra = h[1]

    try:
        k = bitget.klines(s["sym"], "5m", 1000)
    except Exception as e:
        return {"error": str(e)}
    velas = [v for v in sesion.filtrar_rth(k, cal)
             if int(v[0]) > s["ts"] * 1000 and sesion.ts_a_et(v[0]) < cierra]
    if not velas:
        return {"error": "sin velas posteriores"}

    niveles = [ent + dist * r if side == "long" else ent - dist * r for r in C.TP_R]
    sl_vivo, alcanzados, be, evento = sl, 0, False, None
    for v in velas:
        hi, lo = float(v[2]), float(v[3])
        golpe_sl = lo <= sl_vivo if side == "long" else hi >= sl_vivo
        sig = niveles[alcanzados] if alcanzados < len(niveles) else None
        golpe_tp = sig is not None and (hi >= sig if side == "long" else lo <= sig)
        if golpe_sl:                      # pesimista: el SL gana el empate
            evento = "BE" if be else "SL"
            salida = 0.0 if be else -1.0
            break
        if golpe_tp:
            alcanzados += 1
            if alcanzados == 1:
                be, sl_vivo = True, ent
            if alcanzados == len(niveles):
                evento, salida = "TP3", C.TP_R[-1]
                break
    else:
        ult = float(velas[-1][4])
        salida = ((ult - ent) if side == "long" else (ent - ult)) / dist
        evento = "CIERRE"

    total, resto = 0.0, 1.0
    for i, (p, r) in enumerate(zip(C.TP_PARCIAL, C.TP_R)):
        if i < alcanzados:
            total += p * r
            resto -= p
    return {"r": total + resto * salida, "evento": evento, "tps": alcanzados,
            "abierta": evento == "CIERRE" and alcanzados < len(C.TP_R)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dia", help="YYYY-MM-DD (por defecto hoy)")
    ap.add_argument("--todas", action="store_true")
    ap.add_argument("--telegram", action="store_true")
    a = ap.parse_args()

    filas = cargar(a.dia, a.todas)
    if not filas:
        print("Sin señales registradas todavia. El log lo escribe servicio.py "
              "a partir de la proxima sesion.")
        return

    L = []
    dia = "historico completo" if a.todas else (a.dia or sesion.a_et().strftime("%Y-%m-%d"))
    L.append(f"📊 Señales emitidas — {dia}")
    L.append("")
    rs, sl_n, tp3_n, be_n = [], 0, 0, 0
    for s in filas:
        res = evaluar(s)
        if not res or res.get("error"):
            L.append(f"  {s['et'][11:]} {s['sym']:<12} {s['side'][:1].upper()} "
                     f"score {s['score']:>3}  -> {res.get('error') if res else 'sin datos'}")
            continue
        rs.append(res["r"])
        sl_n += res["evento"] == "SL"
        tp3_n += res["evento"] == "TP3"
        be_n += res["evento"] == "BE"
        marca = "🟢" if res["r"] > 0.01 else ("🔴" if res["r"] < -0.01 else "⚪")
        L.append(f"  {marca} {s['et'][11:]} {s['sym']:<12} {s['side'][:1].upper()} "
                 f"score {s['score']:>3} {s['setup']:<15} "
                 f"{res['evento']:<7} {res['r']:>+6.2f}R")
    if rs:
        L.append("")
        L.append(f"  {len(rs)} señal(es) | aciertos {sum(1 for r in rs if r>0.01)/len(rs)*100:.0f}% "
                 f"| R total {sum(rs):+.2f} | R medio {sum(rs)/len(rs):+.3f}")
        L.append(f"  SL {sl_n} · BE {be_n} · TP3 {tp3_n} · resto cerro en sesion")
        riesgo = C.CAPITAL_TOTAL * C.MARGEN_OP_PCT / 100 * C.SL_PCT_MARGEN / 100
        L.append(f"  equivale a {sum(rs)*riesgo:+.2f}$ con la cuenta de {C.CAPITAL_TOTAL:.0f}$ "
                 f"(BRUTO, sin comisiones)")
    texto = "\n".join(L)
    print(texto)
    if a.telegram and telegram.configurado():
        telegram.enviar(texto)


if __name__ == "__main__":
    main()
