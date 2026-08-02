# -*- coding: utf-8 -*-
"""
DIAGNOSTICO.PY - Comprueba que el canal de Telegram funciona de verdad.

Por que existe
--------------
Un bot de trading que se queda mudo y reporta "success" es peor que uno que
falla: te quedas sin señales y sin enterarte. control.py salia con codigo 0
tanto si mandaba los avisos como si no tenia credenciales, asi que el workflow
salia verde con el canal roto.

Esto comprueba la cadena entera y DEVUELVE CODIGO DISTINTO DE 0 si algo falla,
para que la ejecucion se ponga en rojo.

SEGURIDAD: este repo es PUBLICO y los logs de Actions los puede leer cualquiera.
Aqui NO se imprime el token ni el chat_id, solo si existen, su longitud y el
error exacto de la API. Con `--local` (solo en tu maquina) muestra ademas los
chat_id disponibles, que es lo que necesitas para rellenar el secret.

USO:
  python tools/diagnostico.py            # seguro para logs publicos
  python tools/diagnostico.py --local    # en tu PC: enseña los chat_id
"""
import sys, os, json, argparse, urllib.request, urllib.parse, urllib.error

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import telegram


def _api(tok, metodo, params=None, timeout=15):
    """Llama a la API de Telegram y devuelve (ok, datos_o_error)."""
    url = f"https://api.telegram.org/bot{tok}/{metodo}"
    data = urllib.parse.urlencode(params).encode() if params else None
    try:
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode())
        return bool(d.get("ok")), d.get("result", d)
    except urllib.error.HTTPError as e:
        try:
            cuerpo = json.loads(e.read().decode())
            return False, f"HTTP {e.code}: {cuerpo.get('description', '')}"
        except Exception:
            return False, f"HTTP {e.code}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--local", action="store_true",
                    help="muestra los chat_id (SOLO en tu maquina, nunca en Actions)")
    a = ap.parse_args()

    print("=" * 62)
    print("  DIAGNOSTICO DEL CANAL DE TELEGRAM")
    print("=" * 62)
    fallos = []

    # --- 1) ¿llegan las credenciales al proceso? ---
    tok_env = os.environ.get("TELEGRAM_TOKEN")
    chat_env = os.environ.get("TELEGRAM_CHAT_ID")
    tok, chat = telegram._cred()

    def estado(nombre, valor):
        if not valor:
            print(f"  {nombre:<20} AUSENTE o VACIO")
            return False
        print(f"  {nombre:<20} presente ({len(valor)} caracteres)")
        return True

    print("\n1) VARIABLES DE ENTORNO")
    ok_tok = estado("TELEGRAM_TOKEN", tok_env)
    ok_chat = estado("TELEGRAM_CHAT_ID", chat_env)
    if not ok_tok or not ok_chat:
        print("\n  -> Las variables no llegan al proceso. En GitHub revisa que los")
        print("     secrets se llamen EXACTAMENTE TELEGRAM_TOKEN y TELEGRAM_CHAT_ID")
        print("     (Settings > Secrets and variables > Actions > pestaña Secrets,")
        print("     NO 'Variables'), y que el valor no este vacio.")
        fallos.append("credenciales ausentes")

    if not tok:
        print("\nSin token no se puede comprobar nada mas.")
        return 1

    # --- 2) ¿el token es valido? ---
    print("\n2) TOKEN (getMe)")
    ok, res = _api(tok, "getMe")
    if ok:
        print(f"  token VALIDO -> bot @{res.get('username')} ({res.get('first_name')})")
    else:
        print(f"  token INVALIDO -> {res}")
        print("  -> Copia otra vez el token de @BotFather. Formato: 1234567890:AAxx...")
        fallos.append("token invalido")
        return 1

    # --- 3) ¿el bot ve mensajes? ---
    print("\n3) MENSAJES PENDIENTES (getUpdates)")
    ok, res = _api(tok, "getUpdates")
    if not ok:
        print(f"  fallo -> {res}")
        fallos.append("getUpdates fallo")
    else:
        chats = {}
        for u in res:
            m = u.get("message") or u.get("channel_post") or {}
            c = m.get("chat") or {}
            if c.get("id") is not None:
                chats[c["id"]] = c.get("type", "?")
        print(f"  {len(res)} actualizacion(es) pendiente(s), {len(chats)} chat(s) distinto(s)")
        if not res:
            print("  -> El bot no ha recibido NADA. Escribele /estado por privado")
            print("     (y pulsa START si es la primera vez). Si usas un GRUPO,")
            print("     el bot debe ser ADMIN. En un CANAL los bots no leen comandos:")
            print("     usa chat privado o grupo.")
        if a.local:
            print("\n  chat_id disponibles (usa uno de estos como TELEGRAM_CHAT_ID):")
            for cid, tipo in chats.items():
                print(f"     {cid}   (tipo: {tipo})")
        elif chats:
            print("  (los chat_id no se imprimen: este log es publico. Usa --local)")

    # --- 4) ¿se puede ENVIAR? ---
    print("\n4) ENVIO DE PRUEBA (sendMessage)")
    if not chat:
        print("  sin CHAT_ID, no se intenta")
        fallos.append("sin chat_id")
    else:
        ok, res = _api(tok, "sendMessage",
                       {"chat_id": chat, "text": "✅ Diagnostico: el canal funciona."})
        if ok:
            print("  ENVIADO. Mira tu Telegram: deberias tener el mensaje.")
        else:
            print(f"  FALLO -> {res}")
            if "chat not found" in str(res).lower():
                print("  -> El CHAT_ID no existe para este bot. Causas tipicas:")
                print("     · es el id de otro bot o de otra cuenta")
                print("     · nunca has iniciado la conversacion (pulsa START)")
                print("     · falta el prefijo -100 en un canal/supergrupo")
            elif "bot was blocked" in str(res).lower():
                print("  -> Has bloqueado al bot. Desbloquealo en Telegram.")
            elif "not enough rights" in str(res).lower():
                print("  -> El bot no es admin del canal/grupo.")
            fallos.append("envio fallido")

    print("\n" + "=" * 62)
    if fallos:
        print(f"  RESULTADO: FALLA ({', '.join(fallos)})")
        print("=" * 62)
        return 1
    print("  RESULTADO: CANAL OK")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
