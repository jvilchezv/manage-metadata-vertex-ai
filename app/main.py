from fastapi import FastAPI, HTTPException
from google.cloud import bigquery
import logging

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)

logger = logging.getLogger(__name__)

from app.adapters.bq_reader import get_table_metadata, get_table_status
from app.services.profiling import get_table_profile
from app.services.prompt_builder import build_prompt
from app.adapters.vertex_llm import generate_metadata
from app.validators.metadata_schema import validate_metadata
from app.models import TableMetadata, TableStatus
from app.services.schema_updater import update_table_metadata

# NUEVO: importar el writer de Dataplex para publicar los 3 aspects en /approve
from app.services.dataplex_writer import upsert_dataplex_aspects

app = FastAPI(title="Metadata Generator API")


@app.get("/")
async def health():
    return {"status": "ok"}


@app.get(
    "/projects/{project}/datasets/{dataset}/tables/{table}", response_model=TableStatus
)
async def get_table_info(project: str, dataset: str, table: str) -> TableStatus:
    client = bigquery.Client()
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
async def generate(project: str, dataset: str, table: str) -> TableMetadata:
    """Genera las descripciones para la tabla. Revisar el JSON antes de aprobar."""
    try:
        profile = get_table_profile(
            project=project.strip(),
            dataset=dataset.strip(),
            table_id=table.strip(),
            location="us-central1",
            # results_project=project.strip(), # opcional: exportar a BQ
            # results_dataset="profiling_results",
            # results_table="dataplex_profiles",
        )
        table_obj = get_table_metadata(project.strip(), dataset.strip(), table.strip())
        prompt = build_prompt(table=table_obj, profile=profile)
        payload = generate_metadata(prompt)

        errors = validate_metadata(payload)
        if errors:
            raise HTTPException(
                status_code=422,
                detail={"error": "Invalid metadata schema", "details": errors},
            )
        return payload

    except HTTPException:
        raise
    except Exception:
        logger.exception("Unhandled error generating metadata")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post(
    "/projects/{project}/datasets/{dataset}/tables/{table}/approve",
    response_model=TableMetadata,
)
async def approve(project: str, dataset: str, table: str, payload: TableMetadata) -> TableMetadata:
    """Recibe el JSON revisado por la persona y aplica las descripciones en BigQuery."""
    try:
        table_fqn = f"{project.strip()}.{dataset.strip()}.{table.strip()}"
        update_table_metadata(table_fqn, payload.model_dump())
        logger.info(f"[Approve] BigQuery actualizado para: {table_fqn}")

        # ── Paso 2: Publicar en Dataplex ──────────────────────────────────────
        # Construye y publica los 3 Aspect Types en Dataplex Catalog.
        # Usa el mismo payload aprobado, no hace ninguna transformación previa.
        dataplex_result = upsert_dataplex_aspects(payload.model_dump())

        if dataplex_result.success:
            logger.info(
                f"[Approve] Dataplex actualizado para: {table_fqn}. "
                f"Aspects: {dataplex_result.aspects_updated}"
            )
        else:
            # Loguear el error pero retornar 200 igual — BigQuery ya se escribió
            # y no queremos que el usuario tenga que re-aprobar por un fallo de Dataplex.
            # El equipo de datos puede re-publicar en Dataplex manualmente si es necesario.
            logger.error(
                f"[Approve] Dataplex falló para {table_fqn}: {dataplex_result.errors}"
            )

        return payload

    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unhandled error applying metadata")
        raise HTTPException(status_code=500, detail="Internal server error")