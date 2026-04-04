from fastapi import FastAPI, HTTPException
from google.cloud import bigquery
import logging

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)

logger = logging.getLogger(__name__)

from app.adapters.bq_reader import get_table_metadata, get_table_status
from app.adapters.dataplex_profiler import get_table_profile
from app.services.prompt_builder import build_prompt
from app.adapters.vertex_llm import generate_metadata
from app.validators.metadata_schema import validate_metadata
from app.models import TableMetadata, TableStatus
from app.services.schema_updater import update_table_metadata

# NUEVO: importar el writer de Dataplex para publicar los 3 aspects en /approve
from app.services.dataplex_writer import upsert_dataplex_aspects

app = FastAPI(title="Metadata Generator API")

# Proyecto transversal donde Dataplex gestiona los DataScans
DATAPLEX_PROJECT = "rs-nprd-dlk-transversal"   # ← ajusta si es diferente


@app.get("/")
async def health():
    return {"status": "ok"}


@app.get(
    "/projects/{project}/datasets/{dataset}/tables/{table}",
    response_model=TableStatus,
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
    """
    Genera las descripciones para la tabla.

    Flujo:
        1. get_table_profile()  → busca o crea el DataScan en el proyecto
                                   transversal de Dataplex. Reutiliza el perfil
                                   si tiene < 7 días. Sin transferencia de datos
                                   a Cloud Run.
        2. get_table_metadata() → obtiene schema BQ (liviano).
        3. build_prompt()       → construye el prompt con perfil + schema.
        4. generate_metadata()  → llama al LLM.
        5. validate_metadata()  → valida el schema del payload.
    """
    try:
        p, d, t = project.strip(), dataset.strip(), table.strip()
        # ── Paso 1: Perfilamiento vía Dataplex ───────────────────────────────
        profile = get_table_profile(
            project=p,                        # proyecto de la tabla BQ
            dataset=d,
            table_id=t,
            dataplex_project=DATAPLEX_PROJECT, # proyecto transversal
            location="us-central1",            # ← ajusta a tu región
            sample_percent=1.0,
            max_age_days=7,
            # (opcional)
            # results_project=DATAPLEX_PROJECT,
            # results_dataset="profiling_results",
            # results_table="dataplex_profiles",
        )

        if not profile:
            raise HTTPException(
                status_code=422,
                detail=f"No se pudo obtener el perfil de {p}.{d}.{t}",
            )

        # ── Paso 2: Schema BQ + Prompt + LLM ─────────────────────────────────
        table_obj = get_table_metadata(p, d, t)
        prompt    = build_prompt(table=table_obj, profile=profile)
        payload   = generate_metadata(prompt)

        # ── Paso 3: Validar ───────────────────────────────────────────────────
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
async def approve(
    project: str, dataset: str, table: str, payload: TableMetadata
) -> TableMetadata:
    """Recibe el JSON revisado por la persona y aplica las descripciones en BigQuery."""
    try:
        table_fqn = f"{project.strip()}.{dataset.strip()}.{table.strip()}"

        # ── Paso 1: Actualizar BigQuery ───────────────────────────────────────
        update_table_metadata(table_fqn, payload.model_dump())
        logger.info(f"[Approve] BigQuery actualizado para: {table_fqn}")

        # ── Paso 2: Publicar en Dataplex ──────────────────────────────────────
        dataplex_result = upsert_dataplex_aspects(payload.model_dump())

        if dataplex_result.success:
            logger.info(
                f"[Approve] Dataplex actualizado para: {table_fqn}. "
                f"Aspects: {dataplex_result.aspects_updated}"
            )
        else:
            # BigQuery ya se escribió — no forzamos re-aprobación por fallo de Dataplex
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
