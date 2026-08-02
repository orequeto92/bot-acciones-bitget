# Bot de Acciones Tokenizadas — Bitget

Escáner intradía de **acciones tokenizadas y materias primas** en perpetuos de
Bitget (USDT-M Futures → pestaña **Stock / TradFi**). Datos y ejecución, los dos
en **Bitget**. El bot **NO opera**: propone señales completas y tú las colocas.

- **Capital**: 50 $ · **Riesgo**: 1% por operación (0,50 $)
- **Horario**: 9:30–16:00 ET, lunes a viernes (respeta festivos de NYSE)
- **Universo**: 30 acciones/ETFs de EE.UU. + 4 materias primas
- **Sin dependencias**: Python puro, ni `pip install` ni API key

Lee `ESTRATEGIA.md` para el detalle del motor.

---

## Lo que hace distinto a un bot de cripto

Este motor viene del Bot Portero de cripto, pero una acción **no es una cripto**:

| | cripto | acciones tokenizadas |
|---|---|---|
| Mercado | 24/7 | 9:30–16:00 ET, L-V, con festivos |
| Velas útiles | todas | **el 77% son ruido nocturno** |
| Apertura | no existe | concentra el **35%** del volumen del día |
| Gaps | no hay | todos los días |
| Apalancamiento | uniforme | de 5x a 100x **según la acción** |
| Dormir la posición | normal | te comes el gap de mañana |

### El detalle que lo rompía todo

El perpetuo de NVDA en Bitget cotiza 24/7, pero la acción sólo se mueve en
sesión. Medido con velas reales:

| Hora ET (viernes) | Volumen | Rango |
|---|---|---|
| 09:30–10:30 (apertura) | **3,40 M$** | 1,67% |
| 15:00–16:00 (cierre) | 1,26 M$ | 1,79% |
| 19:00–20:00 (noche) | 27 K$ | 0,07% |
| Sábado entero | 10–50 K$/h | 0,10% |

Si metes las velas nocturnas en el análisis técnico, el ATR se aplana y **el
stop sale al 0,18%** — te barren en el primer tick. Por eso todo pasa antes por
`sesion.filtrar_rth()`:

| | sin filtrar | con filtro RTH |
|---|---|---|
| Velas útiles (de 1000) | 1000 | 234 |
| ATR | 0,041% | **0,289%** |
| SL implícito | 0,18% ☠️ | **1,30%** ✅ |
| Sesgo | `entre-EMAs (no-operar)` | `alcista` |

---

## Uso

```bash
# escanear el universo (se autodescarta si el mercado está cerrado)
python tools/acciones.py

# activos concretos
python tools/acciones.py NVDAUSDT TSLAUSDT XAUUSDT

# ver TODAS las señales válidas, no sólo las 2 mejores
python tools/acciones.py --todas

# cambiar el capital TOTAL de la cuenta
python tools/acciones.py --capital 100

# ¿está abierto el mercado?
python tools/sesion.py
```

En Windows, si ves errores de emoji: `set PYTHONIOENCODING=utf-8` antes de ejecutar.

### Probar con el mercado cerrado (fin de semana)

El mercado está abierto 32,5 h de las 168 de la semana. Para no quedarte a
ciegas el resto del tiempo, `replay.py` rebobina el motor a un momento pasado
usando velas reales y **cortando el futuro**:

```bash
python tools/replay.py NVDAUSDT                  # recorre la última sesión entera
python tools/replay.py NVDAUSDT --hora 10:30     # un momento concreto
python tools/replay.py --todos --hora 11:00      # todo el universo a esa hora
```

---

## Cómo leer una señal

- **Cuenta TOTAL = 50 $**. Cada señal trae el **margen** (10% = 5 $), la
  **posición** (margen × lev), la **cantidad** exacta y **cuánto arriesgas** en $.
- **1R = 0,50 $ = 1% de la cuenta.** Es la pérdida si salta el SL.
- **Semáforo 🟢 VERDE** = calidad alta (riesgo x1). **🔴 ROJA** = margen x0,30 y
  TP tope 1,7R.
- **Lev sugerido**: calculado para que el SL sea el 10% del margen. Ya viene
  limitado al **tope real del contrato** (Bitget da 100x en NVDA pero 20x en
  PLTR; pedir de más hace que rechace la orden).
- **TP1/TP2/TP3** en 1,0 / 1,7 / 2,5 R, cerrando **33/33/34%**.
- Tras **TP1** → mueve el **SL a break-even**.
- La señal incluye **rango de apertura**, **gap del día** y **minutos que quedan
  para el cierre**.

### Colocar la operación en Bitget (manual)

1. USDT-M Futures → pestaña **Stock** → busca el símbolo.
2. Ajusta el apalancamiento al que dice la señal (no más: es el tope del contrato).
3. Entrada + SL.
4. Pon TP1/TP2/TP3 a la vez con parciales 33/33/34.
5. Tras TP1, SL a entrada (break-even). Nunca promediar a la baja.
6. **Cierra antes de las 16:00 ET.** El bot te avisa 10 minutos antes.

---

## Las 9 estrategias

Las dos primeras son propias de acciones; las otras siete vienen del motor de
cripto pero alimentadas con velas ya filtradas a sesión.

| Estrategia | Qué busca |
|---|---|
| **ORB** | Rotura del rango de apertura (los primeros 15 min). La reina en acciones |
| **GAP_FADE** | Gap moderado (0,5–2%) → se rellena. Gap grande (>2%) → continúa |
| SMC_REVERSAL | Giro desde zona premium/discount |
| BREAKOUT | Rotura de rango con volumen |
| TREND_PULLBACK | Retroceso a la EMA50 en tendencia |
| RSI_DIVERGENCE | Divergencia confirmada en 1h |
| VP_MEAN_REVERT | Precio muy lejos de su media, con RSI en extremo |
| LIQUIDITY_GRAB | Barrido de máximos/mínimos y cierre de vuelta |
| MOMENTUM_SHIFT | Giro del histograma MACD + momentum 1h |

Una sola estrategia rara vez basta: hace falta **confluencia** (≥2 de acuerdo) o
que una sola sea muy fuerte **y** alineada con el 1h.

---

## Calibración (62 sesiones reales, may–jul 2026)

`tools/backtest.py` recorrió 62 sesiones sobre 34 activos cortando el futuro en
cada paso. Resultados **netos de comisiones**, que es lo único que importa:

| Ejecución | Coste por operación | R neto | En 3 meses |
|---|---|---|---|
| A mercado (taker 0,06%) | 0,174R = **17% de tu riesgo** | +1,31 | +0,66 $ |
| Mixta (entra límite) | 0,087R = 9% | +36,12 | +18,06 $ |
| A límite (maker 0,02%) | 0,058R = 6% | +47,71 | +23,86 $ |

**Entra siempre con orden límite.** El SL medio de este sistema es 0,69%; a
mercado, la comisión se lleva el 17% de cada R y anula toda la ventaja.

### Por qué no se operan las ROJAS

| | ops | aciertos | R total |
|---|---|---|---|
| 🟢 VERDE | 233 | 58,4% | **+45,77R** |
| 🔴 ROJA | 388 | 45,4% | **−11,29R** |

Pierden incluso a un tercio de riesgo, y ocupan huecos de cartera que le quitan
el sitio a las verdes: al dejarlas fuera, las verdes ejecutadas pasan de 233 a
401 y el bruto sube de +34,49R a +70,92R. Lo controla `OPERAR_ROJAS`.

Con `OPERAR_ROJAS = False`, **`MIN_SCORE` deja de tener efecto**: el filtro VERDE
(score ≥72 + prob ≥0,50 + alineada con el 1h) ya es más estricto. Da igual 60
que 70: las mismas 401 operaciones.

### Cuidado con las muestras pequeñas

Una corrida previa de sólo 10 sesiones decía lo contrario en casi todo:

| | 10 sesiones | 62 sesiones |
|---|---|---|
| SMC_REVERSAL | −3,78R (el peor) | **+27,84R (el mejor)** |
| Apertura | −5,35R | +11,75R |
| Confluencia | mejor `False` | mejor `True` |

Si se hubiese "optimizado" con aquello, se habría eliminado la mejor estrategia
del sistema. **No toques umbrales con menos de ~50 sesiones.**

### Lo que el backtest NO sabe

- **Supone que la orden límite siempre se llena.** No es cierto, y las que no se
  llenan suelen ser las que se van sin ti, o sea las ganadoras. El +47,7% real
  será menor.
- Un solo régimen de mercado (3 meses).
- Sin slippage ni spread real: usa el precio de las velas.

## Ajustes de selectividad

Todo en `config.py`:

| Palanca | Efecto | Más señales ↔ Menos |
|---|---|---|
| `EXIGIR_CONFLUENCIA` | Exige ≥2 estrategias | `False` = más |
| `SINGLE_STRONG_SCORE` (72) | Score que necesita una sola estrategia | bajar = más |
| `MIN_SCORE` (60) / `MIN_PROB` (0.40) | Umbral base | bajar = más |
| `SEM_VERDE_SCORE` (72) | Corte 🟢 VERDE vs 🔴 ROJA | — |
| `ORB_MINUTOS` (15) | Minutos que forman el rango de apertura | 30 = más selectivo |
| `SL_MAX_PCT` (2.5) | Si el stop estructural se pasa, **se descarta** la señal | subir = más |
| `ATR_PCT_ALTO/BAJO` | Umbral dinámico según volatilidad | — |

Los parámetros del SL están **calibrados con datos reales** (18 acciones × 3
momentos de sesión), no heredados de cripto: ver la tabla en `config.py`.

---

## Seguimiento

El escaneo (5m) DETECTA entradas; el seguimiento (1m) VIGILA lo abierto y avisa
de TP1/TP2/TP3/SL, del break-even tras TP1 y del **cierre de sesión**.

```bash
# al escanear, registra las señales para vigilarlas
python tools/acciones.py --telegram --registrar

# revisa las posiciones abiertas (una pasada)
python tools/seguimiento.py --telegram

# en un host 24/7: cada 60s
python tools/seguimiento.py --loop --telegram
```

Estado en `datos/posiciones.json`. Una posición se cierra por TP3, por SL o
**por fin de sesión** (`PERMITIR_OVERNIGHT = False`).

---

## Telegram

1. Crea un bot **NUEVO** con @BotFather (no reutilices el de cripto: este
   proyecto va aparte).
2. Crea un canal, añade el bot como admin y saca el `TELEGRAM_CHAT_ID`
   (`https://api.telegram.org/bot<TOKEN>/getUpdates`; los canales privados
   empiezan por `-100`).
3. Prueba local:
   ```bash
   set TELEGRAM_TOKEN=123:ABC    &  set TELEGRAM_CHAT_ID=-100123456789
   python tools/telegram.py "prueba"
   python tools/acciones.py --telegram
   ```
   (o crea `datos/telegram.txt` con dos líneas `TOKEN=...` y `CHAT_ID=...`).

### Comandos

| Comando | Efecto |
|---|---|
| `/sesion` | si el mercado está abierto y cuánto queda |
| `/estado` | capital, activo/pausa, operaciones abiertas |
| `/saldo <n>` | fija el capital TOTAL |
| `/escanear` | fuerza un escaneo (avisa si el mercado está cerrado) |
| `/seguir` | revisa TP/SL/break-even |
| `/activar` / `/parar` | reactiva / pausa el envío automático |
| `/ayuda` | lista de comandos |

```bash
python tools/control.py          # procesa los comandos pendientes
python tools/control.py --loop   # los escucha cada 10s
```

---

## Hosting 24/5

`.github/workflows/acciones.yml` ya está configurado para escanear **sólo en
horario de mercado** (cron 13–21 UTC de lunes a viernes, ventana ancha porque el
cron no entiende de horario de verano; el reloj interno decide de verdad).

⚠️ **Minutos de Actions**: en repo **privado** el plan gratis da 2000 min/mes y
esta cadencia consume más. Si lo quieres 24/5 sin pagar, pon el repo en
**público** — Actions es ilimitado y aquí no hay nada sensible (las credenciales
viven en Secrets, no en el código).

---

## Aviso

Ninguna señal es consejo de inversión. El `Score/Prob` es una heurística de
confluencia calibrable, no un modelo entrenado. Las acciones tokenizadas añaden
riesgos propios: liquidez limitada, spread ancho fuera de sesión y dependencia
del emisor del token. Tú ejecutas y asumes el riesgo.
