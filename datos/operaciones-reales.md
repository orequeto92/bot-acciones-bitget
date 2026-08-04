# Operaciones reales

Registro de las señales que Brahian ha ejecutado de verdad, con su resultado y
lo que enseñan. Sirve para dos cosas: comprobar si el backtest se parece a la
realidad, y acumular la muestra fuera de muestra que hace falta para decidir si
el sistema tiene ventaja (faltan ~53 sesiones desde el 3-ago-2026).

**Estado de la muestra: 2 operaciones.** No se puede concluir nada todavía.

| # | Fecha | Activo | Lado | Entrada | Salida | R | Comisión |
|---|---|---|---|---|---|---|---|
| 1 | 3-ago-2026 | HOODUSDT | SHORT | 91,46 | 92,24 (SL) | **−1,00** | taker 0,0594% |
| 2 | 4-ago-2026 | MSTRUSDT | SHORT | 95,23 | *abierta* | — | **maker 0,0200%** ✅ |

---

## ✅ RESUELTO: el maker es alcanzable (4-ago-2026)

**Era la pregunta que decidía el proyecto entero.** Toda la rentabilidad calculada
dependía de pagar 0,02% en vez de 0,06%, y no se podía saber desde el backtest:
con velas de 5m se ve si el precio toca tu nivel, pero no si tu orden habría
descansado en el libro.

| Operación | Comisión | Posición | **Tasa** |
|---|---|---|---|
| HOOD, orden límite normal | 0,0356304 | $60,01 | **0,0594% = taker** |
| MSTR, límite + **Post Only** | 0,00895162 | $44,76 | **0,0200% = maker** |

Exacto, al cuarto decimal. **Tres veces más barato.**

Lo que cambia: el escenario realista del sistema pasa de **+8,3%** por trimestre
(`t = 0,46`, indistinguible de cero) a **+25,9%** (`t = 1,47`). Sigue sin ser
estadísticamente concluyente, pero ahora hacen falta **115 sesiones** en total
para saberlo, no los 4,9 años que salían con la configuración a taker.

**Condición para que esto valga: Post Only siempre.** Una orden límite normal
no basta — la #1 se puso con límite y pagó taker igualmente, porque cruzaba el
mercado.

**Falta medir**: cuántas órdenes Post Only rechaza Bitget. El backtest predice
un 9,3% (a 1 bps). Si en la práctica es mucho más, hay que rehacer el cálculo.

---

## #1 — HOODUSDT SHORT (3-ago-2026) — ❌ SL

**Primera señal del bot en producción.**

```
Señal   12:08 ET   entrada límite 91,46 | SL 92,24 (0,86%) | TP1 90,66
        Score 74/100 | Prob 0,51 | VERDE | SMC_REVERSAL + MOMENTUM_SHIFT
        Lev 12x | margen 5,00$ | posición 60,00$ | riesgo 0,52$ (1% cuenta)
Entrada 12:11 ET   orden límite (marketable) -> comisión 0,0356304 USDT
SL      13:56 ET   92,24
Máximo del día 92,46
```

**Resultado: −1,00R = −0,52 $.** Exactamente el riesgo previsto. La gestión de
dinero funcionó como está diseñada.

### Lo que enseñó

**1. Una orden límite no es automáticamente maker.**
Se entró con orden límite y aun así se pagó **0,0594% ≈ taker (0,06%)**. Lo que
decide maker/taker no es el tipo de orden sino si el precio **cruza el mercado**:
una venta al precio o por debajo se ejecuta al instante contra las compras que
ya hay. Fue una límite *marketable*, funcionalmente igual que una de mercado.

La señal daba 91,46 con el mercado en 91,45 —un céntimo por encima, lo justo
para no cruzarse— pero para cuando se colocó (3 minutos después) el precio había
bajado a 91,37 y ese margen ya no valía.

→ **Usar Post Only.** Garantiza maker o rechazo. Sin eso, todo el cálculo de
rentabilidad del proyecto (que depende de pagar 0,02% en vez de 0,06%) no se
sostiene.

**2. La señal caduca en minutos.** El precio se movió 9 céntimos en 3 minutos.
Hay que recalcular el límite contra el precio actual, no contra el de la señal.

**3. El contexto contaba una historia incómoda.** HOOD llevaba **+5,3% en el
día** y el SHORT estaba **4,9% por encima del techo del rango de apertura**
(85,57–87,15). Era una reversión contra un día de tendencia fuerte, el escenario
donde esa estrategia más sufre. Acabó en el máximo del día (92,46).

SMC_REVERSAL fue la mejor estrategia del backtest (+27,84R en 254 operaciones),
así que esto entra dentro de lo esperado: con 53% de aciertos, perder una es
normal. Pero queda anotado por si aparece un patrón — **si las señales muy
alejadas del rango de apertura pierden sistemáticamente**, hay un filtro que
añadir. Con una operación no se puede saber.

**4. El cron de GitHub no disparó.** De ~42 ejecuciones esperadas corrió 1, así
que el seguimiento no vigiló la posición: ni aviso de SL ni de cierre de sesión.
Corregido el mismo día con `tools/servicio.py`.
