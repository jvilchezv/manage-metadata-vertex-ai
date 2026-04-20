import logging
import sys
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from job.bq_tracker import claim_pending_tables, batch_update_status
from job.processor import process_table
from job.config import JobConfig
from job.bq_client_factory import get_bq_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


def chunked(iterable, size):
    for i in range(0, len(iterable), size):
        yield iterable[i : i + size]


# cache local para evitar múltiples lookups al factory
_clients_cache = {}


def get_client_cached(project_id: str):
    client = _clients_cache.get(project_id)
    if client is None:
        client = get_bq_client(project_id)
        _clients_cache[project_id] = client
    return client


def run() -> None:
    cfg = JobConfig.from_env()

    # evita thundering herd entre múltiples tasks
    time.sleep(random.uniform(0, cfg.startup_jitter_sec))

    logger.info(
        f"Job iniciado | workers={cfg.max_workers} | "
        f"batch_size={cfg.batch_size} | tracker={cfg.tracker_table_fqn}"
    )

    tracker_client = get_bq_client(cfg.tracker_project)

    # claim de tablas
    job_id, tables = claim_pending_tables(
        tracker_client,
        tracker_table=cfg.tracker_table_fqn,
        batch_size=cfg.batch_size or 500,
    )

    if not tables:
        logger.info("No hay tablas pendientes. Job finalizado.")
        return

    logger.info(f"Tablas claimadas: {len(tables)} | job_id={job_id}")

    results = []
    stats = {"ok": 0, "error": 0}

    # tamaño de batch para escribir en BigQuery
    BATCH_UPDATE_SIZE = 200

    with ThreadPoolExecutor(max_workers=cfg.max_workers) as executor:
        for chunk in chunked(tables, cfg.max_workers):
            futures = {
                executor.submit(
                    process_table,
                    row["catalog"],
                    row["schema"],
                    row["table"],
                    get_client_cached(row["catalog"]),
                ): row
                for row in chunk
            }

            for future in as_completed(futures):
                row = futures[future]
                fqn = f"{row['catalog']}.{row['schema']}.{row['table']}"

                try:
                    result = future.result()

                    results.append(
                        {
                            "catalog": row["catalog"],
                            "schema": row["schema"],
                            "table": row["table"],
                            "estado": result["estado"],
                            "error": result["error"],
                        }
                    )

                    if result["estado"] == "OK":
                        stats["ok"] += 1
                        logger.info(f"[OK] {fqn}")
                    else:
                        stats["error"] += 1
                        logger.error(f"[ERROR] {fqn} — {result['error']}")

                    # manejo básico de rate limit
                    if result.get("error_type") == "RATE_LIMIT":
                        sleep_time = random.uniform(1, 3)
                        logger.warning(f"[RATE LIMIT] Pausando {sleep_time:.2f}s")
                        time.sleep(sleep_time)

                    # flush parcial a BigQuery
                    if len(results) >= BATCH_UPDATE_SIZE:
                        logger.info("Flushing batch parcial al tracker...")
                        batch_update_status(
                            tracker_client,
                            tracker_table=cfg.tracker_table_fqn,
                            rows=results,
                        )
                        results.clear()

                except Exception as exc:
                    error_msg = str(exc)[:500]

                    results.append(
                        {
                            "catalog": row["catalog"],
                            "schema": row["schema"],
                            "table": row["table"],
                            "estado": "ERROR",
                            "error": error_msg,
                        }
                    )

                    stats["error"] += 1
                    logger.error(f"[CRITICAL] {fqn} — {error_msg}")

    # flush final
    if results:
        logger.info("Flush final al tracker...")
        batch_update_status(
            tracker_client,
            tracker_table=cfg.tracker_table_fqn,
            rows=results,
        )

    total = stats["ok"] + stats["error"]

    logger.info(
        f"Job finalizado | ok={stats['ok']} | error={stats['error']} | total={total}"
    )

    if stats["error"] > 0:
        logger.warning("El job terminó con errores.")
        sys.exit(1)


if __name__ == "__main__":
    run()
