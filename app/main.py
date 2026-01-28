from fastapi import FastAPI, HTTPException
from google.cloud import bigquery
import logging

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


app = FastAPI(title="Metadata Generator API")


@app.get("/")
async def health():
    return {"status": "ok"}


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
