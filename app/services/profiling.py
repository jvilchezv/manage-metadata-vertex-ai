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
    # TABLESAMPLE no filtra filas exactas sino bloques de storage (~approx)
    sample_clause = f"TABLESAMPLE SYSTEM ({sample_percent} PERCENT)"

    if partition_field and partition_bq_type in ("TIMESTAMP", "DATE", "DATETIME"):
        # Normalizar el tipo para el CAST (BigQuery acepta DATE, DATETIME, TIMESTAMP)
        cast_type = partition_bq_type  # ya es uno de los tres válidos

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

    # Sin partición por fecha: muestreo puro
    query = f"SELECT {cols_select_str} FROM `{fq_table}` {sample_clause}"
    return query, None


def build_profile(
    table: bigquery.Table,
    bq_client: bigquery.Client,
    max_examples: int = 10,
    sample_percent: int = 10,
) -> Dict[str, Dict]:
    """
    Perfila una tabla BigQuery usando todos sus campos
    y un muestreo aproximado del sample_percent de los datos del mes más reciente
    (si la tabla tiene partición por fecha) o de toda la tabla.
    """
    fq_table = f"{table.project}.{table.dataset_id}.{table.table_id}"
    logger.info(f"Profiling table: {fq_table} | sample={sample_percent}%")

    all_cols = [f.name for f in table.schema]

    if not all_cols:
        logger.error(f"El schema de {fq_table} está vacío.")
        return {}

    cols_select_str = ", ".join([f"`{c}`" for c in all_cols])

    partition_field = get_partition_field(table)
    partition_bq_type = _get_partition_bq_type(table, partition_field)
    max_partition = None

    if partition_field and partition_bq_type in ("TIMESTAMP", "DATE", "DATETIME"):
        max_partition = get_max_partition(bq_client, fq_table, partition_field)
        logger.info(
            f"Partición detectada: '{partition_field}' ({partition_bq_type}). "
            f"Max partition: {max_partition}"
        )
    elif partition_field:
        logger.info(
            f"Partición no-fecha detectada: '{partition_field}' ({partition_bq_type}). "
            "Solo se aplica TABLESAMPLE."
        )

    query, job_config = _build_profile_query(
        fq_table=fq_table,
        cols_select_str=cols_select_str,
        partition_field=partition_field,
        partition_bq_type=partition_bq_type,
        max_partition=max_partition,
        sample_percent=sample_percent,
    )

    logger.debug(f"Profiling query:\n{query}")
    rows = list(bq_client.query(query, job_config=job_config).result())

    if not rows:
        logger.warning(f"Sin filas para {fq_table}. El sample puede ser demasiado pequeño.")
        return {}

    total_rows = len(rows)

    col_original: Dict[str, List[Any]] = {c: [] for c in all_cols}
    col_norm: Dict[str, List[Any]] = {c: [] for c in all_cols}
    col_missing: Dict[str, int] = {c: 0 for c in all_cols}

    for row in rows:
        for c in all_cols:
            value = row[c]
            if _is_missing(value):
                col_missing[c] += 1
                continue
            if len(col_original[c]) < max_examples:
                col_original[c].append(value)
            col_norm[c].append(_normalize_for_hash(value))

    profile: Dict[str, Dict] = {}

    for field in table.schema:
        name = field.name
        examples = col_original[name]
        norm_values = col_norm[name]
        missing_count = col_missing[name]

        seen: set = set()
        dedup_display: List[str] = []
        for v in examples:
            key = _normalize_for_hash(v)
            if key in seen:
                continue
            seen.add(key)
            dedup_display.append(_to_display(v))
            if len(dedup_display) >= max_examples:
                break

        null_ratio = round(missing_count / total_rows, 3)
        non_null_count = len(norm_values)
        distinct_ratio = round(
            len(set(norm_values)) / non_null_count if non_null_count > 0 else 0.0,
            3,
        )

        profile[name] = {
            "type": field.field_type,
            "mode": field.mode,
            "bq_description": (field.description or "").strip(),  # <-- nuevo
            "example_values": dedup_display,
            "null_ratio": null_ratio,
            "distinct_ratio": distinct_ratio,
        }

    logger.info(f"Perfilado completado: {len(profile)} columnas | {total_rows} filas muestreadas")
    return profile