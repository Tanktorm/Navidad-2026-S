# Traspaso — WSC Simulation Challenge 2026

**Para:** una sesión nueva de Claude Code.
**Cómo usarlo:** pégalo entero al empezar. Contiene el acceso, el contexto, todo
lo medido y —sobre todo— **lo que ya está descartado con número**, para no
repetirlo.

Última actualización: 15 de agosto de 2026.

---

## 1. Acceso

### Repositorio

```bash
git clone -b estrategia-tiempo https://github.com/Tanktorm/Navidad-2026-S.git
```

Ya está clonado en `C:\Users\andre\Documents\Winter 2026\Navidad-2026-S`, rama
`estrategia-tiempo`. `main` está intacta con el código original.

La credencial de GitHub ya está guardada en el Administrador de credenciales de
Windows, así que `git push` funciona sin intervención. Si alguna vez vuelve a
pedir autenticación, **no se puede resolver desde la sesión**: hay que pedirle a
Andrés que haga un `git push` desde su propia terminal, que abre el navegador.

### Otros repositorios en la máquina (solo lectura)

| Ruta / URL | Qué es |
|---|---|
| `Winter 2026\SimulationChallenge2026_Py_Round0` | Clon del repo original de `alemanuel18`. Ramas `Round1/Automatizacion`, `Round1/propuesta-Omar`, `Round1/propuesta-Omar0.2` con el trabajo del resto del equipo. **No modificar** — Andrés pidió expresamente solo leerlo. |
| `Winter 2026\W 2026 S\SimulationChallenge2026_Py_Round0` | Copia idéntica de la base. |
| `github.com/Tanktorm/W-2026-S-` | Subida de la base. Idéntica salvo `o2despy/.gitignore`. |

Verificado: los 31 archivos del simulador, la configuración y los CSV de entrada
son **byte a byte idénticos** entre todos ellos, así que los resultados son
comparables entre equipos.

### Entorno

```bash
cd "C:\Users\andre\Documents\Winter 2026\Navidad-2026-S"
python -m venv .venv
.venv/Scripts/python -m pip install -e ./o2despy
.venv/Scripts/python -m pip install loguru numpy pandas pyjson5 sortedcontainers optuna reportlab pypdf pypdfium2
```

El `-e ./o2despy` del `requirements.txt` es relativo: hay que instalarlo desde la
raíz del repo o con ruta absoluta, si no falla.

---

## 2. El problema y la métrica

Reducir el ATT del escenario con disrupciones **sin modificar el simulador**.
Según el Tech Document (capítulo 5.1), *"any modification should be put under the
`response_strategies` folder... changes made in other folders will not be
considered"*. Todo lo que esté fuera de `response_strategies/` **no se evalúa**.

La puntuación oficial (Tech Document 4.3, *"sole basis for scoring and ranking"*)
es la Cumulative Resilience Loss:

```
CRL = suma sobre periodos de (1 - ATT_baseline / ATT_escenario) x dias
```

El simulador **no la calcula**. Está implementada en
`analysis/resilience_loss.py`.

---

## 3. Trampas del entorno (costaron muchas horas, no repetirlas)

**El portátil se suspende a los 20 minutos con batería** (`powercfg` →
`0x4b0` = 1200 s) y mata todas las corridas. Enchufado no. Mató ocho corridas
largas antes de que lo encontrara.

**Los procesos lanzados desde la sesión mueren al terminar el turno**, incluso
con `Start-Process` o `Win32_Process.Create`. Para corridas largas hay que usar
**tareas programadas de Windows**:

```
schtasks /create /tn "NOMBRE" /tr "C:\ruta\wrapper.cmd" /sc ONCE /st 04:00 /sd 01/01/2030 /f /it
schtasks /run /tn "NOMBRE"
```

La fecha lejana evita que la tarea se dispare sola y machaque los logs.

**Exit code `3221225786` (`0xC000013A`)** = los hijos recibieron Ctrl+C. Se
arregla con `creationflags=CREATE_NEW_PROCESS_GROUP` (ya está en `run_batch.py` y
`ml_optimizer.py`).

**`printf` en bash se come `\e` y `\r`** al generar los `.cmd`:
`Navidad-2026-S\experiments\run_sim.py` se convirtió en
`Navidad-2026-Sxperimentsun_sim.py`. Usar heredoc con delimitador entrecomillado
(`<<'EOF'`), nunca `printf`.

**Si el fichero de `--params` no existe, no falla: cae a los valores por
defecto** y la corrida mide otra cosa sin avisar. Dos corridas se perdieron así.
Verificar siempre `params_file` en el `summary.json`.

**PowerShell escribe BOM** y `json.loads` lo rechaza. `strategy_params.py` ya lee
con `utf-8-sig`.

**Al filtrar procesos por `ExecutablePath`**, si es `$null` un `-notlike` devuelve
verdadero y matas lo que querías conservar. Me cargué dos corridas buenas así.

**`experiments/inspect_*.py` debe importar `simulation_model` antes que
`response_strategies`**: los dos paquetes se importan mutuamente.

**Las disrupciones están programadas en `WARM_UP_DAYS + día medido`.** Evaluar el
día 60 sin sumar el calentamiento cae sobre una red limpia y todo parece normal.

---

## 4. Hechos medidos (no repetir el trabajo de obtenerlos)

### 4.1 El suelo del problema

| | Días |
|---|---|
| Suelo físico de navegación (camino más corto sobre los tramos) | **16.96** |
| Lo que navega el Default | **16.96** |
| **Desvío evitable** | **0.00 (0%)** |

**El Default ya navega el camino físicamente más corto en los 380 pares
origen-destino.** Optimizar rutas no tiene margen. Esto está medido con
`experiments/inspect_challenger.py` y con un Dijkstra directo sobre
`context.legs`.

### 4.2 De qué está hecho el ATT

Del log de envíos (`--shipment-log`), escenario baseline, ponderado por TEU:

| Componente | Días |
|---|---|
| Navegación (irreducible) | 16.96 |
| **Exceso sobre el mínimo físico** | **7.40** |
| Duración real media | 24.36 |

El exceso es **constante entre 6 y 9 días sin importar la distancia** — es un
sobrecoste fijo por envío, que es la espera por frecuencia. Los viajes de 0-6
días de navegación tardan **3.2×** su mínimo.

Y **no está concentrado**: el 1% más lento aporta el 2.8% de los TEU-días, el 10%
más lento el 19.5%. No hay una cola pequeña que atacar.

### 4.3 El ruido — el número más importante del proyecto

El **mismo** Default, tres semillas, 360 días:

| Semilla | ATT | CRL |
|---|---|---|
| 2026 | 20.451 | 20.44 |
| 2027 | 20.660 | 23.91 |
| 2028 | 20.768 | 25.68 |
| **σ** | **0.162** | **2.67** |

Con una corrida por configuración hace falta una mejora de **más de 0.32 días**
para afirmar nada. Con tres, 0.19. Con diez, 0.10.

σ(CRL) es el **11.4%** de su media; σ(ATT) el 0.8%. **La CRL es catorce veces más
ruidosa en términos relativos** — sirve para puntuar, no para desarrollar.
Optimizar contra ATT medio de ≥3 semillas y reportar CRL al final.

### 4.4 La propiedad de la CRL que lo explica todo

`1 - B/S` se satura y `1/S` es convexa, así que **a igualdad de media, repartir
una degradación entre muchos períodos cuesta más que concentrarla en pocos**:

| Perfil (media 20.00, baseline 19.00) | CRL |
|---|---|
| Plano: cuatro períodos a 20.0 | 1.000 |
| Concentrado: tres a 19.0 y uno a 23.0 | **0.870** |

Un período al nivel del baseline aporta **cero**. El Default conserva **16 de
72**. Cada estrategia que reoptimiza a todos los envíos los convierte en pérdida.

**Corolario:** cada envío que se toca sin necesidad es un impuesto. Y se ha
verificado la correlación: cuanto mayor el porcentaje de envíos intervenidos,
peor el resultado.

### 4.5 Dónde está el daño de la disrupción

Coste medio **+1.39 d/TEU**. Los 20 pares peores concentran el **40.8%**, y ocho
de los diez peores van a **Los Ángeles**.

Motivo estructural: **Los Ángeles solo lo sirve S4 y su única entrada es el tramo
Kaohsiung → Los Angeles** — el puerto que cierra. Es un nodo hoja colgado de la
disrupción. No hay ruta alternativa porque no existe otro tramo que llegue allí.
**Buena parte del daño es físicamente irreducible.**

### 4.6 El horario real de la red

En `vessel_awaiting_instructions.py`: todos los buques entran al pool en t=0 y
**nunca vuelven**; el primero de cada ruta sale en la próxima ocurrencia de su
`StartDayOfWeek` (de `Input/service_routes.csv`) y los siguientes **uno cada 7
días exactos**.

Como el ciclo no es múltiplo de 7, las salidas no quedan uniformes. Hueco
irregular = `C - 7(n-1)`:

| Ruta | Buques | Ciclo | Huecos reales | `ciclo/buques` (**incorrecto**) |
|---|---|---|---|---|
| S1 | 11 | 78.43 | 7.00 ×10, 8.43 | 7.13 |
| S2 | 2 | 9.76 | 2.76, 7.00 | 4.88 |
| S3 | 3 | 8.03 | 1.03, 1.03, 5.97 | 2.68 |
| S4 | 10 | 66.67 | 3.67, 7.00 ×9 | 6.67 |
| S5 | 9 | 72.16 | 7.00 ×8, **16.16** | 8.02 |
| S6 | 3 | 30.09 | 7.00, 7.00, **16.09** | 10.03 |
| S7 | 1 | 15.60 | 15.60 | 15.60 |

Una línea está balanceada cuando `C/7 = n`. La flota total (39) casi coincide con
la que pediría un servicio semanal limpio (40.1), pero está mal repartida.

La espera media real de una llegada aleatoria es `Σg²/2C`, **no** `C/2n`.

### 4.7 Cosas que no existen en este modelo

- **No hay congestión portuaria.** `PORT_CONGESTION_MULTIPLIER` solo se usa como
  umbral para disparar `select_vessel_for_berth`; **ningún código multiplica el
  tiempo de manipulación**, pese al comentario del config.
- **`select_vessel_for_berth` nunca se dispara.** Requiere
  `buques_esperando ≥ muelles × 3`; colas medidas: Kaohsiung 0.22, el resto ≤0.05.
- **La capacidad nunca ata.** Utilización global 5.05%, máximo 6.87%. Un buque de
  S1 llega 93% vacío. QCR y FCP no se activan jamás.
- **El puerto de transbordo no tiene punto de decisión.**
  `shipment_waiting_for_loading_at_transshipment_port.py` no referencia ninguna
  estrategia. Una vez descargada en un hub, la carga es intocable.

### 4.8 El ATT de 9 del otro equipo

Es un artefacto del calentamiento. **Default puro, sin ninguna estrategia:**

| Warm-up | ATT |
|---|---|
| 10 días | 8.22 |
| 15 días | 10.35 |
| **140 (oficial)** | **20.45** |

Ninguna definición alternativa del ATT da 9: ponderada por TEU solo completados
25.73, sin ponderar 26.65, oficial 20.45. **Todas por encima de 20.**
Y `WARM_UP_DAYS` vive en `config/`, fuera de lo evaluable.

---

## 5. Las ocho estrategias probadas — todas pierden

Semilla 2026, 360 días, contra Default = 20.451 / CRL 20.44.

| # | Estrategia | ATT | CRL | Δ ATT | ¿Supera σ? |
|---|---|---|---|---|---|
| 1 | Ruteo por tiempo esperado (horas) | 20.51 | 21.60 | +0.06 | no |
| 2 | CHALLENGER E10 (del equipo) | 20.45 | 20.44 | +0.00 | no |
| 3 | RESCUE (rescatar carga varada) | 20.66 | 23.77 | +0.21 | borderline |
| 4 | Quirúrgica (solo lo irresoluble) | 20.24 | 10.39* | +0.04 | no |
| 5 | Ruta crítica sobre horario derivado | 20.175 | 10.52* | +0.02 | no |
| 6 | Balanceo de flota (S3→S7) | 20.449 | 12.58* | +0.30 | borderline |
| 7 | Guardián de conexiones | 20.489 | 20.89 | +0.04 / **+0.95**† | **sí, pierde** |
| 8 | **Horario observado** | **21.513** | **37.10** | **+1.06 / +1.33**† | **sí, pierde claro** |

\* medidas a 180 días, no comparables con las de 360.
† primera cifra semilla 2026, segunda semilla 2027.

**El patrón: cuanto mayor el porcentaje de envíos intervenidos, peor el
resultado.** El guardián tocaba el 1.3% y perdía poco; el horario observado toca
el 11.4% y pierde 1.19 días de media.

### Detalle de lo que ya está descartado

**CHALLENGER E10** (`challenger_routing.assign_challenger`): su gate no se abre
nunca. Medido: **0 de 380 pares** en las cinco ventanas. `DefaultStrategy`
descarta los tramos congestionados *antes* de buscar, así que su ruta no puede
cruzarlos. Los propios datos del equipo lo confirman: nueve combinaciones de
parámetros dan 20.4638 hasta el último decimal.

**Balanceo de flota** (`fleet_rebalance.py`): funciona mecánicamente — el
traslado S3→S7 por Singapore se ejecuta el día 2.76 — y el modelo estático
predice 0.19 días de ahorro. La corrida da peor. El donante correcto es S3 (tiene
1.85 buques de sobra), no S5; el puerto correcto es Singapore, que es inicio de
ciclo de ambas. Con S5→Colombo el traslado **nunca ocurre** (58 oportunidades, 7
con el buque vacío, 0 en el puerto correcto).

**Horario observado** (`observed_timetable.py`): el mecanismo funciona — 7,892
pasadas anotadas, 52 paradas, **85% de las consultas respondidas con
observaciones reales**. La deriva de fase quedó eliminada. Y aun así pierde 1.19
días. **La hipótesis de que el problema era la deriva era falsa.**

**Suprimir las rutas alternativas del Default**: peor (CRL 23.85 vs 20.44). Las
alternativas del Default **sí ayudan**.

---

## 6. Qué hay en el repositorio

Todo bajo `response_strategies/` es entregable; `experiments/` y `analysis/` son
herramientas que el simulador no importa.

| Archivo | Qué es |
|---|---|
| `response_strategies/user_strategy.py` | Puerta de entrada. Despacha según `ROUTING_MODE`. |
| `response_strategies/strategy_params.py` | Todos los parámetros. Se sobreescriben con `--params fichero.json`, `WSC_PARAM_<CLAVE>` o `WSC_STRATEGY_ENABLED=0`. |
| `challenger_routing.py` | Modos `challenger`, `rescue`, `surgical`, `unified`. Más `_graphs`, `_windows`, `_default_path`, `_apply` que reutiliza todo lo demás. |
| `timetable_routing.py` | Llegada más temprana sobre el horario (ruta crítica). |
| `observed_timetable.py` | Horario aprendido de la observación. |
| `surgical_boarding.py` | Desvío con umbral de espera y filtro de viaje largo. |
| `connection_guard.py` | Evita bajar la carga a una conexión mala. |
| `fleet_rebalance.py` | Balanceo de líneas. |
| `experiments/run_sim.py` | Una corrida sin dashboard, con `summary.json`. `--shipment-log` exporta la distribución. |
| `experiments/run_batch.py` | Varias semillas en paralelo. |
| `experiments/ml_optimizer.py` | Optuna. **Nunca llegó a correr en serio** — ver §7. |
| `experiments/inspect_routing.py` | Qué ruta elige cada estrategia y por qué. |
| `experiments/inspect_challenger.py` | Verifica si el gate de E10 se abre. |
| `analysis/resilience_loss.py` | **Calcula la CRL.** El simulador no lo hace. |
| `analysis/line_balance.py` | Balanceo de líneas sobre el horario real. |
| `analysis/analyze_results.py` | Análisis de los CSV de salida. |

Modos disponibles en `ROUTING_MODE`: `time`, `challenger`, `rescue`, `foresight`,
`unified`, `surgical`, `boarding`, `timetable`.

### El dashboard en blanco

`Output/Baseline_ATT_By_Statistics_Interval.csv` **no lo genera ningún código**:
viene versionado. `auto_tune.py` del equipo lo borra con
`OUTPUT_DIR.glob("*.csv")`. Se recupera con
`git checkout -- Output/Baseline_ATT_By_Statistics_Interval.csv` y se evita
saltándose los ficheros `Baseline_*` en `clean_root_outputs()`.

---

## 7. Qué NO volver a intentar, y qué queda

### No repetir

1. **Optimizar rutas.** Desvío evitable 0.00%.
2. **Cualquier estrategia que reoptimice todos los envíos.** Ocho intentos, ocho
   derrotas, con correlación clara entre porcentaje intervenido y pérdida.
3. **Prioridad de muelle** (`select_vessel_for_berth`). No se dispara nunca.
4. **QCR, FCP o cualquier cosa basada en capacidad.** No ata: utilización 5%.
5. **El gate de CHALLENGER E10.** Demostrado que no se abre.
6. **Optimizar contra CRL con pocas corridas.** σ = 2.67, el 11.4% de su media.
7. **Cualquier comparación con una sola semilla.** Hace falta >0.32 días para
   verse.
8. **Perseguir el ATT de 9.** Es un warm-up de doce días.

### Lo que queda sin explorar

- **`adjust_bookings_before_cargo_handling`** más allá del guardián de conexiones.
  Es la única interfaz que alcanza carga ya embarcada, y ahí vive el daño: la CRL
  es un eco retardado de una travesía completa (47% de la pérdida está después
  del día 150, y seguía creciendo al final del horizonte).
- **El optimizador de Optuna nunca corrió de verdad.** Está montado con el
  espacio de búsqueda incluyendo las decisiones estructurales, pero se abandonó
  al descubrir que σ hace indistinguible cualquier diferencia por debajo de 0.32
  días. Solo tendría sentido con muchas semillas por ensayo, lo que multiplica el
  coste por tres o cinco.
- **Un ATT bajo puede que no exista.** Es la conclusión honesta tras dos semanas:
  el Default parece ser el óptimo de este problema, y hay tres mediciones
  independientes que lo explican (§4.1, §4.4, §4.5).

### Presupuesto de cómputo

Una corrida de 360 días son **~25 min sola**, ~50 con dos en paralelo, ~100 con
cuatro. Distinguir una mejora de una décima exige diez corridas por
configuración: **quince horas de máquina por estrategia**.

---

## 8. Preferencias de Andrés

Avanzar sin pedir permiso y reportar al final. No preguntar por decisiones de
rutina. Sí avisar —sin pedir permiso— cuando algo sea irreversible o cuando la
sesión esté bloqueada por algo que solo él puede hacer (autenticar contra GitHub,
enchufar el portátil).

Prefiere que se le diga claramente cuando algo no funciona, en vez de adornarlo.
