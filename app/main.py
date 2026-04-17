from fastapi import FastAPI, HTTPException
from google.cloud import bigquery
import logging
from fastapi import BackgroundTasks


logging.basicConfig(
    level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)

logger = logging.getLogger(__name__)

from app.adapters.bq_reader import get_table_metadata, get_table_status
from app.services.profiling import build_profile
from app.services.prompt_builder import build_prompt
from app.adapters.vertex_llm import generate_metadata
from app.validators.metadata_schema import validate_metadata
from app.models import TableMetadata, TableStatus
from app.services.schema_updater import update_table_metadata
from app.services.dataplex_writer import (
    upsert_dataplex_aspects,
    _publish_dataplex_background,
)

app = FastAPI(title="Metadata Generator API")


@app.get("/")
async def health():
    return {"status": "ok"}


@app.get(
    "/projects/{project}/datasets/{dataset}/tables/{table}", response_model=TableStatus
)
async def get_table_info(project: str, dataset: str, table: str) -> TableStatus:
    client = bigquery.Client(project=project.strip())
    status = get_table_status(
        client=client,
        project=project.strip(),
        dataset=dataset.strip(),
        table=table.strip(),
    )
    if not status.get("exists"):
        raise HTTPException(status_code=404, detail="Table not found")
    return status


@app.post(
    "/projects/{project}/datasets/{dataset}/tables/{table}/generate",
    response_model=TableMetadata,
)
def generate(
    project: str, dataset: str, table: str, background_tasks: BackgroundTasks
) -> TableMetadata:
    """Genera las descripciones para la tabla. Revisar el JSON antes de aprobar."""

    table_fqn = f"{project.strip()}.{dataset.strip()}.{table.strip()}"

    try:
        client = bigquery.Client(project=project.strip())

        logger.info(f"Obteniendo metadata para: {table_fqn}")
        table_obj = get_table_metadata(
            project.strip(), dataset.strip(), table.strip(), client
        )

        logger.info(f"Metadata obtenida de BigQuery para: {table_fqn}")
        profile = build_profile(table=table_obj, bq_client=client)

        logger.info(f"Profile construido para: {table_fqn}")
        prompt = build_prompt(table=table_obj, profile=profile)

        logger.info(f"Prompt construido para: {table_fqn}")
        payload = generate_metadata(prompt)

        logger.info(f"Metadata generada por LLM para: {table_fqn}")
        errors = validate_metadata(payload)
        if errors:
            raise HTTPException(
                status_code=422,
                detail={"error": "Invalid metadata schema", "details": errors},
            )
        try:
            update_table_metadata(table_fqn, payload, client)
            logger.info(f"Metadata actualizada en BigQuery para: {table_fqn}")
        except:
            logger.warning(
                f"[Generate] Sin permisos para actualizar BigQuery en {table_fqn}. "
                f"Se continúa con Dataplex..."
            )

        background_tasks.add_task(
            _publish_dataplex_background,
            payload,
            table_fqn,
        )

        logger.info(f"[Generate] Publicación programada de Dataplex  para {table_fqn}")

        return payload

    except HTTPException as he:
        raise he
    except Exception:
        logger.exception(f"Error generating metadata for {table_fqn}")
        raise HTTPException(
            status_code=500, detail="Error interno al procesar la tabla"
        )
