# -*- coding: utf-8 -*-
"""
AJUSTES.PY - Ajustes persistentes que se pueden cambiar EN CALIENTE desde Telegram.
Se guardan en datos/ajustes.json y el motor los lee en cada escaneo.

Campos:
  capital_total : capital TOTAL de la cuenta en $ (None = usa config.CAPITAL_TOTAL)
  activo        : True/False. Si False, el escaneo programado NO envia señales (pausa).
  tg_offset     : ultimo update_id de Telegram procesado (para no repetir comandos)
"""
import os, json

PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "datos", "ajustes.json")
DEFAULT = {"capital_total": None, "activo": True, "tg_offset": 0}

def cargar():
    d = dict(DEFAULT)
    try:
        d.update(json.load(open(PATH, encoding="utf-8")))
    except Exception:
        pass
    return d

def guardar(d):
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    json.dump(d, open(PATH, "w", encoding="utf-8"), indent=2, ensure_ascii=False)

def get(clave, fallback=None):
    return cargar().get(clave, fallback)

def set(clave, valor):
    d = cargar(); d[clave] = valor; guardar(d)
    return d

def capital(config_default):
    """Capital vigente: el guardado en ajustes o, si no hay, el de config."""
    c = get("capital_total")
    return float(c) if c else float(config_default)
