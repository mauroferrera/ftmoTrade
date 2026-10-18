# Alcance congelado — FTMO Copilot Dashboard

> Este fichero es un CONTRATO. Ampliarlo exige una entrada nueva en
> `.agent/DECISIONS.md` con su fecha. La lista de "no entra" no se toca por
> comodidad a mitad de una fase.

## Qué es

Un dashboard de trading para pasar el **challenge FTMO 2-Step de $25.000**. Un
copiloto analiza **EURUSD + XAUUSD**, marca soportes y resistencias en el
gráfico, avisa de setups y registra todo para el post-mortem.

## Decisiones fijadas

| Tema | Decisión |
|------|----------|
| Copiloto | Determinista (niveles S/R) **+** chat IA |
| Alcance | Análisis + ejecución semiautomática (**avisa y tú confirmas**) |
| Motor de reglas FTMO | Sí, desde el inicio |
| Proveedor IA | Google Gemini (vía litellm) |
| Score | SMC simplificado + killzone |
| Pares | EURUSD + XAUUSD |
| Símbolos/timeframe | M15 por defecto |
| Riesgo | 0.5 % del capital fijo por operación; máx. 3 operaciones/día |

## Reglas FTMO (perfil por defecto)

`ftmo_2step_25k`: producto 2-Step, capital $25.000.

- Drawdown diario: **5 %** ($1.250). Corte interno propuesto: **4 %**.
- Drawdown total: **10 %** ($2.500). Corte interno propuesto: **7 %**.
- Objetivo: **10 %** ($2.500).
- Mínimo **4 días de trading**.
- El sizing se calcula sobre el **capital fijo**, no sobre el balance vivo.

## NO entra (por ahora)

Estas piezas existen en el proyecto de referencia (`Trinity_proyect`) y **no se
portan** sin decisión nueva:

- COT / sentimiento macro.
- CVD real / order flow por ticks.
- DXY / correlaciones.
- Alerting de noticias macro (FTMO no lo exige en evaluación; el puerto queda
  preparado pero apagado).
- Múltiples estrategias (CTA, mean-reversion VWAP, datasim, sandbox).
- Mercados: B3 (WIN/WDO), cripto, futuros CME.
- Databento, ccxt, Binance, yfinance.
- Auto-ejecución sin confirmación humana.
- Ejecución directa desde el agente IA.

## Persistencia

12 tablas (recorte del proyecto de referencia): fuera `cot_reports`,
`chart_alerts` y `trade_excursions`.

## Cómo se sabe que una fase está terminada

1. `pytest -q` en verde.
2. Una demo concreta y observable (`python app.py` + algo que se pueda ver).
3. `core/` sigue puro (`tests/unit/test_core_purity.py` en verde).
4. Estado actualizado en `.agent/PROJECT_STATE.json` y decisiones en
   `.agent/DECISIONS.md`.
