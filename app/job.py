"""
Cloud Run Job — Batch metadata generator

Variables de entorno requeridas:
  PROJECT_ID   → proyecto de GCP  (ej: my-gcp-project)
  DATASETS     → datasets separados por coma (ej: dataset_a,dataset_b)

Variables de entorno opcionales:
  CONCURRENCY  → tablas en paralelo (default: 10)
  MAX_RETRIES  → reintentos por tabla en rate limit (default: 3)
"""

import asyncio
import logging
import os
import time

from google.cloud import bigquery

from app.adapters.bq_reader import get_table_metadata
from app.adapters.vertex_llm import generate_metadata
from app.adapters.bq_writer import update_table_schema
from app.services.profiling import build_profile
from app.services.prompt_builder import build_prompt
from app.validators.metadata_schema import validate_metadata

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

# ── Configuración ─────────────────────────────────────────────────────────────
PROJECT_ID  = os.environ["PROJECT_ID"]
DATASETS    = [d.strip() for d in os.environ["DATASETS"].split(",") if d.strip()]
CONCURRENCY = int(os.getenv("CONCURRENCY", "10"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))


# ── Pipeline por tabla ────────────────────────────────────────────────────────
def run_pipeline(project: str, dataset: str, table: str, bq_client: bigquery.Client) -> dict:
    """Pipeline completo para una tabla: profiling → LLM → validación → escritura BQ."""
    table_obj = get_table_metadata(project, dataset, table)
    profile   = build_profile(table=table_obj, bq_client=bq_client)
    prompt    = build_prompt(table=table_obj, profile=profile)
    payload   = generate_metadata(prompt)

    errors = validate_metadata(payload)
    if errors:
        raise ValueError(f"Invalid metadata: {errors}")

    update_table_schema(payload)
    return payload


# ── Helpers ───────────────────────────────────────────────────────────────────
def list_tables(client: bigquery.Client, project: str, dataset: str) -> list[tuple[str, str, str]]:
    tables = client.list_tables(f"{project}.{dataset}")
    return [(project, dataset, t.table_id) for t in tables]


async def process_table(
    project: str,
    dataset: str,
    table: str,
    bq_client: bigquery.Client,
    semaphore: asyncio.Semaphore,
    stats: dict,
) -> None:
    table_fqn = f"{project}.{dataset}.{table}"
    loop = asyncio.get_event_loop()

    async with semaphore:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                await loop.run_in_executor(None, run_pipeline, project, dataset, table, bq_client)
                stats["ok"] += 1
                logger.info(f"[OK] {table_fqn}")
                return  # éxito, salir del loop de reintentos

            except Exception as e:
                error_msg = str(e)
                is_rate_limit = "429" in error_msg or "ResourceExhausted" in error_msg

                if is_rate_limit and attempt < MAX_RETRIES:
                    wait = 60 * attempt  # backoff incremental: 60s, 120s, 180s
                    logger.warning(f"[RATE LIMIT] {table_fqn} — intento {attempt}/{MAX_RETRIES}, esperando {wait}s")
                    await asyncio.sleep(wait)
                else:
                    stats["errors"] += 1
                    logger.error(f"[ERROR] {table_fqn} — intento {attempt}/{MAX_RETRIES}: {error_msg}")
                    return  # agotó reintentos o error no recuperable


# ── Main ──────────────────────────────────────────────────────────────────────
async def main() -> None:
    bq_client = bigquery.Client(project=PROJECT_ID)
    semaphore = asyncio.Semaphore(CONCURRENCY)
    stats     = {"ok": 0, "errors": 0}

    # 1. Recolectar todas las tablas
    all_tables: list[tuple[str, str, str]] = []
    for dataset in DATASETS:
        try:
            tables = list_tables(bq_client, PROJECT_ID, dataset)
            all_tables.extend(tables)
            logger.info(f"Dataset {dataset}: {len(tables)} tablas encontradas")
        except Exception as e:
            logger.error(f"No se pudo listar el dataset {dataset}: {e}")

    total = len(all_tables)
    logger.info(f"Total tablas a procesar: {total}")

    if total == 0:
        logger.warning("No se encontraron tablas. Verifica PROJECT_ID y DATASETS.")
        return

    # 2. Procesar en paralelo controlado por semáforo
    start = time.time()
    tasks = [
        process_table(project, dataset, table, bq_client, semaphore, stats)
        for project, dataset, table in all_tables
    ]
    await asyncio.gather(*tasks)

    elapsed = round(time.time() - start, 1)
    logger.info(
        f"Job finalizado en {elapsed}s — "
        f"OK: {stats['ok']} | Errores: {stats['errors']} | Total: {total}"
    )


if __name__ == "__main__":
    asyncio.run(main())