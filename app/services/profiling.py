from typing import Any, Dict, List
from google.cloud import bigquery
import logging
import base64
import datetime
import concurrent.futures
import json
from decimal import Decimal

from app.adapters.bq_reader import get_partition_field, get_max_partition

logger = logging.getLogger(__name__)

MAX_COLUMNS_TO_PROFILE = 100
BQ_QUERY_TIMEOUT = 120


def _is_missing(value: Any) -> bool:
    """Considera ausente None o lista vacía (REPEATED sin elementos)."""
    if value is None:
        return True
    if isinstance(value, list) and len(value) == 0:
        return True
    return False


def _normalize_for_hash(value: Any) -> Any:
    """
    Convierte cualquier valor a algo hasheable para comparación/deduplicación.
    """
    if isinstance(value, dict):
        return tuple(sorted((k, _normalize_for_hash(v)) for k, v in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_normalize_for_hash(v) for v in value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime.datetime, datetime.date)):
        if isinstance(value, datetime.datetime) and value.tzinfo:
            return value.astimezone(datetime.timezone.utc).isoformat()
        return value.isoformat()
    return value


def _to_display(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    except TypeError:
        norm = _normalize_for_hash(value)
        return json.dumps(norm, ensure_ascii=False, separators=(",", ":"), default=str)


def _get_partition_bq_type(
    table: bigquery.Table, partition_field_name: str | None
) -> str | None:
    """Obtiene el tipo BigQuery del campo de partición directamente desde el schema."""
    if not partition_field_name:
        return None
    if partition_field_name == "_PARTITIONDATE":
        return "DATE"
    if partition_field_name == "_PARTITIONTIME":
        return "TIMESTAMP"
    for f in table.schema:
        if f.name == partition_field_name:
            return f.field_type.upper()
    return None


def build_profile(
    table: bigquery.Table,
    bq_client: bigquery.Client,
    max_examples: int = 10,
    sample_percent: int = 5,
) -> Dict[str, Dict]:
    """
    Calcula estadísticas y obtiene ejemplos delegando el procesamiento a BigQuery.
    """
    fq_table = f"{table.project}.{table.dataset_id}.{table.table_id}"

    partition_field = get_partition_field(table)
    partition_bq_type = _get_partition_bq_type(table, partition_field)
    max_partition = None
    if partition_field and partition_bq_type in ("TIMESTAMP", "DATE", "DATETIME"):
        max_partition = get_max_partition(bq_client, fq_table, partition_field)

    # 1. Construir agregaciones por columna
    stat_parts = []
    profiled_column_names = []

    for f in table.schema[:MAX_COLUMNS_TO_PROFILE]:
        # Filtrar solo campos simples (no STRUCT, no ARRAY/REPEATED, no JSON/GEOGRAPHY)
        if f.mode == "REPEATED" or f.field_type in ("RECORD", "JSON", "GEOGRAPHY"):
            continue

        name = f.name
        profiled_column_names.append(name)
        stat_parts.append(f"""
            STRUCT(
                COUNTIF(`{name}` IS NULL) as null_count,
                APPROX_COUNT_DISTINCT(`{name}`) as dist_count,
                COUNT(`{name}`) as non_null_count,
                ARRAY_AGG(`{name}` IGNORE NULLS LIMIT {max_examples * 5}) as examples
            ) as `{name}`
        """)

    stats_select = ",\n".join(stat_parts)

    # 2. Manejo de Partición y Muestreo
    where_clause = "1=1"
    job_config = None
    if (
        partition_field
        and partition_bq_type in ("TIMESTAMP", "DATE", "DATETIME")
        and max_partition
    ):
        # Optimizamos: Filtramos por la partición exacta o el mes actual para activar el "Partition Pruning"
        where_clause = f"DATE_TRUNC(DATE(`{partition_field}`), MONTH) = DATE_TRUNC(@max_partition, MONTH)".strip()
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("max_partition", "DATE", max_partition)
            ]
        )

    def run_query(use_sample: bool):
        sample = f"TABLESAMPLE SYSTEM ({sample_percent} PERCENT)" if use_sample else ""
        query = f"SELECT COUNT(*) as total_rows, {stats_select} FROM `{fq_table}` {sample} WHERE {where_clause}"
        return bq_client.query(query, job_config=job_config).result(
            timeout=BQ_QUERY_TIMEOUT
        )

    # Intentar con muestreo, si falla o da 0, reintentar sin muestreo (para tablas pequeñas)
    row = None
    total_rows = 0

    try:
        try:
            # Intento 1: Estadísticas con muestreo sobre la partición
            results = run_query(use_sample=True)
            row = next(results)
            total_rows = row.total_rows
            # Si el muestreo devuelve 0 pero la tabla tiene datos, reintentar sin muestreo
            if total_rows == 0 and getattr(table, "num_rows", 0) > 0:
                results = run_query(use_sample=False)
                row = next(results)
                total_rows = row.total_rows
        except (concurrent.futures.TimeoutError, TimeoutError):
            logger.warning(
                f"Timeout en muestreo para {fq_table}. Intentando sin sample sobre partición."
            )
            results = run_query(use_sample=False)
            row = next(results)
            total_rows = row.total_rows
        except Exception as e:
            logger.warning(
                f"Error en muestreo para {fq_table}: {e}. Reintentando sin sample."
            )
            results = run_query(use_sample=False)
            row = next(results)
            total_rows = row.total_rows

    except Exception as e:
        logger.error(
            f"No se pudieron obtener estadísticas para {fq_table}: {e}. Ejecutando fallback de ejemplos."
        )
        try:
            fallback_query = (
                f"SELECT * FROM `{fq_table}` WHERE {where_clause} LIMIT {max_examples}"
            )
            fallback_results = bq_client.query(
                fallback_query, job_config=job_config
            ).result(timeout=30)
            fallback_rows = list(fallback_results)
            if not fallback_rows:
                return {}

            profile = {}
            for field in table.schema:
                vals = [getattr(r, field.name, None) for r in fallback_rows]
                profile[field.name] = {
                    "type": field.field_type,
                    "mode": field.mode,
                    "bq_description": (field.description or "").strip(),
                    "example_values": list(
                        dict.fromkeys(_to_display(v) for v in vals if v is not None)
                    )[:max_examples],
                }
            return profile
        except Exception as fe:
            logger.error(f"Fallback también falló para {fq_table}: {fe}")
            return {}

    if not row or total_rows == 0:
        return {}

    # 3. Formatear el perfil
    profile = {}
    for field in table.schema:
        if field.name not in profiled_column_names:
            continue

        col_stats = row[field.name]
        raw_examples = col_stats["examples"] or []

        # Deduplicación eficiente en Python para no estresar a BigQuery con DISTINCT
        display_examples = list(
            dict.fromkeys(_to_display(ex) for ex in raw_examples if ex is not None)
        )[:max_examples]

        profile[field.name] = {
            "type": field.field_type,
            "mode": field.mode,
            "bq_description": (field.description or "").strip(),
            "example_values": display_examples,
            "null_ratio": round(col_stats["null_count"] / total_rows, 3),
            "distinct_ratio": round(
                col_stats["dist_count"] / col_stats["non_null_count"]
                if col_stats["non_null_count"] > 0
                else 0.0,
                3,
            ),
        }

    return profile
