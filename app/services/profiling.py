from typing import Any, Dict, List, Optional
from google.cloud import bigquery
import logging
import json

from app.adapters.bq_reader import get_partition_field, get_max_partition

logger = logging.getLogger(__name__)


def _get_partition_bq_type(
    table: bigquery.Table, partition_field_name: Optional[str]
) -> Optional[str]:
    if not partition_field_name:
        return None
    for f in table.schema:
        if f.name == partition_field_name:
            return f.field_type.upper()
    return None


def _col_metrics_sql(
    field: bigquery.SchemaField,
    fq_table: str,
    sample_clause: str,
    partition_filter: str,
    max_examples: int,
) -> List[str]:
    """
    Genera las tres expresiones SQL para una columna:
      - null_ratio_{col}
      - distinct_ratio_{col}
      - examples_{col}

    Devuelve una lista de strings para insertar en el SELECT principal.
    """
    col = field.name
    bq_type = field.field_type.upper()
    mode = field.mode.upper()

    # ------------------------------------------------------------------ #
    # Expresiones de "es nulo" y "no es nulo"
    # ------------------------------------------------------------------ #
    if mode == "REPEATED":
        is_missing = f"(`{col}` IS NULL OR ARRAY_LENGTH(`{col}`) = 0)"
        cast_for_distinct = f"TO_JSON_STRING(`{col}`)"
        cast_for_example  = f"TO_JSON_STRING(`{col}`)"
    else:
        is_missing = f"`{col}` IS NULL"
        if bq_type in ("BYTES", "RECORD", "STRUCT"):
            cast_for_distinct = f"TO_JSON_STRING(`{col}`)"
            cast_for_example  = f"TO_JSON_STRING(`{col}`)"
        else:
            cast_for_distinct = f"`{col}`"
            cast_for_example  = f"CAST(`{col}` AS STRING)"

    non_null_expr = f"NOT ({is_missing})"

    # ------------------------------------------------------------------ #
    # 1. null_ratio
    # ------------------------------------------------------------------ #
    null_ratio_sql = f"""
    ROUND(COUNTIF({is_missing}) / NULLIF(COUNT(*), 0), 3)
        AS null_ratio_{col}"""

    # ------------------------------------------------------------------ #
    # 2. distinct_ratio  (APPROX_COUNT_DISTINCT es O(1) en memoria en BQ)
    # ------------------------------------------------------------------ #
    distinct_ratio_sql = f"""
    ROUND(
        SAFE_DIVIDE(
            APPROX_COUNT_DISTINCT({cast_for_distinct}),
            COUNTIF({non_null_expr})
        ),
        3
    ) AS distinct_ratio_{col}"""

    # ------------------------------------------------------------------ #
    # 3. examples: subquery correlacionada — BQ la optimiza sobre el CTE
    # ------------------------------------------------------------------ #
    examples_sql = f"""
    (
        SELECT TO_JSON_STRING(ARRAY_AGG(val LIMIT {max_examples}))
        FROM (
            SELECT DISTINCT {cast_for_example} AS val
            FROM base
            WHERE {non_null_expr}
            LIMIT {max_examples * 4}   -- margen para que DISTINCT baje a max_examples
        )
    ) AS examples_{col}"""

    return [null_ratio_sql, distinct_ratio_sql, examples_sql]


def _build_profile_sql(
    fq_table: str,
    table: bigquery.Table,
    partition_field: Optional[str],
    partition_bq_type: Optional[str],
    max_partition: Any,
    sample_percent: int,
    max_examples: int,
) -> tuple:
    """
    Construye UNA sola query que devuelve UNA sola fila con todas
    las métricas de todas las columnas.

    Arquitectura:
        WITH base AS (
            SELECT * FROM tabla TABLESAMPLE SYSTEM (N PERCENT)
            WHERE <filtro partición opcional>
        )
        SELECT
            COUNT(*) AS total_rows,
            -- por cada col:
            ROUND(COUNTIF(col IS NULL) / COUNT(*), 3)   AS null_ratio_col,
            ROUND(APPROX_COUNT_DISTINCT(col) / ..., 3)  AS distinct_ratio_col,
            (SELECT TO_JSON_STRING(...) FROM base ...)   AS examples_col,
            ...
        FROM base
    """
    sample_clause = f"TABLESAMPLE SYSTEM ({sample_percent} PERCENT)"
    query_params: List[bigquery.ScalarQueryParameter] = []
    partition_filter = ""

    if partition_field and partition_bq_type in ("TIMESTAMP", "DATE", "DATETIME"):
        cast_type = partition_bq_type
        partition_filter = f"""
        WHERE DATE_TRUNC(CAST(`{partition_field}` AS {cast_type}), MONTH)
            = DATE_TRUNC(CAST(@max_partition AS {cast_type}), MONTH)"""
        query_params.append(
            bigquery.ScalarQueryParameter("max_partition", cast_type, max_partition)
        )

    # Generar bloques de métricas para cada campo
    all_metrics: List[str] = []
    for field in table.schema:
        all_metrics.extend(
            _col_metrics_sql(
                field=field,
                fq_table=fq_table,
                sample_clause=sample_clause,
                partition_filter=partition_filter,
                max_examples=max_examples,
            )
        )

    metrics_str = ",\n        ".join(all_metrics)

    query = f"""
    WITH base AS (
        SELECT *
        FROM `{fq_table}` {sample_clause}
        {partition_filter}
    )
    SELECT
        COUNT(*) AS total_rows,
        {metrics_str}
    FROM base
    """

    job_config = (
        bigquery.QueryJobConfig(query_parameters=query_params)
        if query_params
        else None
    )
    return query, job_config


# --------------------------------------------------------------------------- #
# Función pública                                                              #
# --------------------------------------------------------------------------- #

def build_profile(
    table: bigquery.Table,
    bq_client: bigquery.Client,
    max_examples: int = 10,
    sample_percent: int = 10,
) -> Dict[str, Dict]:
    """
    Perfila una tabla BigQuery ejecutando UNA SOLA QUERY SQL en BigQuery.

    Todo el cómputo ocurre en la infraestructura de BQ:
      - Sin transferencia masiva de filas a Cloud Run.
      - Sin riesgo de OOM ni timeout por iteración de filas.
      - Soporta tablas de 15M+ filas sin problema.
      - Solo UNA fila de resultado viaja por la red.

    Retorna un dict keyed por nombre de columna con:
        - type            : tipo BQ del campo
        - mode            : NULLABLE / REQUIRED / REPEATED
        - bq_description  : descripción registrada en BQ
        - example_values  : lista de strings (hasta max_examples)
        - null_ratio      : proporción de nulos/vacíos sobre total de filas
        - distinct_ratio  : proporción de valores únicos sobre no-nulos
    """
    fq_table = f"{table.project}.{table.dataset_id}.{table.table_id}"
    logger.info(f"Profiling table (SQL mode): {fq_table} | sample={sample_percent}%")

    all_fields = table.schema
    if not all_fields:
        logger.error(f"El schema de {fq_table} está vacío.")
        return {}

    # --- Detectar partición ---
    partition_field = get_partition_field(table)
    partition_bq_type = _get_partition_bq_type(table, partition_field)
    max_partition = None

    if partition_field and partition_bq_type in ("TIMESTAMP", "DATE", "DATETIME"):
        max_partition = get_max_partition(bq_client, fq_table, partition_field)
        logger.info(
            f"Partición: '{partition_field}' ({partition_bq_type}) | max={max_partition}"
        )
    elif partition_field:
        logger.info(
            f"Partición no-fecha: '{partition_field}' ({partition_bq_type}). Solo TABLESAMPLE."
        )

    # --- Construir y ejecutar query ---
    query, job_config = _build_profile_sql(
        fq_table=fq_table,
        table=table,
        partition_field=partition_field,
        partition_bq_type=partition_bq_type,
        max_partition=max_partition,
        sample_percent=sample_percent,
        max_examples=max_examples,
    )

    logger.debug(f"Profiling query:\n{query}")

    result = list(bq_client.query(query, job_config=job_config).result())

    if not result:
        logger.warning(f"Sin resultado para {fq_table}.")
        return {}

    row = result[0]  # Solo UNA fila con todas las métricas
    total_rows = row["total_rows"]

    if total_rows == 0:
        logger.warning(
            f"0 filas muestreadas para {fq_table}. "
            "Reduce sample_percent o revisa el filtro de partición."
        )
        return {}

    # --- Construir perfil desde la única fila resultado ---
    profile: Dict[str, Dict] = {}

    for field in all_fields:
        name = field.name

        null_ratio     = float(row.get(f"null_ratio_{name}") or 0.0)
        distinct_ratio = float(row.get(f"distinct_ratio_{name}") or 0.0)

        raw_examples = row.get(f"examples_{name}")
        try:
            example_values: List[str] = json.loads(raw_examples) if raw_examples else []
        except (json.JSONDecodeError, TypeError):
            example_values = [str(raw_examples)] if raw_examples else []

        profile[name] = {
            "type":           field.field_type,
            "mode":           field.mode,
            "bq_description": (field.description or "").strip(),
            "example_values": example_values,
            "null_ratio":     null_ratio,
            "distinct_ratio": distinct_ratio,
        }

    logger.info(
        f"Perfilado completado (SQL): {len(profile)} columnas | "
        f"~{total_rows:,} filas en el sample"
    )
    return profile
