# -*- coding: utf-8 -*-
"""
CONFIG - Bot de ACCIONES TOKENIZADAS en Bitget (USDT-M Futures -> pestaña Stock).

Toda la parametrizacion en un solo sitio. Cambia aqui y el motor obedece.

QUE ES ESTO
-----------
El motor viene del "Bot Portero" de cripto (7 estrategias + ensemble + semaforo
+ gestion 33/33/34), pero reconvertido a acciones. Las diferencias de fondo:

  cripto                        |  acciones tokenizadas
  ------------------------------|------------------------------------------
  mercado 24/7                  |  sesion 9:30-16:00 ET, lunes a viernes
  todas las velas valen         |  el 77% de las velas son ruido nocturno
  sin apertura                  |  la apertura es el 35% del volumen del dia
  sin gaps                      |  gap de apertura todos los dias
  apalancamiento uniforme       |  maxLever real va de 5x a 100x por accion
  se puede dormir la posicion   |  dormirla = comerse el gap del dia siguiente

NO OPERA SOLO: propone señales, tu las ejecutas a mano en Bitget.
"""

# ------------------------- FUENTE DE DATOS -------------------------
LIVE_SOURCE   = "bitget_usdtm"        # klines publicos v2 mix, sin API key
EXEC_EXCHANGE = "bitget"              # donde EJECUTAS a mano (el bot NO opera)
TF_SCAN   = "5m"                      # timeframe de escaneo/entrada
TF_FOLLOW = "1m"                      # timeframe de seguimiento de lo abierto
TF_HTF    = "1h"                      # timeframe superior para el sesgo
# Pedimos MUCHAS velas porque despues del filtro RTH solo sobrevive ~1 de cada 4:
# 1000 velas de 5m -> ~234 en sesion -> ~3 dias de mercado. Es el minimo para que
# la EMA200 y la estructura tengan sentido.
KLINES_LIMIT     = 1000
# El 1h sufre el filtro aun mas: solo deja 6 velas por sesion (las de 10:00 a
# 15:00), asi que 500 velas se quedaban en 90 utiles. Se pide el maximo.
KLINES_LIMIT_HTF = 1000

# ------------------------- UNIVERSO -------------------------
# Solo activos que cotizan en la sesion de EE.UU. Nada de acciones asiaticas
# (SAMSUNG, TENCENT, SKHYNIX...) ni ETFs apalancados 3x (SOXL, TQQQ, SQQQ):
# tienen otro calendario u otra volatilidad y romperian el modelo de riesgo.
ACCIONES_US = [
    # megacaps tecnologicas
    "NVDAUSDT", "TSLAUSDT", "AAPLUSDT", "MSFTUSDT", "AMZNUSDT", "GOOGLUSDT", "METAUSDT",
    # semiconductores
    "AVGOUSDT", "TSMUSDT", "ASMLUSDT", "AMDUSDT", "INTCUSDT", "MUUSDT", "SNDKUSDT",
    "WDCUSDT", "MRVLUSDT", "QCOMUSDT", "ARMUSDT", "SMCIUSDT",
    # software / datos / IA
    "PLTRUSDT", "ORCLUSDT", "NFLXUSDT", "CRWVUSDT", "NBISUSDT", "IRENUSDT",
    # proxies de cripto (correlacionan con BTC, se mueven mucho)
    "MSTRUSDT", "COINUSDT", "HOODUSDT", "CRCLUSDT", "BMNRUSDT",
]

MATERIAS_PRIMAS = [
    "XAGUSDT",      # plata
    "CLUSDT",       # petroleo WTI
    "NATGASUSDT",   # gas natural
    "COPPERUSDT",   # cobre
]
# ORO (XAUUSDT) FUERA: no es operable con esta cuenta. La cantidad minima que
# acepta Bitget es 0.01 onzas y el oro cotiza a ~4.070$, o sea 40.74$ por orden:
# el 81% de una cuenta de 50$ en una sola posicion. El motor ya lo descartaba
# solo por el control de minimos, pero dejarlo en el universo era gastar dos
# llamadas a la API cada 5 minutos para nada. Vuelve a meterlo si la cuenta
# pasa de ~400$. Mismo problema parcial con ASML (1.653$): sus señales ROJA
# (margen 1.50$) no llegan al minimo de 16.53$, solo operara las VERDE.

ACTIVOS = ACCIONES_US + MATERIAS_PRIMAS

# Grupos (salen en el campo "Grupo:" de la señal).
GRUPOS = {
    "megacap":    {"NVDAUSDT","TSLAUSDT","AAPLUSDT","MSFTUSDT","AMZNUSDT","GOOGLUSDT","METAUSDT"},
    "semis":      {"AVGOUSDT","TSMUSDT","ASMLUSDT","AMDUSDT","INTCUSDT","MUUSDT","SNDKUSDT",
                   "WDCUSDT","MRVLUSDT","QCOMUSDT","ARMUSDT","SMCIUSDT"},
    "software_ia":{"PLTRUSDT","ORCLUSDT","NFLXUSDT","CRWVUSDT","NBISUSDT","IRENUSDT"},
    "cripto_prox":{"MSTRUSDT","COINUSDT","HOODUSDT","CRCLUSDT","BMNRUSDT"},
    "materias":   set(MATERIAS_PRIMAS) | {"XAUUSDT"},   # XAU sigue siendo materia prima aunque este fuera
}

def grupo_de(sym):
    for g, s in GRUPOS.items():
        if sym in s:
            return g
    return "otros"

def calendario_de(sym):
    """Que reloj de mercado le toca a cada activo (ver tools/sesion.py)."""
    return "COMMODITY" if sym in GRUPOS["materias"] else "US_EQUITY"

# ------------------------- SESION -------------------------
# La apertura (9:30 ET) concentra el 35% del volumen del dia y es donde mas
# ruido hay. No entramos a ciegas: dejamos que se forme el rango y operamos su
# rotura (ORB), que es el estandar de intradia en acciones.
ORB_MINUTOS = 15          # minutos de apertura que forman el rango (15 o 30)
ORB_VALIDEZ_MIN = 120     # tras 2h el rango de apertura ya no manda; deja de operarse

# No abrir nada en los ultimos minutos: no da tiempo a que la operacion respire
# y el cierre trae subastas con mechas absurdas.
NO_ABRIR_ULTIMOS_MIN = 30
# Aviso de cierre forzado antes de la campana. Dormir una posicion abierta es
# regalar el gap de la manana siguiente con el spread de un mercado sin volumen.
CERRAR_ANTES_MIN = 10
PERMITIR_OVERNIGHT = False

# Gap de apertura: por debajo de esto no es un gap, es ruido.
GAP_MIN_PCT = 0.5
GAP_GRANDE_PCT = 2.0      # gap enorme: suele continuar, no rellenar

# ------------------------- UMBRALES DE SEÑAL -------------------------
MIN_SCORE = 60            # score minimo (0-100) para emitir señal
MIN_PROB  = 0.40          # probabilidad estimada minima
THRESHOLD_MODE = "DYNAMIC"  # ajusta el minimo segun volatilidad (ATR%)
ENSEMBLE_BONUS = 6        # pts por cada estrategia extra que confirma

# RECALIBRADO PARA ACCIONES. En cripto el umbral dinamico usaba ATR% de 0.8-3.0,
# pero una accion en 5m (ya filtrada a sesion) tiene un ATR% de ~0.20-0.45. Con
# los cortes de cripto TODAS las acciones caerian siempre en "volatilidad baja"
# y el umbral se relajaria permanentemente. Medido en NVDA: ATR 0.289%.
ATR_PCT_ALTO = 0.90       # por encima: mercado nervioso -> exige mas score
ATR_PCT_BAJO = 0.35       # por debajo: mercado dormido -> puede relajarse

# --- SELECTIVIDAD ---
HTF_BONUS   = 8           # +pts si el 1h acompaña la direccion
HTF_PENALTY = 12          # -pts si el 1h va en contra
EXIGIR_CONFLUENCIA = True # exige >=2 estrategias... salvo una sola muy fuerte:
SINGLE_STRONG_SCORE = 72  #   score minimo de una estrategia SOLA para pasar

# Estrategias activas. ORB y GAP son las dos propias de acciones; el resto viene
# del motor de cripto y funciona igual sobre velas filtradas a sesion.
ESTRATEGIAS = [
    "ORB",              # rotura del rango de apertura (la reina en acciones)
    "GAP_FADE",         # relleno / continuacion del gap de apertura
    "SMC_REVERSAL",
    "BREAKOUT",
    "TREND_PULLBACK",
    "RSI_DIVERGENCE",
    "VP_MEAN_REVERT",
    "LIQUIDITY_GRAB",
    "MOMENTUM_SHIFT",
]

# ------------------------- SEMAFORO (calidad) -------------------------
SEM_VERDE_SCORE = 72
SEM_VERDE_PROB  = 0.50
RIESGO_VERDE = 1.00
RIESGO_ROJA  = 0.30       # las "rojas" arriesgarian un tercio... si se operasen

# NO SE OPERAN LAS ROJAS. Medido sobre 62 sesiones reales (mayo-julio 2026,
# 34 activos, 621 operaciones simuladas):
#
#     🟢 VERDE  233 ops  58.4% aciertos   +45.77R
#     🔴 ROJA   388 ops  45.4% aciertos   -11.29R
#
# Las rojas pierden dinero incluso arriesgando un tercio, y ademas ocupan
# huecos de cartera (max 2 simultaneas) que le quitan el sitio a las verdes:
# al dejar de operarlas, las verdes ejecutadas pasan de 233 a 401 y el
# resultado bruto sube de +34.49R a +70.92R. El mismo patron aparecio en una
# corrida independiente de 10 sesiones, con la misma direccion.
#
# Ponlo en True si quieres volver al comportamiento anterior.
OPERAR_ROJAS = False

# ------------------------- GESTION DE RIESGO / DINERO -------------------------
# CUENTA TOTAL = 50$. Riesgo por operacion = 1% = 0.50$.
#   - Margen por operacion = 10% de la cuenta = 5$
#   - SL = 10% del margen = 0.50$ = 1R = 1% de la cuenta
#   - Apalancamiento sugerido = 10 / distancia_SL%   (SL 1.3% -> 7.7x -> 8x)
#   - TP1 = 1.0R (cierra 33%) -> SL a BREAKEVEN ; TP2 = 1.7R (33%) ; TP3 = 2.5R (34%)
CAPITAL_TOTAL  = 50.0     # $ TOTALES de la cuenta
MARGEN_OP_PCT  = 10.0     # % de la cuenta como MARGEN por operacion (10% = 5$)
SL_PCT_MARGEN  = 10.0     # el SL equivale a este % del margen (=1R)
INTERES_COMPUESTO = True  # recalcula sobre el balance vivo
TP_R = [1.0, 1.7, 2.5]
TP_PARCIAL = [0.33, 0.33, 0.34]
TP_CAP_ROJA_R = 1.7
MOVER_BE_TRAS_TP1 = True

def margen_por_op(capital_total=None):
    """Margen en $ para una operacion = MARGEN_OP_PCT% de la cuenta."""
    return (CAPITAL_TOTAL if capital_total is None else capital_total) * MARGEN_OP_PCT / 100.0

# SL estructural: swing reciente + colchon ATR.
#
# CALIBRADO CON DATOS REALES (18 acciones x 3 momentos de sesion, 1-ago-2026).
# Los valores de cripto (60 / 2.0 / 4.5) daban un SL mediano del 3.35% y solo el
# 7% de los casos caia en un rango operable. Medido:
#
#   look  buffer  minATR |  SL% mediano  | % de casos en 0.5-2.0%
#     60     2.0     4.5 |     3.35%     |        7%   <- cripto
#     36     1.5     3.0 |     2.48%     |       33%
#     24     1.0     2.5 |     1.97%     |       50%
#     18     0.5     1.5 |     1.29%     |       65%   <- zona buena
#
# Una accion tiene un ATR de 5m del 0.5-0.7%: mirar 60 velas atras (5 horas, casi
# la sesion entera) arrastraba el swing de la apertura y estiraba el stop.
SL_LOOKBACK   = 20        # velas de sesion hacia atras (~1h 40m) para el swing
SL_BUFFER_ATR = 0.7       # colchon sobre el swing, en ATR
SL_MIN_ATR    = 2.0       # distancia minima en ATR
# SUELO SUBIDO DE 0.40 A 0.80. El coste de transaccion en R es 2*comision/SL,
# asi que un stop ceñido se come la ventaja: con SL 0.40 el sistema daba -1.41R
# netos y con 0.80 da +8.28R. Barrido sobre 62 sesiones (entrada a mercado,
# taker 0.06%):
#     SL 0.40%  398 ops  52.3%  -1.41R
#     SL 0.80%  340 ops  53.2%  +8.28R   <- optimo, y es un punto INTERIOR
#     SL 1.20%  246 ops  51.2%  +5.30R
#     SL 1.60%  206 ops  49.0%  -1.91R
# Que el optimo caiga dentro del rango y no en un extremo es buena señal, pero
# OJO: sigue estando ajustado sobre los mismos datos con los que se midio.
SL_MIN_PCT    = 0.80      # suelo absoluto de distancia (%)
# TECHO: si el SL estructural se pasa de aqui, la señal se DESCARTA (no se
# recorta). Recortarlo dejaria el stop por delante del swing, o sea dentro del
# ruido: te barren y encima el swing seguia intacto. Mejor no operar.
SL_MAX_PCT    = 2.50

# Apalancamiento maximo de seguridad NUESTRO (perfil moderado). El motor ademas
# respeta el maxLever REAL de cada contrato, que Bitget fija muy distinto por
# activo (NVDA 100x, PLTR 20x, TENCENT 5x): se queda con el menor de los dos.
LEV_MAX_ACCIONES = 15
LEV_MAX_MATERIAS = 10

def lev_max_propio(sym):
    return LEV_MAX_MATERIAS if sym in GRUPOS["materias"] else LEV_MAX_ACCIONES

# ------------------------- MULTIPLICIDAD / COOLDOWN -------------------------
MAX_TRADES_SIMULTANEOS = 2   # con 50$ son 10$ de margen comprometido
MAX_POR_ACTIVO = 1

# TOPE POR SECTOR. El 4-ago-2026 el bot emitio 6 señales de semiconductores
# (ASML, MU, SNDK, QCOM, ARM, INTC) TODAS EN CORTO, en un dia en que el sector
# entero abrio con gap de +3% a +6%. Perdieron 5 de 6: -4.67R. Las otras 9
# señales del dia, repartidas entre sectores, sumaron -1.33R.
#
# Seis cortos de semis a la vez no son seis apuestas: son UNA apuesta -que el
# sector se gire- multiplicada por seis. Cuando las estrategias de reversion
# (SMC_REVERSAL, LIQUIDITY_GRAB) ven "sobrecompra", la ven en todo el sector a
# la vez, porque el sector se mueve junto.
#
# GRUPOS ya existia pero solo se usaba para imprimir el campo "Grupo:".
MAX_POR_GRUPO = 1
COOLDOWN_H = 3               # en una sesion de 6.5h esto deja ~2 intentos por activo
MAX_ROJAS_DIA = 2

# Liquidez minima para que el activo sea operable (volumen 24h en USDT).
# Con posiciones de ~50-100$ sobra, pero por debajo de esto el spread se come el R.
MIN_VOL_24H = 50_000

# ------------------------- ETIQUETA -------------------------
BOT_NOMBRE = "Bot Acciones Bitget"
