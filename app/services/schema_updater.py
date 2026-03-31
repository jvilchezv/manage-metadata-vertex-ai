from google.cloud import bigquery
import logging

from app.adapters.bq_writer import update_table_schema
from app.validators.metadata_schema import validate_metadata

logger = logging.getLogger(__name__)


def update_table_metadata(table_fqn: str, payload: dict) -> None:
    """
    Valida y aplica el metadata generado en el schema de BigQuery.
    """
    errors = validate_metadata(payload)
    if errors:
        raise ValueError(f"Invalid metadata for {table_fqn}: {errors}")

    update_table_schema(payload)