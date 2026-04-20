import json
import logging
import math
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from google.cloud import bigquery

logger = logging.getLogger(__name__)

# Límite conservador de tamaño de payload JSON por MERGE
# BigQuery soporta parámetros STRING hasta ~1MB; usamos 800KB de margen
_MAX_PAYLOAD_BYTES = 800_000
# Truncado de campo error para evitar payloads enormes
_MAX_ERROR_LEN = 800


def claim_pending_tables(
    bq_client: bigquery.Client,
    tracker_table: str,
    batch_size: int,
) -> Tuple[str, List[Dict]]:
    """
    Claim atómico con QUALIFY ROW_NUMBER().

    Gap 1 (race condition): BigQuery serializa DML sobre la misma tabla,
    pero entre el subquery y el UPDATE puede haber una ventana mínima.
    Lo cerramos añadiendo AND estado != 'PROCESSING' en el WHERE del UPDATE
    además del subquery — doble filtro defensivo.
    Si dos tasks llegan exactamente al mismo tiempo, la segunda no encuentra
    filas que cumplan ambos filtros y retorna 0 rows afectadas, sin duplicar.
    """
    job_id = str(uuid.uuid4())

    claim_query = f"""
        UPDATE `{tracker_table}`
        SET
            job_id     = @job_id,
            estado     = 'PROCESSING',
            updated_at = CURRENT_TIMESTAMP()
        WHERE
            (estado IS NULL OR estado = 'ERROR')
            AND job_id IS NULL
            AND STRUCT(catalog, schema, `table`) IN (
                SELECT AS STRUCT catalog, schema, `table`
                FROM `{tracker_table}`
                WHERE estado IS NULL OR estado = 'ERROR'
                QUALIFY ROW_NUMBER() OVER (
                    ORDER BY catalog, schema, `table`
                ) <= {batch_size}
            )
    """

    bq_client.query(
        claim_query,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("job_id", "STRING", job_id),
            ]
        ),
    ).result()

    fetch_query = f"""
        SELECT catalog, schema, `table`
        FROM `{tracker_table}`
        WHERE job_id = @job_id
    """

    rows = list(
        bq_client.query(
            fetch_query,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("job_id", "STRING", job_id),
                ]
            ),
        ).result()
    )

    logger.info(f"Tablas claimadas: {len(rows)} (job_id={job_id})")
    return job_id, [dict(row) for row in rows]


def batch_update_status(
    bq_client: bigquery.Client,
    tracker_table: str,
    rows: List[Dict],
) -> None:
    """
    Actualiza estado final en UNA query MERGE por chunk.

    Gap 2 (payload corrupto): sanitiza cada campo antes de serializar.
    Gap 3 (coste de escaneo): si el payload supera _MAX_PAYLOAD_BYTES,
    lo divide en chunks y lanza un MERGE por chunk — sigue siendo O(chunks)
    queries, no O(filas).
    """
    if not rows:
        return

    sanitized = [_sanitize_row(r) for r in rows]
    chunks = _split_into_chunks(sanitized)

    logger.info(f"Batch update: {len(rows)} filas en {len(chunks)} chunk(s)...")

    for i, chunk in enumerate(chunks):
        _merge_chunk(bq_client, tracker_table, chunk)
        logger.info(f"Chunk {i + 1}/{len(chunks)} actualizado ({len(chunk)} filas).")

    logger.info(f"Batch update completado: {len(rows)} filas totales.")


# ── internals ────────────────────────────────────────────────────────────────


def _sanitize_row(r: Dict) -> Dict:
    """
    Gap 2: limpia y valida cada campo antes de enviarlo a BigQuery.
    - Trunca el error a _MAX_ERROR_LEN para no reventar el payload
    - Normaliza None a null JSON
    - Elimina caracteres de control que rompen JSON_VALUE en BQ
    """
    error = r.get("error")
    if error is not None:
        # Trunca y elimina caracteres de control (ASCII 0-31 excepto \n \t)
        error = "".join(c for c in str(error) if c == "\n" or c == "\t" or ord(c) >= 32)
        error = error[:_MAX_ERROR_LEN]

    return {
        "catalog": str(r["catalog"]).strip(),
        "schema": str(r["schema"]).strip(),
        "table": str(r["table"]).strip(),
        "estado": str(r["estado"]).strip(),
        "error": error,
        "processed_at": _ts(r.get("processed_at")),
    }


def _split_into_chunks(rows: List[Dict]) -> List[List[Dict]]:
    """
    Gap 3: divide en chunks que no superen _MAX_PAYLOAD_BYTES.
    Calcula el tamaño del JSON serializado y corta cuando es necesario.
    """
    chunks: List[List[Dict]] = []
    current_chunk: List[Dict] = []
    current_size = 0

    for row in rows:
        row_bytes = len(json.dumps(row, ensure_ascii=False).encode("utf-8"))
        if current_chunk and current_size + row_bytes > _MAX_PAYLOAD_BYTES:
            chunks.append(current_chunk)
            current_chunk = []
            current_size = 0
        current_chunk.append(row)
        current_size += row_bytes

    if current_chunk:
        chunks.append(current_chunk)

    return chunks


def _merge_chunk(
    bq_client: bigquery.Client,
    tracker_table: str,
    rows: List[Dict],
) -> None:
    payload = json.dumps(rows, ensure_ascii=False)

    merge_query = f"""
        MERGE `{tracker_table}` AS t
        USING (
            SELECT
                JSON_VALUE(item, '$.catalog')                        AS catalog,
                JSON_VALUE(item, '$.schema')                         AS schema,
                JSON_VALUE(item, '$.table')                          AS `table`,
                JSON_VALUE(item, '$.estado')                         AS estado,
                JSON_VALUE(item, '$.error')                          AS error,
                CAST(JSON_VALUE(item, '$.processed_at') AS TIMESTAMP) AS processed_at
            FROM UNNEST(JSON_QUERY_ARRAY(@payload)) AS item
        ) AS src
        ON  t.catalog = src.catalog
        AND t.schema  = src.schema
        AND t.`table` = src.`table`
        WHEN MATCHED THEN UPDATE SET
            t.estado       = src.estado,
            t.error        = src.error,
            t.processed_at = src.processed_at,
            t.updated_at   = CURRENT_TIMESTAMP()
    """

    bq_client.query(
        merge_query,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("payload", "STRING", payload),
            ]
        ),
    ).result()


def _ts(value) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return str(value)
