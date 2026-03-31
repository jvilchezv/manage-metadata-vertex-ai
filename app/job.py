"""
Cloud Run Job — Batch metadata generator

Variables de entorno requeridas:
  PROJECTS        → proyectos separados por coma (ej: proy1,proy2,proy3)

Variables de entorno opcionales:
  EXCLUDE_TABLES  → tablas a excluir, separadas por coma (ej: tabla1,tabla2)
  CONCURRENCY     → tablas en paralelo (default: 10)
  MAX_RETRIES     → reintentos por tabla en rate limit (default: 3)
"""

import asyncio
import logging
import os
import time

from google.cloud import bigquery

from app.adapters.bq_reader import get_table_metadata
from app.adapters.bq_writer import update_table_schema
# from app.adapters.dataplex_writer import update_dataplex_aspect
from app.adapters.vertex_llm import generate_metadata
from app.services.profiling import build_profile
from app.services.prompt_builder import build_prompt
from app.validators.metadata_schema import validate_metadata

# NUEVO: importar el writer de Dataplex para publicar los 3 aspects en el batch
from app.services.dataplex_writer import upsert_dataplex_aspects

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

# ── Configuración ─────────────────────────────────────────────────────────────
PROJECTS       = [p.strip() for p in os.environ["PROJECTS"].split(",") if p.strip()]
EXCLUDE_TABLES = [t.strip() for t in os.getenv("EXCLUDE_TABLES", "").split(",") if t.strip()]
CONCURRENCY    = int(os.getenv("CONCURRENCY", "10"))
MAX_RETRIES    = int(os.getenv("MAX_RETRIES", "3"))


# ── Listar tablas via INFORMATION_SCHEMA ──────────────────────────────────────
def list_all_tables(client: bigquery.Client, project: str) -> list[tuple[str, str, str]]:
    """Lista todas las tablas del proyecto excluyendo las indicadas en EXCLUDE_TABLES."""
    exclude_str = ", ".join(f"'{t}'" for t in EXCLUDE_TABLES) if EXCLUDE_TABLES else "''"

    query = f"""
        SELECT table_catalog, table_schema, table_name
        FROM `{project}`.INFORMATION_SCHEMA.TABLES
        WHERE table_type = 'BASE TABLE'
          AND table_name NOT IN ({exclude_str})
        ORDER BY table_schema, table_name
    """

    rows = client.query(query).result()
    return [(row.table_catalog, row.table_schema, row.table_name) for row in rows]


# ── Pipeline por tabla ────────────────────────────────────────────────────────
def run_pipeline(project: str, dataset: str, table: str, bq_client: bigquery.Client) -> dict:
    """Pipeline completo: profiling → LLM → validación → BQ → Dataplex."""
    table_obj = get_table_metadata(project, dataset, table)
    profile   = build_profile(table=table_obj, bq_client=bq_client)
    prompt    = build_prompt(table=table_obj, profile=profile)
    payload   = generate_metadata(prompt)

    errors = validate_metadata(payload)
    if errors:
        raise ValueError(f"Invalid metadata: {errors}")

    # Escritura en paralelo: BigQuery y Dataplex
    update_table_schema(payload)

    # update_dataplex_aspect(payload)
    logger.info(f"[Pipeline] BigQuery actualizado: {table_fqn}")

    # ── Paso 7: Publicar en Dataplex ──────────────────────────────────────────
    # Construye y publica los 3 Aspect Types en Dataplex Catalog.
    # Usa el mismo payload validado, no hace ninguna transformación previa.
    # Si falla Dataplex, se loguea el error pero NO se interrumpe el batch
    # para no bloquear el procesamiento de las tablas restantes.
    dataplex_result = upsert_dataplex_aspects(payload)

    if dataplex_result.success:
        logger.info(
            f"[Pipeline] Dataplex actualizado: {table_fqn}. "
            f"Aspects: {dataplex_result.aspects_updated}"
        )
    else:
        logger.error(
            f"[Pipeline] Dataplex falló para {table_fqn}: {dataplex_result.errors}. "
            f"BigQuery ya fue actualizado. Dataplex puede re-intentarse luego."
        )

    return payload


# ── Procesamiento por tabla ───────────────────────────────────────────────────
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
                return

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
                    return


# ── Main ──────────────────────────────────────────────────────────────────────
async def main() -> None:
    bq_client = bigquery.Client()
    semaphore = asyncio.Semaphore(CONCURRENCY)
    stats     = {"ok": 0, "errors": 0}

    # 1. Recolectar todas las tablas de todos los proyectos
    all_tables: list[tuple[str, str, str]] = []
    for project in PROJECTS:
        try:
            tables = list_all_tables(bq_client, project)
            all_tables.extend(tables)
            logger.info(f"Proyecto {project}: {len(tables)} tablas encontradas")
        except Exception as e:
            logger.error(f"No se pudo acceder al proyecto {project}: {e}")

    total = len(all_tables)
    logger.info(f"Total tablas a procesar: {total}")

    if total == 0:
        logger.warning("No se encontraron tablas. Verifica PROJECTS y EXCLUDE_TABLES.")
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