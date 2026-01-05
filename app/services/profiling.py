from typing import Dict, List
from google.cloud import bigquery
import logging

from adapters.bq_reader import get_partition_field, get_max_partition

logger = logging.getLogger(__name__)

def build_profile(
    table: bigquery.Table,
    bq_client: bigquery.Client,
    max_examples: int = 10,
    max_rows: int = 50
) -> Dict[str, Dict]:

    fq_table = f"{table.project}.{table.dataset_id}.{table.table_id}"
    logger.info(f"Profiling table: {fq_table}")

    partition_field = get_partition_field(table)

    job_config = None

    if partition_field:
        max_partition = get_max_partition(
            bq_client, fq_table, partition_field
        )

        logger.info(
            f"Using partition field '{partition_field}' "
            f"with max value {max_partition}"
        )

        query = f"""
        SELECT *
        FROM `{fq_table}`
        WHERE {partition_field} = @max_partition
        LIMIT {max_rows}
        """

        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter(
                    "max_partition",
                    "STRING" if isinstance(max_partition, str)
                    else "TIMESTAMP" if hasattr(max_partition, "tzinfo")
                    else "DATE",
                    max_partition
                )
            ]
        )

    else:
        query = f"""
        SELECT *
        FROM `{fq_table}`
        TABLESAMPLE SYSTEM (1 PERCENT)
        LIMIT {max_rows}
        """

    logger.debug(f"Profiling query: {query}")

    rows = list(
        bq_client.query(query, job_config=job_config).result()
    )

    if not rows:
        logger.warning(f"No rows returned for table {fq_table}")
        return {}

    col_names = [field.name for field in table.schema]
    col_values: Dict[str, List] = {c: [] for c in col_names}

    for row in rows:
        for c in col_names:
            if len(col_values[c]) < max_examples:
                value = row[c]
                if value is not None:
                    col_values[c].append(value)

    profile = {}

    for field in table.schema:
        values = col_values[field.name]

        profile[field.name] = {
            "type": field.field_type,
            "mode": field.mode,
            "example_values": list(dict.fromkeys(values))[:max_examples],
            "null_ratio": round(
                1 - (len(values) / max(1, len(rows))), 3
            ),
            "distinct_ratio": round(
                len(set(values)) / max(1, len(values)), 3
            )
        }

    logger.info(
        f"Profiled {len(profile)} columns for table {fq_table}"
    )

    return profile
