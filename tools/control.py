# -*- coding: utf-8 -*-
"""
CONTROL.PY - Procesa los COMANDOS que le envias al bot por Telegram.

Lee los mensajes nuevos (getUpdates), ejecuta el comando y responde. Pensado para
correr en cada ciclo del workflow (antes del escaneo) o en bucle en un host 24/7.

COMANDOS:
  /ayuda                 lista de comandos
  /estado                capital vigente, activo/pausa y posiciones abiertas
  /saldo <n>   (/capital) fija el capital TOTAL de la cuenta -> las señales lo usan ya
  /escanear    (/scan)   fuerza un escaneo ahora y envia las señales
  /seguir                revisa ahora las operaciones abiertas (TP/SL/break-even)
  /activar               reactiva el envio automatico de señales
  /parar   (/pausar)     pausa el envio automatico (el seguimiento sigue)

USO:
  python tools/control.py           # procesa los comandos pendientes una vez
  python tools/control.py --loop    # los procesa cada 10s (host 24/7)
"""
import sys, os, time, argparse

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import telegram, ajustes, seguimiento, acciones, sesion
import config as C

AYUDA = (
    "🤖 *Bot Acciones Bitget* — comandos:\n"
    "/sesion — si el mercado esta abierto y cuanto queda\n"
    "/estado — capital, estado y posiciones abiertas\n"
    "/saldo <n> — fija el capital TOTAL (ej: /saldo 250)\n"
    "/escanear — fuerza un escaneo ahora\n"
    "/seguir — revisa TP/SL/break-even de lo abierto\n"
    "/activar — reactiva las señales automaticas\n"
    "/parar — pausa las señales automaticas\n"
    "/ayuda — esta lista"
)


def _responder(chat_id, texto):
    telegram.enviar(texto, chat_id=chat_id)


def _cmd_estado(chat_id):
    cap = ajustes.capital(C.CAPITAL_TOTAL)
    activo = ajustes.get("activo", True)
    pos = seguimiento._cargar().get("posiciones", [])
    abiertas = [p for p in pos if p.get("estado") == "activa"]
    txt = [f"📊 Estado del Bot Acciones",
           f"Capital total: {cap:.2f}$  (margen/op {C.MARGEN_OP_PCT:.0f}% = {cap*C.MARGEN_OP_PCT/100:.2f}$)",
           f"Automatico: {'🟢 ACTIVO' if activo else '⏸️ EN PAUSA'}",
           f"Operaciones abiertas: {len(abiertas)}"]
    for p in abiertas:
        hechos = [k.upper() for k in ("tp1", "tp2", "tp3") if p["hitos"].get(k)]
        txt.append(f"  · {p['sym']} {p['side'].upper()} entrada {p['entrada']:g}"
                   + (f" [{'/'.join(hechos)}]" if hechos else ""))
    _responder(chat_id, "\n".join(txt))


def _cmd_saldo(chat_id, arg):
    try:
        n = float(arg.replace(",", ".").replace("$", "").strip())
        if n <= 0:
            raise ValueError
    except Exception:
        _responder(chat_id, "❌ Usa: /saldo <numero>  (ej: /saldo 250)")
        return
    ajustes.set("capital_total", n)
    _responder(chat_id, f"✅ Capital total actualizado a *{n:.2f}$*.\n"
                        f"Margen por operacion: {n*C.MARGEN_OP_PCT/100:.2f}$ | "
                        f"riesgo por op: ~{n*C.MARGEN_OP_PCT/100*C.SL_PCT_MARGEN/100:.2f}$.\n"
                        f"Las proximas señales ya lo usan.")


def _cmd_sesion(chat_id):
    """Estado del reloj de mercado. En acciones es la pregunta mas frecuente:
    el bot puede estar perfecto y no decir nada simplemente porque es sabado."""
    lineas = []
    for cal in ("US_EQUITY", "COMMODITY"):
        e = sesion.estado(cal, orb_min=C.ORB_MINUTOS, aviso_cierre_min=C.NO_ABRIR_ULTIMOS_MIN)
        nombre = sesion.CALENDARIOS[cal]["nombre"]
        if e["abierto"]:
            lineas.append(f"🔔 {nombre}: ABIERTO (fase {e['fase']})\n"
                          f"    lleva {e['desde_apertura']:.0f} min | "
                          f"cierra en {e['para_cierre']:.0f} min"
                          + ("\n    ⚠️ hoy es MEDIA SESION (cierre 13:00 ET)" if e["media_sesion"] else ""))
        else:
            lineas.append(f"🌙 {nombre}: CERRADO ({e['motivo']})")
    hora = sesion.a_et().strftime("%a %d-%b %H:%M")
    return _responder(chat_id, f"🕒 Hora del mercado: {hora} ET\n\n" + "\n".join(lineas))


def _cmd_escanear(chat_id):
    cap = ajustes.capital(C.CAPITAL_TOTAL)
    est = sesion.estado("US_EQUITY")
    if not est["abierto"]:
        _responder(chat_id, f"🌙 Mercado cerrado ({est['motivo']}). "
                            f"El bot solo opera en sesion: 9:30-16:00 ET, lunes a viernes.")
        return
    _responder(chat_id, f"🔎 Escaneando {len(C.ACTIVOS)} activos (capital {cap:.0f}$)...")
    top, todas, desc, cerrados = acciones.escanear(C.ACTIVOS, cap, verbose=False)
    if not top:
        _responder(chat_id, "Sin señales que superen el umbral ahora mismo.")
        return
    for r in top:
        telegram.enviar(acciones.formatear(r), chat_id=chat_id)
    seguimiento.registrar(top)


def _cmd_seguir(chat_id):
    avisos, activas = seguimiento.seguir(enviar_tg=True, verbose=False)
    if not avisos:
        _responder(chat_id, f"Sin novedades en {activas} operacion(es) abierta(s).")


def procesar_una_vez():
    """Lee y ejecuta todos los comandos pendientes. Devuelve cuantos proceso."""
    offset = ajustes.get("tg_offset", 0)
    updates = telegram.get_updates(offset=offset)
    n = 0
    for u in updates:
        ajustes.set("tg_offset", u["update_id"] + 1)   # avanza aunque falle el comando
        msg = u.get("message") or u.get("channel_post") or {}
        text = (msg.get("text") or "").strip()
        chat_id = (msg.get("chat") or {}).get("id")
        if not text.startswith("/") or chat_id is None:
            continue
        n += 1
        partes = text.split(maxsplit=1)
        cmd = partes[0].split("@")[0].lower()      # quita @NombreBot en grupos
        arg = partes[1] if len(partes) > 1 else ""
        try:
            if cmd in ("/ayuda", "/help", "/start"):
                _responder(chat_id, AYUDA)
            elif cmd == "/estado":
                _cmd_estado(chat_id)
            elif cmd in ("/saldo", "/capital"):
                _cmd_saldo(chat_id, arg)
            elif cmd in ("/escanear", "/scan"):
                _cmd_escanear(chat_id)
            elif cmd == "/seguir":
                _cmd_seguir(chat_id)
            elif cmd in ("/sesion", "/mercado", "/horario"):
                _cmd_sesion(chat_id)
            elif cmd == "/activar":
                ajustes.set("activo", True); _responder(chat_id, "🟢 Bot ACTIVADO. Vuelve a enviar señales.")
            elif cmd in ("/parar", "/pausar", "/desactivar"):
                ajustes.set("activo", False); _responder(chat_id, "⏸️ Bot EN PAUSA. No enviara señales nuevas (el seguimiento sigue).")
            else:
                _responder(chat_id, f"❓ No conozco '{cmd}'.\n{AYUDA}")
        except Exception as e:
            _responder(chat_id, f"⚠️ Error ejecutando {cmd}: {e}")
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true", help="procesa comandos cada 10s")
    ap.add_argument("--intervalo", type=int, default=10)
    a = ap.parse_args()
    if not telegram.configurado():
        print("[control] sin credenciales de Telegram; nada que hacer.")
        return
    if not a.loop:
        print(f"[control] procesados {procesar_una_vez()} comando(s).")
        return
    print(f"[control] escuchando comandos cada {a.intervalo}s. Ctrl+C para parar.")
    while True:
        try:
            procesar_una_vez()
        except Exception as e:
            print(f"[control] error: {e}")
        time.sleep(a.intervalo)


if __name__ == "__main__":
    main()
