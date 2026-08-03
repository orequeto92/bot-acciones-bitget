# -*- coding: utf-8 -*-
"""
SERVICIO.PY - Un solo proceso que cubre la sesion entera.

Por que existe
--------------
El workflow original disparaba un cron cada 5 minutos. Medido en la primera
sesion real (3-ago-2026): de ~42 ejecuciones esperadas desde la apertura,
GitHub lanzo UNA. Los cron de Actions no son fiables: bajo carga se retrasan y
directamente se descartan, y en repos publicos pasa constantemente.

El efecto no era solo perderse señales. El SEGUIMIENTO tampoco corria, asi que
nadie avisaba de TP1 (y por tanto de mover el SL a break-even) ni del cierre de
sesion. Un bot que detecta pero no vigila es peor que ninguno.

La solucion es invertir el modelo: en vez de 78 disparos que quiza no ocurran,
UN disparo que se queda vivo toda la sesion haciendo el ciclo cada 5 minutos.
Un job de Actions aguanta hasta 6 horas, asi que hacen falta dos tandas para
cubrir las 6.5 horas de mercado.

USO:
  python tools/servicio.py                 # corre hasta que cierre el mercado
  python tools/servicio.py --minutos 350   # tope de tiempo (limite del job)
  python tools/servicio.py --intervalo 300 # segundos entre ciclos
  python tools/servicio.py --una-vez       # un solo ciclo (equivale al workflow viejo)
"""
import sys, os, time, argparse, traceback

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import sesion, control, seguimiento, acciones, ajustes, telegram
import config as C


def _hay_mercado_abierto():
    """True si alguno de los dos calendarios esta operativo."""
    return any(sesion.estado(cal)["abierto"] for cal in ("US_EQUITY", "COMMODITY"))


def ciclo(n, con_telegram=True):
    """Un ciclo completo: comandos -> seguimiento -> escaneo."""
    et = sesion.a_et().strftime("%H:%M")
    est = sesion.estado("US_EQUITY", orb_min=C.ORB_MINUTOS,
                        aviso_cierre_min=C.NO_ABRIR_ULTIMOS_MIN)
    fase = est["fase"] if est["abierto"] else f"cerrado ({est['motivo']})"
    print(f"\n{'='*58}\n  ciclo {n} | {et} ET | {fase}\n{'='*58}", flush=True)

    # 1) comandos de Telegram
    try:
        n_cmd = control.procesar_una_vez() if telegram.configurado() else 0
        if n_cmd:
            print(f"  [control] {n_cmd} comando(s) procesado(s)", flush=True)
    except Exception as e:
        print(f"  [control] error: {e}", flush=True)

    # 2) seguimiento de lo abierto (SIEMPRE, incluso fuera de sesion: es quien
    #    avisa de que hay una posicion durmiendo con el mercado cerrado)
    try:
        avisos, activas = seguimiento.seguir(enviar_tg=con_telegram, verbose=False)
        print(f"  [seguimiento] {len(avisos)} aviso(s), {activas} posicion(es) activa(s)",
              flush=True)
        for a in avisos:
            print(f"      {a}", flush=True)
    except Exception as e:
        print(f"  [seguimiento] error: {e}", flush=True)

    # 3) escaneo (solo si hay mercado y el bot no esta en pausa)
    if not est["abierto"]:
        print("  [escaneo] mercado cerrado, no se escanea", flush=True)
        return
    if not ajustes.get("activo", True):
        print("  [escaneo] bot en PAUSA (/activar en Telegram)", flush=True)
        return
    try:
        capital = ajustes.capital(C.CAPITAL_TOTAL)
        top, todas, desc, cerrados = acciones.escanear(C.ACTIVOS, capital, verbose=False)
        print(f"  [escaneo] {len(todas)} señal(es) validas, {desc} descartadas", flush=True)
        if top:
            enviadas = 0
            for r in top:
                texto = acciones.formatear(r)
                print("\n" + texto, flush=True)
                if con_telegram and telegram.configurado() and telegram.enviar(texto):
                    enviadas += 1
            seguimiento.registrar(top)
            print(f"  [escaneo] {enviadas}/{len(top)} enviadas y registradas", flush=True)
    except Exception as e:
        print(f"  [escaneo] error: {e}", flush=True)
        traceback.print_exc()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--intervalo", type=int, default=300, help="segundos entre ciclos")
    ap.add_argument("--minutos", type=int, default=350,
                    help="tope de minutos vivo (el job de Actions muere a las 6h)")
    ap.add_argument("--una-vez", action="store_true", help="un solo ciclo y salir")
    ap.add_argument("--sin-telegram", action="store_true")
    a = ap.parse_args()

    con_tg = not a.sin_telegram
    print(f"🤖 {C.BOT_NOMBRE} — servicio de sesion")
    print(f"   intervalo {a.intervalo}s | tope {a.minutos} min | "
          f"telegram {'si' if con_tg and telegram.configurado() else 'no'}")

    if a.una_vez:
        ciclo(1, con_tg)
        return 0

    t0 = time.time()
    n = 0
    ocioso = 0          # ciclos seguidos con el mercado cerrado
    while (time.time() - t0) < a.minutos * 60:
        n += 1
        try:
            ciclo(n, con_tg)
        except Exception as e:
            print(f"  [ciclo] error no controlado: {e}", flush=True)
            traceback.print_exc()

        if _hay_mercado_abierto():
            ocioso = 0
        else:
            ocioso += 1
            # Si arranca antes de la apertura espera; pero si lleva mucho rato
            # cerrado es que la sesion termino: salir y dejar que se guarde el
            # estado en vez de quemar minutos de Actions para nada.
            if ocioso >= 3:
                print("\n  Mercado cerrado de forma sostenida: fin del servicio.", flush=True)
                break
        time.sleep(a.intervalo)

    mins = (time.time() - t0) / 60
    print(f"\n✅ Servicio terminado: {n} ciclos en {mins:.0f} minutos.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
