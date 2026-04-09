from google.cloud import bigquery
import logging
from google.api_core.exceptions import NotFound

logger = logging.getLogger(__name__)


client = bigquery.Client()


def get_table_metadata(project: str, dataset: str, table: str) -> bigquery.Table:
    """
    Retorna el objeto bigquery.Table completo.
    """
    table_id = f"{project}.{dataset}.{table}"
    return client.get_table(table_id)


def get_partition_field(table: bigquery.Table) -> str:
    """
    Retorna el campo de partición si existe, o "" si no es particionada.
    """
    if table.time_partitioning:
        return table.time_partitioning.field or "_PARTITIONDATE"
    return ""


def get_max_partition(client, fq_table, partition_field):
    project, dataset, table = fq_table.split('.')
    
    query = f"""
    SELECT MAX(partition_id) as max_id
    FROM `{project}.{dataset}.INFORMATION_SCHEMA.PARTITIONS`
    WHERE table_name = '{table}' AND partition_id <> '__NULL__'
    """
    
    res = client.query(query).result()
    row = next(res, None)
    
    if row and row.max_id:
        mid = row.max_id
        return f"{mid[:4]}-{mid[4:6]}-{mid[6:]}"
    return None



def get_table_status(client: bigquery.Client, project: str, dataset: str, table: str):
    table_id = f"{project}.{dataset}.{table}"

    try:
        table_obj = client.get_table(table_id)
    except NotFound:
        return {"table_fqn": table_id, "exists": False}

    partition_field = (
        table_obj.time_partitioning.field if table_obj.time_partitioning else None
    )

    columns = []
    for field in table_obj.schema:
        columns.append(
            {
                "name": field.name,
                "type": field.field_type,
                "mode": field.mode,
                "description": field.description,
                "is_partitioning_column": field.name == partition_field,
            }
        )

    return {
        "table_fqn": table_id,
        "exists": True,
        "is_partitioned": partition_field is not None,
        "partition_field": partition_field,
        "row_count": table_obj.num_rows,
        "size_mb": round(table_obj.num_bytes / 1024 / 1024, 2),
        "description": table_obj.description,
        "columns": columns,
        "labels": table_obj.labels or {},
        "last_modified": table_obj.modified,
    }
