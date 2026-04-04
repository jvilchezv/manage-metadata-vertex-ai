from typing import Any, Dict, List
from google.cloud import bigquery
import logging
import base64
import datetime
import json
from decimal import Decimal

from app.adapters.bq_reader import get_partition_field, get_max_partition

logger = logging.getLogger(__name__)


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
    for f in table.schema:
        if f.name == partition_field_name:
            return f.field_type.upper()
    return None


def _build_profile_query(
    fq_table: str,
    cols_select_str: str,
    partition_field: str | None,
    partition_bq_type: str | None,
    max_partition: Any,
    sample_percent: int,
) -> tuple[str, bigquery.QueryJobConfig | None]:
    """
    Construye la query de perfilado con:
    - TABLESAMPLE SYSTEM para muestrear ~sample_percent% de la tabla.
    - Filtro DATE_TRUNC al mes más reciente si la tabla tiene partición por fecha.

    Retorna (query_str, job_config_o_None).
    """
    sample_clause = f"TABLESAMPLE SYSTEM ({sample_percent} PERCENT)"

    if partition_field and partition_bq_type in ("TIMESTAMP", "DATE", "DATETIME"):
        cast_type = partition_bq_type

        query = f"""
        SELECT {cols_select_str}
        FROM `{fq_table}` {sample_clause}
        WHERE DATE_TRUNC(CAST(`{partition_field}` AS {cast_type}), MONTH)
            = DATE_TRUNC(CAST(@max_partition AS {cast_type}), MONTH)
        """

        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("max_partition", cast_type, max_partition)
            ]
        )
        return query, job_config

    query = f"SELECT {cols_select_str} FROM `{fq_table}` {sample_clause}"
    return query, None


def build_profile(
    table: bigquery.Table,
    bq_client: bigquery.Client,
    max_examples: int = 10,
    sample_percent: int = 5,
) -> Dict[str, Dict]:
    fq_table = f"{table.project}.{table.dataset_id}.{table.table_id}"
    logger.info(f"Profiling table: {fq_table} | sample={sample_percent}%")

    all_cols = [f.name for f in table.schema]
    if not all_cols:
        return {}

    cols_select_str = ", ".join([f"`{c}`" for c in all_cols])
    partition_field = get_partition_field(table)
    partition_bq_type = _get_partition_bq_type(table, partition_field)
    max_partition = None

    if partition_field and partition_bq_type in ("TIMESTAMP", "DATE", "DATETIME"):
        max_partition = get_max_partition(bq_client, fq_table, partition_field)

    query, job_config = _build_profile_query(
        fq_table=fq_table,
        cols_select_str=cols_select_str,
        partition_field=partition_field,
        partition_bq_type=partition_bq_type,
        max_partition=max_partition,
        sample_percent=sample_percent,
    )

    # OPTIMIZACIÓN: Añadimos un LIMIT para asegurar que quepa en 512MiB
    query += " LIMIT 50000"

    logger.debug(f"Profiling query:\n{query}")

    # OPTIMIZACIÓN: No convertir a list(). Usar el iterador directamente.
    query_job = bq_client.query(query, job_config=job_config)
    rows_iter = query_job.result()

    total_rows = 0
    col_original: Dict[str, List[Any]] = {c: [] for c in all_cols}
    col_distinct_set: Dict[str, set] = {
        c: set() for c in all_cols
    }  # Usar sets directamente
    col_missing: Dict[str, int] = {c: 0 for c in all_cols}
    col_non_null_count: Dict[str, int] = {c: 0 for c in all_cols}

    for row in rows_iter:
        total_rows += 1
        for c in all_cols:
            value = row[c]
            if _is_missing(value):
                col_missing[c] += 1
                continue

            col_non_null_count[c] += 1

            # Solo guardamos ejemplos hasta el límite (ahorro de RAM)
            if len(col_original[c]) < max_examples:
                col_original[c].append(value)

            # Guardamos el hash para el cálculo de distintos (ahorro de RAM vs guardar objeto completo)
            col_distinct_set[c].add(_normalize_for_hash(value))

    if total_rows == 0:
        logger.warning(f"Sin filas para {fq_table}.")
        return {}

    profile: Dict[str, Dict] = {}
    for field in table.schema:
        name = field.name
        examples = col_original[name]
        distinct_count = len(col_distinct_set[name])
        missing_count = col_missing[name]
        non_null_count = col_non_null_count[name]

        # Deduplicar ejemplos para mostrar
        seen = set()
        dedup_display = []
        for v in examples:
            key = _normalize_for_hash(v)
            if key not in seen:
                seen.add(key)
                dedup_display.append(_to_display(v))

        profile[name] = {
            "type": field.field_type,
            "mode": field.mode,
            "bq_description": (field.description or "").strip(),
            "example_values": dedup_display,
            "null_ratio": round(missing_count / total_rows, 3),
            "distinct_ratio": round(
                distinct_count / non_null_count if non_null_count > 0 else 0.0, 3
            ),
        }

    logger.info(
        f"Perfilado completado: {len(profile)} columnas | {total_rows} filas procesadas"
    )
    return profile
