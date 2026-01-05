from fastapi import FastAPI, HTTPException
from google.cloud import bigquery
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)

logger = logging.getLogger(__name__)

from adapters.bq_reader import get_table_metadata
from services.profiling import build_profile
from services.prompt_builder import build_prompt
from adapters.vertex_llm import generate_metadata
from adapters.bq_reader import get_table_status
from validators.metadata_schema import validate_metadata
from models import GenerateMetadataRequest, TableMetadata, TableStatus, ColumnStatus


app = FastAPI()

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get(
    "/tables/{project}/{dataset}/{table}",
    response_model=TableStatus
)
def get_table_info(project: str, dataset: str, table: str):
    client = bigquery.Client()

    status = get_table_status(
        client=client,
        project=project,
        dataset=dataset,
        table=table
    )

    if not status.get("exists"):
        raise HTTPException(
            status_code=404,
            detail="Table not found"
        )

    return status


@app.post("/generate-metadata", response_model=TableMetadata)
def generate(request: GenerateMetadataRequest):
    try:
        client = bigquery.Client()

        table_obj = get_table_metadata(
            request.project,
            request.dataset,
            request.table
        )

        profile = build_profile(
            table=table_obj,
            bq_client=client
        )

        prompt = build_prompt(
            table=table_obj,
            profile=profile
        )

        payload = generate_metadata(prompt)

        errors = validate_metadata(payload)
        if errors:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "Invalid metadata schema",
                    "details": errors
                }
            )

        return payload

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error generating metadata")
        raise HTTPException(
            status_code=500,
            detail="Internal server error"
        )
