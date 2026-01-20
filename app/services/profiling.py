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

    partition_field = get_partition_field(table)
    partition_bq_type = _get_partition_bq_type(table, partition_field)

    job_config = None

    if partition_field:
        max_partition = get_max_partition(bq_client, fq_table, partition_field)
        logger.info(
            f"Using partition field '{partition_field}' with max value {max_partition}"
        )

        # Determina el tipo de parámetro a partir del schema, no del valor
        if partition_bq_type in ("TIMESTAMP",):
            param_type = "TIMESTAMP"
        elif partition_bq_type in ("DATE",):
            param_type = "DATE"
        elif partition_bq_type in ("DATETIME",):
            param_type = "DATETIME"
        else:
            # Fallback seguro
            param_type = "STRING"

        query = f"""
        SELECT *
        FROM `{fq_table}`
        WHERE {partition_field} = @max_partition
        LIMIT {max_rows}
        """

        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter(
                    "max_partition", param_type, max_partition
                )
            ]
        )
    else:
        query = f"""
        SELECT *
        FROM `{fq_table}`
        LIMIT {max_rows}
        """

    logger.debug(f"Profiling query: {query}")

    rows = list(bq_client.query(query, job_config=job_config).result())

    if not rows:
        logger.warning(f"No rows returned for table {fq_table}")
        return {}

    col_names = [field.name for field in table.schema]
    # Guardamos valores "originales" (para example_values) y también una versión normalizada
    col_values_original: Dict[str, List[Any]] = {c: [] for c in col_names}
    col_values_norm: Dict[str, List[Any]] = {c: [] for c in col_names}

    for row in rows:
        for c in col_names:
            value = row[c]

            # Tratamiento de "ausente"
            if _is_missing(value):
                continue

            # Limita ejemplos originales
            if len(col_values_original[c]) < max_examples:
                col_values_original[c].append(value)

            # Siempre acumula la normalización (sirve para distinct_ratio)
            col_values_norm[c].append(_normalize_for_hash(value))

    profile: Dict[str, Dict] = {}

    total_rows = max(1, len(rows))

    for field in table.schema:
        name = field.name
        examples = col_values_original[name]
        norm_values = col_values_norm[name]

        # Deduplicación segura de ejemplos
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

        # null_ratio: proporción de filas sin valor (None o lista vacía)
        missing_count = 0
        for row in rows:
            v = row[name]
            if _is_missing(v):
                missing_count += 1
        null_ratio = round(missing_count / total_rows, 3)

        # distinct_ratio: distintos sobre no-nulos (usando normalización)
        non_null_count = max(1, len(norm_values))
        distinct_ratio = round(len(set(norm_values)) / non_null_count, 3)

        profile[name] = {
            "type": field.field_type,
            "mode": field.mode,  # REPEATED/NULLABLE/REQUIRED
            "example_values": dedup_examples_display,
            "null_ratio": null_ratio,
            "distinct_ratio": distinct_ratio,
        }

    logger.info(f"Profiled {len(profile)} columns for table {fq_table}")

    return profile
