import logging
import uuid
from typing import List, Dict, Optional


from google.cloud import bigquery

logger = logging.getLogger(__name__)


def claim_pending_tables(
    bq_client: bigquery.Client,
    tracker_table: str,
    batch_size: int,
) -> (str, List[Dict]):
    """
    Claim de tablas con job_id único para evitar duplicados.
    """

    job_id = str(uuid.uuid4())

    query = f"""
    UPDATE `{tracker_table}`
    SET
        estado = 'PROCESSING',
        job_id = @job_id,
        processed_at = CURRENT_TIMESTAMP()
    WHERE STRUCT(catalog, schema, `table`) IN (
        SELECT AS STRUCT catalog, schema, `table`
        FROM `{tracker_table}`
        WHERE estado IS NULL OR estado = 'ERROR'
        LIMIT {batch_size}
    )
    """

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("job_id", "STRING", job_id),
        ]
    )

    logger.info(f"Claiming hasta {batch_size} tablas con job_id={job_id}")
    bq_client.query(query, job_config=job_config).result()

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

    logger.info(f"Tablas claimadas: {len(rows)}")

    return job_id, [dict(row) for row in rows]


def _escape(value: Optional[str]) -> str:
    if value is None:
        return "NULL"
    return "'" + value.replace("'", "\\'")[:500] + "'"


def batch_update_status(
    bq_client: bigquery.Client,
    tracker_table: str,
    rows: List[Dict],
) -> None:
    """
    Batch update seguro y eficiente.
    """

    if not rows:
        return

    logger.info(f"Batch updating {len(rows)} tablas...")

    values = ",\n".join(
        f"STRUCT('{r['catalog']}', '{r['schema']}', '{r['table']}', "
        f"'{r['estado']}', {_escape(r['error'])})"
        for r in rows
    )

    query = f"""
    UPDATE `{tracker_table}` t
    SET
        estado = src.estado,
        error = src.error,
        processed_at = CURRENT_TIMESTAMP()
    FROM (
        SELECT * FROM UNNEST([
            {values}
        ]) AS src(catalog, schema, table, estado, error)
    ) src
    WHERE
        t.catalog = src.catalog
        AND t.schema = src.schema
        AND t.table = src.table
    """

    bq_client.query(query).result()
