# Estrategia y arquitectura — Bot de Acciones Tokenizadas (Bitget)

Documento técnico del motor. Para el uso diario, `LEEME.md`.

---

## 1. El activo: qué son estos perpetuos

Bitget lista perpetuos sobre acciones, ETFs y materias primas dentro del mismo
`productType=usdt-futures` que las cripto. Se distinguen por un campo del
contrato:

```
isRwa: "YES"     -> activo del mundo real (262 contratos)
isRwa: "NO"      -> cripto
```

Es el filtro oficial, no una lista a mano (`bitget.rwa_symbols()`). Pero ese
universo mezcla **cuatro calendarios distintos**, y ahí está la primera trampa:

| Tipo | Ejemplos | Sesión |
|---|---|---|
| Acciones/ETFs US | NVDA, TSLA, AAPL, PLTR | 9:30–16:00 ET |
| ETFs apalancados 3x | SOXL, TQQQ, SQQQ, TZA | 9:30–16:00 ET, pero 3x volátiles |
| Acciones asiáticas | SKHYNIX, SAMSUNG, TENCENT | KOSPI / HK |
| Materias primas | XAU, XAG, CL, NATGAS | casi 24/5 |

El universo de `config.py` se queda **sólo** con acciones US (un calendario) y
materias primas (otro calendario, declarado aparte). Las asiáticas y los
apalancados 3x quedan fuera a propósito: romperían el reloj o el modelo de
riesgo.

### Apalancamiento: no es uniforme

Bitget fija el `maxLever` por activo y varía muchísimo:

| Activo | maxLever |
|---|---|
| NVDA, TSLA, AAPL, MSFT | 100x |
| INTC, MU, SNDK, NFLX | 50x |
| MSTR, CRCL | 25x |
| PLTR, COIN, HOOD, AMD | 20x |
| TENCENT, XIAOMI | 5x |

Pedir más del tope hace que el exchange **rechace la orden**. El motor toma
`min(nuestro_tope, maxLever_real)` en `acciones._lev_tope()`.

---

## 2. El reloj de mercado (`tools/sesion.py`)

La pieza que no existía en el bot de cripto y sin la cual nada funciona.

### El problema, medido

El perpetuo cotiza 24/7; la acción no. NVDA, velas de 1h reales:

```
09:30-10:30 ET   $3.40M   rango 1.67%    <- sesión
15:00-16:00 ET   $1.26M   rango 1.79%    <- sesión
19:00-20:00 ET   $  27K   rango 0.07%    <- fuera
sábado           $10-50K  rango 0.10%    <- fuera
```

Fuera de sesión el precio es una línea plana. Metida en el análisis técnico
produce tres fallos, los tres silenciosos:

1. **ATR aplanado** → stops absurdamente pegados
2. **RSI clavado en 50** → ninguna divergencia real
3. **BREAKOUT falso** → lee 12 h de línea plana nocturna como "consolidación" y
   canta rotura con el primer tick de la apertura

De 1000 velas de 5m descargadas, **766 son ruido**. El efecto medido:

| | sin filtrar | con filtro RTH |
|---|---|---|
| ATR | 0,041% | 0,289% |
| SL implícito (4,5×ATR) | 0,18% | 1,30% |
| Sesgo | `entre-EMAs (no-operar)` | `alcista` |

Por eso `filtrar_rth()` se aplica **antes** de cualquier indicador.

### Hora del Este sin dependencias

`zoneinfo` no sirve: Windows no trae la base de datos IANA y
`ZoneInfo("America/New_York")` lanza `ZoneInfoNotFoundError`. Como el proyecto
es Python puro, el DST va implementado a mano con la regla vigente desde 2007:

- empieza el **2.º domingo de marzo** a las 02:00 locales (07:00 UTC)
- termina el **1.er domingo de noviembre** a las 02:00 locales (06:00 UTC)

Verificado en las fronteras exactas: `06:59 UTC → 01:59 ET (-5)` y
`07:00 UTC → 03:00 ET (-4)`.

### Festivos: calculados, no listados

Los 10 festivos de NYSE se **calculan** para cualquier año (incluido el Viernes
Santo por el algoritmo de Gauss/Butcher), con la regla de observación: sábado →
viernes anterior, domingo → lunes siguiente. Así el bot no caduca.

También calcula las **medias sesiones** (cierre a las 13:00 ET): víspera de
Independencia, viernes tras Acción de Gracias y Nochebuena.

### Fases de la sesión

| Fase | Cuándo | Qué hace el bot |
|---|---|---|
| `cerrado` | fuera de horario | no escanea (ni gasta llamadas a la API) |
| `apertura` | primeros 15 min | forma el rango ORB, **no entra** |
| `sesion` | ventana normal | opera |
| `cierre_cercano` | últimos 30 min | no abre nada nuevo |

---

## 3. Las dos estrategias propias de acciones

### ORB — Opening Range Breakout

Los primeros minutos son una subasta: se cruzan las órdenes acumuladas durante
la noche y el precio va y viene sin dirección. En vez de adivinar, se deja que
se forme el rango (alto/bajo de los primeros `ORB_MINUTOS`) y se opera su
**rotura**.

Filtros que la hacen selectiva:

- **rango < 0,15%** → la acción está muerta, cualquier tick lo rompe: descartada
- **rotura ya extendida > 1 rango** → llegamos tarde: descartada
- **sin pico de volumen** → −10 puntos (el volumen es lo que separa la rotura
  real de la trampa)
- **caduca** a los `ORB_VALIDEZ_MIN` (120 min)

### GAP_FADE

El gap se comporta de dos formas **opuestas** según su tamaño, y confundirlas es
caro:

- **Gap moderado (0,5–2%)** → tiende a **rellenarse**. El cierre de ayer actúa
  de imán. Se opera *en contra* del gap, y sólo con señales de agotamiento
  (RSI extremo o divergencia).
- **Gap grande (>2%)** → hay una noticia detrás. **Continúa**. Ponerse en contra
  de un gap de noticia es de las formas más rápidas de perder. Se opera *a favor*.

Sólo vale en los primeros 90 minutos, y si el gap ya se rellenó no hay operación.

---

## 4. Gestión de riesgo

```
Cuenta         50 $
Margen/op      10% = 5 $
SL             10% del margen = 0,50 $ = 1R = 1% de la cuenta
Lev sugerido   10 / distancia_SL%     (SL 1,25% -> 8x)
TP1/TP2/TP3    1,0R / 1,7R / 2,5R  cerrando 33/33/34%
Tras TP1       SL a break-even
Señal ROJA     margen x0,30 y TP tope 1,7R
```

### El SL, calibrado con datos reales

Los parámetros de cripto (lookback 60, buffer 2,0×ATR, mínimo 4,5×ATR) daban un
stop mediano del **3,35%** en acciones. Medido sobre 18 acciones × 3 momentos de
sesión:

| lookback | buffer | mín ATR | SL mediano | % en rango 0,5–2,0% |
|---|---|---|---|---|
| 60 | 2,0 | 4,5 | 3,35% | 7% ← cripto |
| 36 | 1,5 | 3,0 | 2,48% | 33% |
| 24 | 1,0 | 2,5 | 1,97% | 50% |
| 18 | 0,5 | 1,5 | 1,29% | 65% ← zona buena |

La causa: una acción tiene un ATR de 5m del 0,5–0,7%; mirar 60 velas atrás son
5 horas — casi la sesión entera — y arrastraba el swing de la apertura.
Valores finales: **20 / 0,7 / 2,0**.

### El techo del SL descarta, no recorta

Si el SL estructural supera `SL_MAX_PCT` (2,5%), la señal se **descarta**.
Recortarlo dejaría el stop *por delante* del swing, o sea dentro del ruido: te
barren y encima el swing sigue intacto. Es la peor de las opciones.

### Cuando el tope de apalancamiento se queda corto

Si el `maxLever` del contrato no permite el lev ideal, el riesgo real caería por
debajo de 1R. El motor **sube el margen** (hasta el doble del base) para
mantener 1R. Si aun así no llega, acepta arriesgar **menos** — nunca más.

### Mínimos del exchange

Bitget exige `minTradeUSDT = 5` y una cantidad mínima por contrato. Con 50 $ de
cuenta y señales rojas (margen 1,50 $) la posición puede quedarse por debajo:
el motor lo comprueba y descarta con el motivo explícito en vez de emitir una
señal que el exchange rechazaría.

### Nada de overnight

`PERMITIR_OVERNIGHT = False`. Una posición abierta al sonar la campana pasa la
noche en un mercado de 27 K$/hora y amanece con el gap ya hecho, imposible de
gestionar. `seguimiento.py` avisa 10 min antes del cierre y da la posición por
terminada.

---

## 5. Ficheros

```
config.py              toda la parametrización (universo, riesgo, sesión, ORB)
tools/
  bitget.py            datos públicos de Bitget v2 mix (klines, contratos, tickers)
  sesion.py            reloj de mercado: ET+DST, festivos, filtro RTH, ORB, gap
  ta.py                análisis técnico (EMA, RSI, ATR, MACD, pivotes, FVG)
  estrategias.py       las 9 estrategias
  acciones.py          MOTOR: ensemble, semáforo, gestión de dinero, formato
  replay.py            rebobina el motor a un momento pasado (pruebas y calibración)
  seguimiento.py       vigila lo abierto: TP/SL/break-even/cierre de sesión
  telegram.py          envío y lectura de mensajes
  control.py           comandos por Telegram
  ajustes.py           estado persistente (capital, pausa)
datos/                 posiciones.json, ajustes.json (estado vivo)
```

**El sistema NO ejecuta órdenes.** Sólo propone; tú colocas a mano en Bitget.
