# -*- coding: utf-8 -*-
"""
BITGET.PY - Descarga de datos publicos de Bitget USDT-M Futures (v2 mix).
Sin API key: solo endpoints de mercado. Python puro (urllib), sin dependencias.

Las acciones tokenizadas viven en el MISMO productType que las cripto
(usdt-futures) y se distinguen por el campo `isRwa == "YES"` del contrato.
En la app son: USDT-M Futures -> pestaña Stock / TradFi.

Expone:
  klines(symbol, interval, limit)   -> velas [ts,o,h,l,c,volBase,volQuote] ASC
  contratos()                       -> {symbol: dict} de TODOS los contratos (cacheado)
  rwa_symbols()                     -> set de simbolos con isRwa == YES
  contrato(symbol)                  -> dict de un contrato (maxLever, minTradeNum...)
  max_lev(symbol)                   -> apalancamiento maximo REAL que permite Bitget
  tickers()                         -> {symbol: dict} con lastPr / usdtVolume 24h
  top_rwa(n)                        -> n simbolos RWA con mas volumen 24h

FORMATO DE VELA: index 0=ts(ms) 2=high 3=low 4=close 5=volumen base.
Coincide con lo que espera ta.compute(), asi que es intercambiable con el
binance.py del motor original sin tocar el analisis tecnico.
"""
import json, os, time, urllib.request, urllib.error

BASE = "https://api.bitget.com"
PRODUCT = "usdt-futures"             # productType de la API v2 (USDT-M)
_HDR = {"User-Agent": "Mozilla/5.0 (Trading-Acciones-Bitget)"}

# Bitget usa mayusculas para los TF de 1h en adelante. Traducimos el estilo
# "binance" (1h, 4h, 1d) al de Bitget (1H, 4H, 1D) para no tener que cambiar
# los timeframes escritos en config.py.
_GRAN = {
    "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1H", "2h": "2H", "4h": "4H", "6h": "6H", "12h": "12H",
    "1d": "1D", "3d": "3D", "1w": "1W",
}


def _get(path, params=None, retries=3, timeout=20):
    url = BASE + path
    if params:
        url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=_HDR)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read().decode("utf-8"))
            if d.get("code") != "00000":
                raise RuntimeError(f"Bitget code={d.get('code')} msg={d.get('msg')}")
            return d.get("data")
        except (urllib.error.URLError, TimeoutError, RuntimeError, ValueError) as e:
            last = e
            time.sleep(1.2 * (i + 1))
    raise RuntimeError(f"Bitget GET fallo {path}: {last}")


# ----------------------------- velas -----------------------------
def klines(symbol, interval="5m", limit=300):
    """Velas ascendentes (la mas antigua primero), como Binance.

    OJO: Bitget devuelve la vela EN CURSO como ultimo elemento. El motor la usa
    para el precio actual, igual que hacia con Binance.
    """
    gran = _GRAN.get(interval, interval)
    raw = _get("/api/v2/mix/market/candles",
               {"symbol": symbol, "productType": PRODUCT,
                "granularity": gran, "limit": min(int(limit), 1000)})
    velas = [c for c in (raw or []) if c and len(c) >= 6]
    velas.sort(key=lambda c: int(c[0]))
    return velas


def historia(symbol, interval="5m", dias=14, cache_dir=None, verbose=False):
    """Historial largo paginando hacia atras con /history-candles (200 por peticion).

    klines() solo da 1000 velas, que en 5m son ~3 sesiones utiles: poco para
    calibrar nada. Esto baja semanas. Se cachea en disco porque el historial
    pasado NO cambia y una calibracion se repite muchas veces.
    """
    gran = _GRAN.get(interval, interval)
    ahora_ms = int(time.time() * 1000)
    desde_ms = ahora_ms - dias * 86400_000

    cache_path = None
    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
        cache_path = os.path.join(cache_dir, f"{symbol}-{interval}-{dias}d.json")
        if os.path.exists(cache_path) and time.time() - os.path.getmtime(cache_path) < 6 * 3600:
            try:
                with open(cache_path, encoding="utf-8") as f:
                    return json.load(f)
            except (OSError, ValueError):
                pass

    velas, end = {}, ahora_ms
    for _ in range(200):                       # tope de seguridad
        raw = _get("/api/v2/mix/market/history-candles",
                   {"symbol": symbol, "productType": PRODUCT, "granularity": gran,
                    "limit": 200, "endTime": end})
        lote = [c for c in (raw or []) if c and len(c) >= 6]
        if not lote:
            break
        for c in lote:
            velas[int(c[0])] = c
        mas_antigua = min(int(c[0]) for c in lote)
        if mas_antigua <= desde_ms or len(lote) < 200:
            break
        end = mas_antigua
        time.sleep(0.12)                       # cortesia con la API
    out = [velas[t] for t in sorted(velas) if t >= desde_ms]
    if verbose:
        print(f"    {symbol} {interval}: {len(out)} velas ({dias}d)")
    if cache_path:
        try:
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(out, f)
        except OSError:
            pass
    return out


# ----------------------------- contratos -----------------------------
_cache_contratos = {"t": 0, "d": None}

def contratos(max_edad_s=900):
    """{symbol: contrato}. Cacheado 15 min: en un escaneo de 30 activos no tiene
    sentido pedir la lista de 733 contratos una vez por activo."""
    ahora = time.time()
    if _cache_contratos["d"] and ahora - _cache_contratos["t"] < max_edad_s:
        return _cache_contratos["d"]
    data = _get("/api/v2/mix/market/contracts", {"productType": PRODUCT})
    d = {c["symbol"]: c for c in (data or [])}
    _cache_contratos.update(t=ahora, d=d)
    return d


def contrato(symbol):
    return contratos().get(symbol.upper())


def rwa_symbols():
    """Simbolos de activos del mundo real (acciones, ETFs, materias primas).
    Bitget los marca con isRwa=YES; es el filtro oficial, no una lista a mano."""
    return {s for s, c in contratos().items() if c.get("isRwa") == "YES"}


def max_lev(symbol, defecto=20):
    """Apalancamiento maximo REAL del simbolo. En acciones varia muchisimo
    (NVDA 100x, PLTR 20x, TENCENT 5x): pedir mas del tope hace que Bitget
    rechace la orden, asi que el motor tiene que respetarlo."""
    c = contrato(symbol)
    try:
        return int(float(c["maxLever"])) if c else defecto
    except (KeyError, TypeError, ValueError):
        return defecto


def min_qty(symbol, defecto=0.01):
    c = contrato(symbol)
    try:
        return float(c["minTradeNum"]) if c else defecto
    except (KeyError, TypeError, ValueError):
        return defecto


def decimales_precio(symbol, defecto=2):
    c = contrato(symbol)
    try:
        return int(float(c["pricePlace"])) if c else defecto
    except (KeyError, TypeError, ValueError):
        return defecto


def decimales_qty(symbol, defecto=2):
    c = contrato(symbol)
    try:
        return int(float(c["volumePlace"])) if c else defecto
    except (KeyError, TypeError, ValueError):
        return defecto


def min_notional(symbol, defecto=5.0):
    """Minimo en USDT que Bitget acepta por orden."""
    c = contrato(symbol)
    try:
        return float(c["minTradeUSDT"]) if c else defecto
    except (KeyError, TypeError, ValueError):
        return defecto


def negociable(symbol):
    """True si el contrato existe y esta operativo (symbolStatus == normal)."""
    c = contrato(symbol)
    return bool(c) and c.get("symbolStatus") == "normal"


# ----------------------------- tickers -----------------------------
def tickers():
    """{symbol: ticker}. usdtVolume = volumen 24h en USDT."""
    data = _get("/api/v2/mix/market/tickers", {"productType": PRODUCT})
    return {t["symbol"]: t for t in (data or [])}


def volumen_24h(symbol, tk=None):
    t = (tk or tickers()).get(symbol, {})
    try:
        return float(t.get("usdtVolume") or 0)
    except (TypeError, ValueError):
        return 0.0


def top_rwa(n=30, excluir=()):
    """Top-n activos RWA por volumen 24h. Util para revisar el universo, pero
    OJO: mezcla calendarios (acciones US, asiaticas, materias primas). Para
    operar usa config.ACTIVOS, que ya viene filtrado por sesion."""
    rwa = rwa_symbols()
    tk = tickers()
    filas = [(volumen_24h(s, tk), s) for s in rwa
             if s not in excluir and negociable(s)]
    filas.sort(reverse=True)
    return [s for _, s in filas[:n]]


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "top":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 30
        tk = tickers()
        for s in top_rwa(n):
            print(f"  {s:<14} vol24h ${volumen_24h(s, tk):>12,.0f}  maxLev {max_lev(s):>3}x")
    elif len(sys.argv) > 1:
        sym = sys.argv[1].upper()
        c = contrato(sym)
        print(f"{sym}: RWA={c.get('isRwa')} maxLev={c.get('maxLever')} "
              f"minQty={c.get('minTradeNum')} minUSDT={c.get('minTradeUSDT')} "
              f"estado={c.get('symbolStatus')}")
        for k in klines(sym, "5m", 3):
            print(f"  ts={k[0]} cierre={k[4]} vol={k[5]}")
    else:
        print(f"Contratos RWA en Bitget: {len(rwa_symbols())}")
        print("Uso: python tools/bitget.py NVDAUSDT   |   python tools/bitget.py top 30")
