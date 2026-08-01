# -*- coding: utf-8 -*-
"""
TELEGRAM.PY - Envio de señales a Telegram. Python puro (urllib), sin dependencias.

Credenciales (NO se ponen en el codigo):
  - Variable de entorno TELEGRAM_TOKEN   (token de @BotFather)
  - Variable de entorno TELEGRAM_CHAT_ID (id del chat/canal; para canal usa @nombre_canal
                                          o el id numerico -100xxxxxxxxxx)
  Alternativa local: crea un archivo  datos/telegram.txt  con dos lineas:
      TOKEN=123456:ABC...
      CHAT_ID=-1001234567890

Como sacar el CHAT_ID:
  1) Crea el bot con @BotFather -> te da el TOKEN.
  2) Anade el bot a tu canal/grupo como ADMIN (o escribele por privado).
  3) Manda un mensaje y abre:  https://api.telegram.org/bot<TOKEN>/getUpdates
     El chat.id sale ahi. Para canales privados empieza por -100.

USO:
  python tools/telegram.py "mensaje de prueba"
"""
import os, json, urllib.request, urllib.parse, urllib.error

_CRED_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "datos", "telegram.txt")

def _cred():
    tok = os.environ.get("TELEGRAM_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if tok and chat:
        return tok, chat
    # fallback a archivo local
    try:
        d = {}
        for line in open(_CRED_FILE, encoding="utf-8"):
            if "=" in line:
                k, v = line.strip().split("=", 1)
                d[k.strip().upper()] = v.strip()
        return d.get("TOKEN") or tok, d.get("CHAT_ID") or chat
    except Exception:
        return tok, chat

def configurado():
    tok, chat = _cred()
    return bool(tok and chat)

def enviar(texto, chat_id=None, disable_preview=True):
    """Envia un mensaje. Devuelve True/False. No lanza si falla (lo reporta)."""
    tok, chat = _cred()
    chat = chat_id or chat
    if not tok or not chat:
        print("  [telegram] sin credenciales (TELEGRAM_TOKEN / TELEGRAM_CHAT_ID)")
        return False
    url = f"https://api.telegram.org/bot{tok}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat,
        "text": texto,
        "disable_web_page_preview": "true" if disable_preview else "false",
    }).encode()
    try:
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=15) as r:
            res = json.loads(r.read().decode())
            return bool(res.get("ok"))
    except urllib.error.HTTPError as e:
        print(f"  [telegram] HTTP {e.code}: {e.read().decode()[:200]}")
        return False
    except Exception as e:
        print(f"  [telegram] error: {e}")
        return False

def get_updates(offset=0, timeout=0):
    """Lee los mensajes nuevos que le han enviado al bot (comandos).
    Devuelve la lista 'result' de la API (vacia si no hay o falla)."""
    tok, _ = _cred()
    if not tok:
        return []
    url = f"https://api.telegram.org/bot{tok}/getUpdates?timeout={timeout}"
    if offset:
        url += f"&offset={offset}"
    try:
        with urllib.request.urlopen(url, timeout=max(15, timeout + 5)) as r:
            return json.loads(r.read().decode()).get("result", [])
    except Exception as e:
        print(f"  [telegram] getUpdates error: {e}")
        return []

if __name__ == "__main__":
    import sys
    msg = sys.argv[1] if len(sys.argv) > 1 else "✅ Prueba de Bot Portero (replica) — conexion OK."
    print("enviado" if enviar(msg) else "fallo (revisa credenciales)")
