# Experimentos — WSC Simulation Challenge 2026

Todo lo que hay en esta carpeta son **archivos nuevos**. El simulador no se toca:
`main.py`, `simulation_model/`, `scenario_builders/`, `config/` y los CSV de
`Input/` quedan exactamente como vinieron. Lo único que se modifica del código
original es `response_strategies/user_strategy.py`, que es lo que permite el
enunciado.

## Qué hay

| Archivo | Para qué |
|---|---|
| `run_sim.py` | Corre **una** simulación sin abrir el dashboard y escribe un `summary.json` con el ATT y los KPIs. Es la unidad de medida de todo lo demás. |
| `run_batch.py` | Corre varias semillas en paralelo y reporta la media y la **desviación entre semillas** (el piso de ruido). |
| `inspect_routing.py` | No corre simulación: muestra qué ruta elige cada estrategia para cada par origen-destino, con el costo desglosado en horas. Es la herramienta de diagnóstico. |
| `ml_optimizer.py` | Optimización bayesiana (Optuna) de los parámetros de la estrategia, con semillas fijas para buscar y semillas nuevas para validar. |
| `../analysis/analyze_results.py` | Lee los CSV de una corrida y saca el reporte de ATT, congestión por puerto, utilización de rutas y flujos OD. |

## Cómo se mide

El número oficial es el `OverallMean` que el simulador escribe en
`ATT_By_Statistics_Interval.csv`: **la media de los ATT de los 72 períodos de 5
días**. `run_sim.py` calcula exactamente eso y lo guarda como
`overall_mean_att`. No se usa el ATT del último período: es un solo período de
cinco días y no representa la corrida.

## Receta

```bash
# 0. entorno
python -m venv .venv
.venv/Scripts/python -m pip install -e ./o2despy
.venv/Scripts/python -m pip install loguru numpy pandas pyjson5 sortedcontainers pytest optuna

# 1. referencia: sólo DefaultStrategy, tres semillas
python experiments/run_batch.py --scenario disruption --seeds 2026 2027 2028 --strategy off --label ref --workers 3

# 2. la misma cosa con la estrategia propia
python experiments/run_batch.py --scenario disruption --seeds 2026 2027 2028 --label user --workers 3

# 3. búsqueda de parámetros (barata primero: horizonte corto, dos semillas)
python experiments/ml_optimizer.py --trials 40 --days 180 --seeds 2026 2027

# 4. finalistas a horizonte completo y semillas que el optimizador nunca vio
python experiments/ml_optimizer.py --validate 3 --validation-seeds 2030 2031 2032 2033 2034 --freeze tuned.json
```

## Parámetros

La estrategia lee sus constantes de `response_strategies/strategy_params.py`.
Se pueden sobreescribir sin tocar el archivo:

* `--params archivo.json` en `run_sim.py` (equivale a `WSC_STRATEGY_PARAMS`),
* `WSC_PARAM_<CLAVE>=valor` como variable de entorno,
* `WSC_STRATEGY_ENABLED=0` apaga la estrategia entera y deja correr sólo
  `DefaultStrategy` — así se produce la corrida de referencia.

**Para la entrega final** los valores ganadores se congelan como los `DEFAULTS`
de `strategy_params.py`. Se entrega la estrategia calibrada, no el optimizador.

## Reglas que se respetan

* Nada de nombres de puerto, IDs de ruta ni fechas escritos a mano. Los puertos
  cerrados y los tramos congestionados se leen de `context.disruption_plans` y
  de `leg.sailing_time_multiplier` en el momento de decidir.
* Todo el costo en la misma unidad (horas). Nunca se suman TEU ni porcentajes a
  horas.
* `create_alternative_service_routes` no crea buques ni legs: devuelve `None` y
  deja que `DefaultStrategy` arme las alternativas, que la ruteadora usa
  automáticamente porque considera toda ruta con buques desplegados.
