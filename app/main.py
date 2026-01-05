from fastapi import FastAPI, HTTPException
from google.cloud import bigquery
import logging

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)

logger = logging.getLogger(__name__)

from adapters.bq_reader import get_table_metadata, get_table_status
from services.profiling import build_profile
from services.prompt_builder import build_prompt
from adapters.vertex_llm import generate_metadata
from validators.metadata_schema import validate_metadata
from models import TableMetadata, TableStatus


app = FastAPI()


@app.get("/health")
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
    "/projects/{project}/datasets/{dataset}/tables/{table}",
    response_model=TableMetadata,
)
async def generate_metadata_info(
    project: str, dataset: str, table: str
) -> TableMetadata:
    try:
        client = bigquery.Client()

        table_obj = get_table_metadata(project.strip(), dataset.strip(), table.strip())
        profile = build_profile(table=table_obj, bq_client=client)

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
