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
    Convierte cualquier valor a algo hasheable:
    - dict -> tuple ordenada de (key, normalized_value)
    - list/tuple -> tuple de normalized_value
    - bytes -> base64 string
    - Decimal -> str
    - date/datetime -> ISO string
    - otros -> se devuelven tal cual
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
        # Fallback: usa la representación normalizada
        norm = _normalize_for_hash(value)
        return json.dumps(norm, ensure_ascii=False, separators=(",", ":"), default=str)


def _get_partition_bq_type(
    table: bigquery.Table, partition_field_name: str | None
) -> str | None:
    """Obtiene el tipo BigQuery del campo de partición desde el schema."""
    if not partition_field_name:
        return None
    for f in table.schema:
        if f.name == partition_field_name:
            # BigQuery types: STRING, BYTES, INTEGER, INT64, FLOAT, FLOAT64, NUMERIC,
            # BOOLEAN, BOOL, TIMESTAMP, DATE, TIME, DATETIME, GEOGRAPHY, BIGNUMERIC
            return f.field_type.upper()
    return None


def build_profile(
    table: bigquery.Table,
    bq_client: bigquery.Client,
    max_examples: int = 10,
    max_rows: int = 50,
) -> Dict[str, Dict]:
    fq_table = f"{table.project}.{table.dataset_id}.{table.table_id}"
    logger.info(f"Profiling table: {fq_table}")

    # Campos sin datamasking
    schema_query = f"""
        SELECT column_name 
        FROM `{table.project}.{table.dataset_id}.INFORMATION_SCHEMA.COLUMNS`
        WHERE table_name = '{table.table_id}' 
          AND (policy_tags IS NULL OR ARRAY_LENGTH(policy_tags) = 0)
    """
    allowed_cols_rows = bq_client.query(schema_query).result()
    allowed_cols = [row.column_name for row in allowed_cols_rows]

    if not allowed_cols:
        logger.error(f"No se encontraron columnas accesibles (sin datamasking) para {fq_table}")
        return {}

    cols_select_str = ", ".join([f"`{c}`" for c in allowed_cols])

    partition_field = get_partition_field(table)
    partition_bq_type = _get_partition_bq_type(table, partition_field)

    job_config = None

    if partition_field:
        max_partition = get_max_partition(bq_client, fq_table, partition_field)
        logger.info(f"Usando partición '{partition_field}'. Valor base: {max_partition}")

        if partition_bq_type in ("TIMESTAMP", "DATE", "DATETIME"):
            param_type = partition_bq_type
        else:
            param_type = "STRING"

        query = f"""
        SELECT {cols_select_str}
        FROM `{fq_table}`
        WHERE DATE_TRUNC(CAST({partition_field} AS {param_type}), MONTH) = 
              DATE_TRUNC(CAST(@max_partition AS {param_type}), MONTH)
        LIMIT {max_rows}
        """

        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("max_partition", param_type, max_partition)
            ]
        )
    else:
        query = f"SELECT {cols_select_str} FROM `{fq_table}` LIMIT {max_rows}"

    logger.debug(f"Profiling query: {query}")

    rows = list(bq_client.query(query, job_config=job_config).result())

    if not rows:
        logger.warning(f"No se retornaron filas para la tabla {fq_table}")
        return {}

    # Procesamiento perfilado
    col_values_original: Dict[str, List[Any]] = {c: [] for c in allowed_cols}
    col_values_norm: Dict[str, List[Any]] = {c: [] for c in allowed_cols}

    for row in rows:
        for c in allowed_cols:
            value = row[c]
            if _is_missing(value):
                continue
            if len(col_values_original[c]) < max_examples:
                col_values_original[c].append(value)
            col_values_norm[c].append(_normalize_for_hash(value))

    profile: Dict[str, Dict] = {}
    total_rows = max(1, len(rows))

    # Solo procesamos las columnas que permitimos en el SELECT
    for field in table.schema:
        name = field.name
        if name not in allowed_cols:
            continue
            
        examples = col_values_original[name]
        norm_values = col_values_norm[name]

        seen = set()
        dedup_examples_display: List[str] = []
        for v in examples:
            key = _normalize_for_hash(v)
            if key in seen:
                continue
            seen.add(key)
            dedup_examples_display.append(_to_display(v))
            if len(dedup_examples_display) >= max_examples:
                break

        missing_count = 0
        for row in rows:
            if _is_missing(row[name]):
                missing_count += 1
        
        null_ratio = round(missing_count / total_rows, 3)
        non_null_count = max(1, len(norm_values))
        distinct_ratio = round(len(set(norm_values)) / non_null_count, 3)

        profile[name] = {
            "type": field.field_type,
            "mode": field.mode,
            "example_values": dedup_examples_display,
            "null_ratio": null_ratio,
            "distinct_ratio": distinct_ratio,
        }

    logger.info(f"Perfilado completado: {len(profile)} columnas para {fq_table}")
    return profile
