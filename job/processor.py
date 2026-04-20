import logging
import time

from google.cloud import bigquery

from app.adapters.bq_reader import get_table_metadata
from app.services.profiling import build_profile
from app.services.prompt_builder import build_prompt
from app.adapters.vertex_llm import generate_metadata
from app.validators.metadata_schema import validate_metadata
from app.services.schema_updater import update_table_metadata
from app.services.dataplex_writer import upsert_dataplex_aspects

logger = logging.getLogger(__name__)


class MetadataValidationError(Exception):
    pass


class RateLimitError(Exception):
    pass


def process_table(
    catalog: str,
    schema: str,
    table: str,
    bq_client: bigquery.Client,
) -> dict:
    """
    Retorna metadata útil para el tracker:
    - estado
    - error_type
    - duration_ms
    """

    start_time = time.time()
    table_fqn = f"{catalog}.{schema}.{table}"

    try:
        # 1. Metadata
        table_obj = get_table_metadata(catalog, schema, table, bq_client)

        # 2. Profiling
        profile = build_profile(table=table_obj, bq_client=bq_client)

        # 3. Prompt
        prompt = build_prompt(table=table_obj, profile=profile)

        # Protección (muy importante)
        if len(prompt) > 20000:
            raise ValueError("PROMPT_TOO_LARGE")

        # 4. LLM
        payload = generate_metadata(prompt)

        # 5. Validación
        errors = validate_metadata(payload)
        if errors:
            raise MetadataValidationError(str(errors))

        # 6. BigQuery (best-effort)
        try:
            update_table_metadata(table_fqn, payload, bq_client)
        except Exception as exc:
            logger.warning(f"[{table_fqn}] BQ update failed: {exc}")

        # 7. Dataplex (best-effort)
        try:
            upsert_dataplex_aspects(payload)
        except Exception as exc:
            logger.warning(f"[{table_fqn}] Dataplex failed: {exc}")

        duration = int((time.time() - start_time) * 1000)

        return {
            "estado": "OK",
            "error": None,
            "error_type": None,
            "duration_ms": duration,
        }

    except Exception as e:
        duration = int((time.time() - start_time) * 1000)
        error_msg = str(e)

        # Clasificación de errores
        if "429" in error_msg or "ResourceExhausted" in error_msg:
            error_type = "RATE_LIMIT"
        elif "PROMPT_TOO_LARGE" in error_msg:
            error_type = "PROMPT"
        elif isinstance(e, MetadataValidationError):
            error_type = "VALIDATION"
        else:
            error_type = "UNKNOWN"

        logger.error(f"[{table_fqn}] Error: {error_msg}")

        return {
            "estado": "ERROR",
            "error": error_msg[:500],
            "error_type": error_type,
            "duration_ms": duration,
        }
