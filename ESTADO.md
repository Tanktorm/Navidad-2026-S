# Estado del proyecto — WSC Simulation Challenge 2026

Bitácora viva del trabajo. Se actualiza cada sesión: qué se sabe, qué se probó,
qué sigue. Pegar este archivo al inicio de una sesión nueva es suficiente para
retomar sin redescubrir nada.

**Última actualización:** 12 de agosto de 2026

---

## 1. Objetivo

Bajar el ATT (Average Transport Time) en el escenario con disrupciones **sin
modificar el simulador**. Sólo se puede editar `response_strategies/user_strategy.py`
y agregar archivos nuevos. No se tocan `main.py`, `simulation_model/`,
`scenario_builders/`, `config/simulation_config.PORT_CONGESTION_MULTIPLIER` ni
los CSV de `Input/`.

## 2. Dónde está todo

| Cosa | Ruta |
|---|---|
| Repo de trabajo | `C:\Users\andre\Documents\Winter 2026\Navidad-2026-S` (remoto `Tanktorm/Navidad-2026-S`) |
| Entorno | `.venv/` dentro del repo — `.venv\Scripts\python.exe` |
| Copia del repo original (solo lectura) | `C:\Users\andre\Documents\Winter 2026\SimulationChallenge2026_Py_Round0` |
| Salidas de corridas | `Output/runs/<tag>/` (ignoradas por git) |

El repo tiene los 157 archivos y la versión de **Ronda 1** del escenario de
disrupción. Los CSV que vienen en `Output/` son de corridas viejas de Ronda 0:
no sirven como referencia, hay que regenerarlos.

Instalación del entorno:

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ./o2despy
.venv/Scripts/python -m pip install loguru numpy pandas pyjson5 sortedcontainers pytest optuna
```

(El `-e ./o2despy` del `requirements.txt` es relativo: hay que instalarlo desde
la raíz del repo o con ruta absoluta, si no falla.)

## 3. Hallazgos verificados

### 3.1 La estrategia del equipo no está en ningún lado

Se revisaron las tres copias del proyecto en la máquina. En todas,
`user_strategy.py` era el **stub original** con los cuatro métodos devolviendo
`None`. No existe `_current_or_previous_weekly_time()` ni ninguna otra función
de la estrategia que produjo el ATT de 21.77.

Consecuencia: los pasos 1–2 de la "Fase A" del briefing (arreglar el
`OverflowError`) no aplican — no hay código roto que arreglar. Y **21.77 no es
un número reproducible desde este repo**; la referencia válida es la corrida de
`DefaultStrategy` medida aquí.

### 3.2 S7 no está roto: es correctamente descartado

`experiments/inspect_routing.py` calcula los headways reales de cada servicio
(tiempo de ciclo ÷ buques desplegados):

| Ruta | Buques | Ciclo | Headway | Espera de embarque (0.5 × headway) |
|---|---|---|---|---|
| S3 | 3 | 8.0 d | 2.68 d | 1.3 d |
| S2 | 2 | 9.8 d | 4.88 d | 2.4 d |
| S4 | 10 | 66.7 d | 6.67 d | 3.3 d |
| S1 | 11 | 78.4 d | 7.13 d | 3.6 d |
| S6 | 3 | 30.1 d | 10.03 d | 5.0 d |
| **S7** | **1** | **15.6 d** | **15.60 d** | **7.8 d** |

Con un costo que cobra la espera por frecuencia, S7 pasa de 20 pares OD (ruteo
por distancia) a **0**. No es un bug: sus tres puertos (Singapore, Colombo,
Jebel Ali) también los sirven S1 y S5, y esperar ~7.8 días al único buque es
peor que cualquier alternativa. **No forzar S7.**

### 3.3 El ruteo por defecto minimiza distancia, no tiempo

`DefaultStrategy._find_shortest_booking_path` corre Dijkstra sobre
`edge.total_distance`. Ignora frecuencia, transbordos y cola. Ahí está la
palanca.

### 3.4 `leg.sailing_time_multiplier` es un atributo vivo

`DisruptionManager` lo pone en el valor del plan mientras la disrupción está
activa y en 1.0 cuando no. La estrategia puede leerlo en el momento de decidir,
así que la congestión se puede **cobrar** en vez de prohibirse, y sin escribir
ningún nombre de puerto ni fecha a mano.

## 4. Lo que está implementado

`response_strategies/user_strategy.py` — shortest path sobre estados
`(puerto, ruta_de_servicio)` con un único costo en horas:

```
costo = sailing + espera_de_embarque + transbordo + cola
```

* `sailing` usa el multiplicador vivo de cada leg;
* `espera_de_embarque` = `WAIT_FRACTION` × headway del servicio;
* `transbordo` = `TRANSFER_BUFFER_HOURS` cada vez que el camino cambia de ruta;
* `cola` = TEU ya reservados para esa combinación **puerto+ruta** convertidos a
  headways extra (`TEU_en_espera / capacidad_por_salida × headway`).

Seguir en el mismo servicio no paga transbordo; cambiarlo sí. Si no encuentra
camino devuelve `None` y deja actuar a `DefaultStrategy`.

Parámetros en `response_strategies/strategy_params.py`, sobreescribibles con
`--params archivo.json`, con `WSC_PARAM_<CLAVE>=valor`, o apagando la estrategia
entera con `WSC_STRATEGY_ENABLED=0`.

Herramientas nuevas (ninguna toca el simulador) — ver `experiments/README.md`:
`run_sim.py`, `run_batch.py`, `inspect_routing.py`, `ml_optimizer.py`,
`analysis/analyze_results.py`.

## 5. Trampas de este entorno (costaron tiempo, no repetirlas)

* **Los procesos lanzados desde una sesión de Claude Code mueren al terminar el
  turno**, incluso con `Start-Process` o con `Win32_Process.Create`. Para
  corridas largas hay que usar una **tarea programada de Windows**
  (`schtasks /create ... /sc ONCE /it` y luego `schtasks /run`), que corre bajo
  el servicio del planificador y sobrevive.
* **Exit code `3221225786` (`0xC000013A`)** = los hijos recibieron Ctrl+C. En
  Windows todos los procesos de una misma consola reciben los eventos de
  control. Se arregla lanzando cada corrida con
  `creationflags=CREATE_NEW_PROCESS_GROUP` (ya está puesto en `run_batch.py` y
  en `ml_optimizer.py`).
* `Start-Process -ArgumentList` con un array rompe las rutas que llevan espacio
  (`Winter 2026`). Pasar un único string con las comillas embebidas.
* `experiments/inspect_routing.py` tiene que importar `simulation_model` **antes**
  que `response_strategies`: los dos paquetes se importan mutuamente y sólo
  funciona si `simulation_model` gana la carrera.

## 6. Cómo se mide

El número oficial es el `OverallMean` de `ATT_By_Statistics_Interval.csv`: **la
media de los ATT de los 72 períodos de 5 días**. `run_sim.py` lo calcula igual y
lo guarda como `overall_mean_att`. **No** usar el ATT del último período (es un
solo período de 5 días).

Antes de creerle nada al optimizador hace falta la **desviación estándar del
ATT entre semillas** con parámetros fijos: cualquier "mejora" menor que ese
número es ruido.

## 7. Qué sigue

1. **[en curso]** Corridas de referencia completas (140 warm-up + 360 días):
   disrupción con `DefaultStrategy` en semillas 2026/2027/2028, y baseline sin
   disrupción. De ahí salen el punto de comparación y la σ entre semillas.
2. Correr la estrategia nueva en las mismas tres semillas y comparar contra esa
   referencia, no contra el 21.77 del informe viejo.
3. Búsqueda con Optuna: fase barata primero (`--days 180`, dos semillas) para
   descartar zonas malas, finalistas a horizonte completo.
4. Validar los 3 mejores con semillas que el optimizador nunca vio (2030–2034).
   Gana el mejor en validación, no en búsqueda.
5. Congelar los valores ganadores como los `DEFAULTS` de `strategy_params.py`.
   **Se entrega la estrategia calibrada, no el optimizador.**

Si tras ~50 trials nada mejora, el problema es la lógica y no los parámetros:
volver a `inspect_routing.py` y mirar qué caminos se están eligiendo.

## 8. Reglas que no se rompen

* Nada de nombres de puerto, IDs de ruta ni fechas escritos a mano.
* Todo el costo en la misma unidad (horas). Nunca sumar TEU ni porcentajes a horas.
* No priorizar asignación de muelles: ese hook casi nunca se llama (hacen falta
  ≥3 buques esperando) y la cola promedio de muelle está muy por debajo de 1.
* No agregar rerouting en tránsito antes de demostrar que el ruteo inicial mejoró.
* Toda comparación con varias semillas. El modelo es determinista dada la
  semilla: repetir una corrida idéntica da el mismo CSV.
