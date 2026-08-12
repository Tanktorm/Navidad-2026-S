"""Análisis completo de los CSV de entrada y salida de la simulación.

Uso:
    python analysis/analyze_results.py            # analiza Input/ y Output/
    python analysis/analyze_results.py --top 15   # muestra top 15 en rankings

El script tolera las particularidades del formato de los CSV generados por
``simulation_output_csv_writer.py``:
  * números con separador de miles entre comillas ("1,190"),
  * celdas "-" en las matrices origen-destino (pares OD sin demanda),
  * filas TOTAL al final de las tablas de puertos y rutas,
  * filas de resumen (OverallMean / PeriodStdDev) al final del ATT,
  * porcentajes como texto ("14.25%").

Produce un reporte en consola y guarda las tablas derivadas en
``Output/Analysis/`` para poder graficarlas o inspeccionarlas después.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
INPUT_DIR = PROJECT_ROOT / "Input"
OUTPUT_DIR = PROJECT_ROOT / "Output"
ANALYSIS_DIR = OUTPUT_DIR / "Analysis"
# CSV de ATT del escenario sin disrupción, para comparar. None = buscarlo dentro
# de la carpeta analizada.
BASELINE_PATH = None


# ---------------------------------------------------------------------------
# Carga y limpieza
# ---------------------------------------------------------------------------

def _to_number(value):
    """Convierte celdas como '1,190', '14.25%' o '-' a float (NaN si no aplica)."""
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if text in {"-", ""}:
        return np.nan
    text = text.replace(",", "").rstrip("%")
    try:
        return float(text)
    except ValueError:
        return np.nan


def load_od_matrix(path: Path) -> pd.DataFrame:
    """Carga una matriz origen-destino (primera columna = origen) como floats."""
    df = pd.read_csv(path, index_col=0)
    # apply + map por columna funciona tanto en pandas 1.x/2.x como en 3.x,
    # donde DataFrame.applymap fue eliminado.
    return df.apply(lambda column: column.map(_to_number))


def load_att(path: Path) -> tuple[pd.DataFrame, dict]:
    """Carga ATT_By_Statistics_Interval separando las filas de resumen."""
    df = pd.read_csv(path)
    is_summary = df["PeriodIndex"].isna() | df["PeriodIndex"].astype(str).str.strip().eq("")
    summary_rows = df[is_summary]
    summary = {
        str(row["EndDay"]): _to_number(row["AverageTransportTime"])
        for _, row in summary_rows.iterrows()
    }
    periods = df[~is_summary].copy()
    for column in periods.columns:
        periods[column] = periods[column].map(_to_number)
    return periods.reset_index(drop=True), summary


def load_port_waiting(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df = df[df["Port"] != "TOTAL"].copy()
    for column in df.columns[1:]:
        df[column] = df[column].map(_to_number)
    return df.set_index("Port")


def load_route_utilization(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df = df[df["Route"] != "TOTAL"].copy()
    for column in ["Avg Capacity TEU", "Avg Carried TEU", "Utilization"]:
        df[column] = df[column].map(_to_number)
    return df.set_index("Route")


# ---------------------------------------------------------------------------
# Análisis
# ---------------------------------------------------------------------------

def analyze_att(top: int) -> pd.DataFrame | None:
    att_path = OUTPUT_DIR / "ATT_By_Statistics_Interval.csv"
    baseline_path = BASELINE_PATH or (
        OUTPUT_DIR / "Baseline_ATT_By_Statistics_Interval.csv"
    )
    if not att_path.is_file():
        print("  [omitido] No existe ATT_By_Statistics_Interval.csv")
        return None

    att, att_summary = load_att(att_path)
    _print_header("1. Average Transport Time (ATT) por período")

    mean = att["AverageTransportTime"].mean()
    std = att["AverageTransportTime"].std(ddof=1)
    print(f"  Períodos medidos          : {len(att)}")
    print(f"  ATT promedio              : {mean:.2f} días")
    print(f"  Desviación estándar       : {std:.2f} días")
    print(f"  Mínimo / Máximo           : {att['AverageTransportTime'].min():.2f} / "
          f"{att['AverageTransportTime'].max():.2f} días")
    if att_summary:
        print(f"  (Resumen escrito en el CSV: {att_summary})")

    worst = att.nlargest(min(top, len(att)), "AverageTransportTime")
    print("\n  Peores períodos (mayor ATT):")
    for _, row in worst.iterrows():
        print(f"    Días {row['StartDay']:>4.0f}-{row['EndDay']:>4.0f}: "
              f"{row['AverageTransportTime']:.2f} días")

    if baseline_path.is_file():
        baseline, _ = load_att(baseline_path)
        merged = att.merge(
            baseline,
            on=["PeriodIndex", "StartDay", "EndDay"],
            suffixes=("", "_baseline"),
        )
        merged["Delta"] = (
            merged["AverageTransportTime"] - merged["AverageTransportTime_baseline"]
        )
        merged["DeltaPct"] = 100 * merged["Delta"] / merged["AverageTransportTime_baseline"]
        base_mean = merged["AverageTransportTime_baseline"].mean()
        print("\n  Comparación contra el escenario base (sin disrupción):")
        print(f"    ATT base promedio       : {base_mean:.2f} días")
        print(f"    Delta promedio          : {merged['Delta'].mean():+.2f} días "
              f"({merged['DeltaPct'].mean():+.1f}%)")
        peak = merged.loc[merged["Delta"].idxmax()]
        print(f"    Peor delta              : {peak['Delta']:+.2f} días en días "
              f"{peak['StartDay']:.0f}-{peak['EndDay']:.0f}")
        affected = merged[merged["Delta"] > 2 * merged["Delta"].std(ddof=1)]
        if not affected.empty:
            first, last = affected["StartDay"].min(), affected["EndDay"].max()
            print(f"    Ventana de impacto      : días {first:.0f} a {last:.0f} "
                  f"({len(affected)} períodos con delta > 2σ)")
        merged.to_csv(ANALYSIS_DIR / "ATT_vs_Baseline.csv", index=False)
        return merged

    att.to_csv(ANALYSIS_DIR / "ATT_vs_Baseline.csv", index=False)
    return att


def analyze_port_waiting(top: int) -> None:
    path = OUTPUT_DIR / "Port_Waiting_Statistics.csv"
    if not path.is_file():
        return
    ports = load_port_waiting(path)
    _print_header("2. Congestión por puerto (promedios del período medido)")

    ranked = ports.sort_values("Total Waiting TEU", ascending=False)
    print(f"  TEU total esperando (todos los puertos): "
          f"{ports['Total Waiting TEU'].sum():,.0f}")
    print(f"\n  Top {top} puertos por TEU en espera:")
    print(f"    {'Puerto':<20}{'Origen':>10}{'Transbordo':>12}{'Total':>10}{'Buques':>8}")
    for name, row in ranked.head(top).iterrows():
        print(f"    {name:<20}{row['Origin Waiting TEU']:>10,.0f}"
              f"{row['Transshipment Waiting TEU']:>12,.0f}"
              f"{row['Total Waiting TEU']:>10,.0f}"
              f"{row['Vessels Waiting']:>8.2f}")

    transshipment_hubs = ports[ports["Transshipment Waiting TEU"] > 0]
    if not transshipment_hubs.empty:
        hubs = ", ".join(
            transshipment_hubs.sort_values(
                "Transshipment Waiting TEU", ascending=False
            ).index
        )
        print(f"\n  Puertos que actúan como hub de transbordo: {hubs}")
    ranked.to_csv(ANALYSIS_DIR / "Port_Waiting_Ranked.csv")


def analyze_routes() -> None:
    path = OUTPUT_DIR / "Service_Route_Utilization.csv"
    if not path.is_file():
        return
    routes = load_route_utilization(path)
    _print_header("3. Utilización de rutas de servicio")

    routes = routes.sort_values("Utilization", ascending=False)
    print(f"    {'Ruta':<6}{'Nombre':<32}{'Capacidad':>12}{'Cargado':>10}{'Uso %':>8}")
    for route_id, row in routes.iterrows():
        print(f"    {route_id:<6}{row['Name']:<32}{row['Avg Capacity TEU']:>12,.0f}"
              f"{row['Avg Carried TEU']:>10,.0f}{row['Utilization']:>7.1f}%")
    total_capacity = routes["Avg Capacity TEU"].sum()
    total_carried = routes["Avg Carried TEU"].sum()
    print(f"\n  Utilización global: {100 * total_carried / total_capacity:.2f}% "
          f"({total_carried:,.0f} / {total_capacity:,.0f} TEU)")
    print("  Nota: utilización baja sugiere holgura para redistribuir carga en "
          "una estrategia de respuesta; alta indica rutas cuello de botella.")
    routes.to_csv(ANALYSIS_DIR / "Route_Utilization_Ranked.csv")


def analyze_od_flows(top: int) -> None:
    _print_header("4. Flujos origen-destino (OD)")

    completed_path = OUTPUT_DIR / "Cumulative_Completed_TEU_By_OD.csv"
    transit_path = OUTPUT_DIR / "Average_In_Transit_TEU_By_OD.csv"
    waiting_path = OUTPUT_DIR / "Average_Origin_Waiting_TEU_By_OD.csv"

    frames = {}
    for label, path in [
        ("completed", completed_path),
        ("in_transit", transit_path),
        ("waiting", waiting_path),
    ]:
        if path.is_file():
            frames[label] = load_od_matrix(path)

    if "completed" in frames:
        completed = frames["completed"]
        long = completed.stack().rename("CompletedTEU").reset_index()
        long.columns = ["Origin", "Destination", "CompletedTEU"]
        print(f"  TEU completados en total  : {completed.sum().sum():,.0f}")
        print(f"\n  Top {top} pares OD por TEU completados:")
        for _, row in long.nlargest(top, "CompletedTEU").iterrows():
            print(f"    {row['Origin']:<18}-> {row['Destination']:<18}"
                  f"{row['CompletedTEU']:>10,.0f}")
        long.to_csv(ANALYSIS_DIR / "OD_Flows_Long.csv", index=False)

    demand_path = INPUT_DIR / "demand_matrix.csv"
    if "completed" in frames and demand_path.is_file():
        demand = load_od_matrix(demand_path)
        demand = demand.reindex(
            index=frames["completed"].index, columns=frames["completed"].columns
        )
        # La matriz de demanda es anual; se escala a los días medidos.
        att_periods, _ = load_att(OUTPUT_DIR / "ATT_By_Statistics_Interval.csv") \
            if (OUTPUT_DIR / "ATT_By_Statistics_Interval.csv").is_file() else (None, {})
        measured_days = (
            att_periods["EndDay"].max() if att_periods is not None else 360
        )
        expected = demand * (measured_days / 365.0)
        with np.errstate(divide="ignore", invalid="ignore"):
            service = frames["completed"] / expected
        service_long = service.stack().rename("ServiceLevel").reset_index()
        service_long.columns = ["Origin", "Destination", "ServiceLevel"]
        service_long = service_long[np.isfinite(service_long["ServiceLevel"])]
        overall = frames["completed"].sum().sum() / expected.sum().sum()
        print(f"\n  Throughput relativo global (completado / demanda nominal en "
              f"{measured_days:.0f} días): {100 * overall:.1f}%")
        print("  Nota: el generador de embarques muestrea tamaños estocásticos, "
              "por lo que este ratio puede superar 100%; úsalo para comparar "
              "pares OD entre sí, no como nivel de servicio absoluto.")
        laggards = service_long.nsmallest(top, "ServiceLevel")
        print(f"  Pares OD con menor throughput relativo:")
        for _, row in laggards.iterrows():
            print(f"    {row['Origin']:<18}-> {row['Destination']:<18}"
                  f"{100 * row['ServiceLevel']:>7.1f}%")
        service_long.to_csv(ANALYSIS_DIR / "OD_Service_Level.csv", index=False)

    if "waiting" in frames and "in_transit" in frames:
        waiting_total = frames["waiting"].sum().sum()
        transit_total = frames["in_transit"].sum().sum()
        print(f"\n  TEU promedio esperando en origen : {waiting_total:,.0f}")
        print(f"  TEU promedio en tránsito         : {transit_total:,.0f}")
        by_origin = pd.DataFrame({
            "WaitingTEU": frames["waiting"].sum(axis=1),
            "InTransitTEU": frames["in_transit"].sum(axis=1),
        }).sort_values("WaitingTEU", ascending=False)
        print(f"\n  Top {top} orígenes con más carga esperando:")
        for name, row in by_origin.head(top).iterrows():
            print(f"    {name:<20}espera={row['WaitingTEU']:>8,.0f}  "
                  f"tránsito={row['InTransitTEU']:>8,.0f}")
        by_origin.to_csv(ANALYSIS_DIR / "OD_By_Origin_Summary.csv")


def analyze_vessels() -> None:
    path = OUTPUT_DIR / "Average_Vessel_State_Counts.csv"
    if not path.is_file():
        return
    df = pd.read_csv(path)
    df["Average Count"] = df["Average Count"].map(_to_number)
    _print_header("5. Estados de la flota")
    total = df.loc[df["Metric"].str.startswith("TOTAL"), "Average Count"]
    total = total.iloc[0] if not total.empty else df["Average Count"].sum()
    for _, row in df.iterrows():
        if row["Metric"].startswith("TOTAL"):
            continue
        share = 100 * row["Average Count"] / total if total else 0.0
        print(f"    {row['Metric']:<45}{row['Average Count']:>8.2f}  ({share:.1f}%)")
    print(f"    {'TOTAL':<45}{total:>8.2f}")
    print("  Nota: 'waiting for berth' alto respecto a 'being served' indica "
          "congestión portuaria estructural.")


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

def _print_header(title: str) -> None:
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--top", type=int, default=10,
                        help="cuántas filas mostrar en cada ranking (default: 10)")
    parser.add_argument("--dir", type=Path, default=None,
                        help="carpeta con los CSV a analizar; por defecto Output/. "
                             "Usa Output/runs/<tag> para analizar una corrida concreta.")
    parser.add_argument("--baseline", type=Path, default=None,
                        help="ATT_By_Statistics_Interval.csv de la corrida sin "
                             "disrupción, para la comparación contra base.")
    args = parser.parse_args()

    global OUTPUT_DIR, ANALYSIS_DIR, BASELINE_PATH
    if args.dir is not None:
        OUTPUT_DIR = args.dir.resolve()
        ANALYSIS_DIR = OUTPUT_DIR / "Analysis"
    if args.baseline is not None:
        BASELINE_PATH = args.baseline.resolve()

    if not OUTPUT_DIR.is_dir():
        print(f"No existe el directorio de salida: {OUTPUT_DIR}", file=sys.stderr)
        print("Ejecuta primero la simulación con: python main.py", file=sys.stderr)
        return 1

    ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)

    print("Análisis de resultados de la simulación")
    print(f"  Input : {INPUT_DIR}")
    print(f"  Output: {OUTPUT_DIR}")

    analyze_att(args.top)
    analyze_port_waiting(args.top)
    analyze_routes()
    analyze_od_flows(args.top)
    analyze_vessels()

    print()
    print(f"Tablas derivadas guardadas en: {ANALYSIS_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
