import logging
from threading import Lock
from google.cloud import bigquery

logger = logging.getLogger(__name__)

_client_cache = {}
_lock = Lock()


def get_bq_client(project_id: str) -> bigquery.Client:
    """
    Retorna un cliente BigQuery reutilizable por proyecto.
    Thread-safe para Cloud Run Jobs.
    """

    with _lock:
        client = _client_cache.get(project_id)

        if client is None:
            logger.info(f"Creando cliente BigQuery para proyecto: {project_id}")
            client = bigquery.Client(project=project_id)
            _client_cache[project_id] = client

    return client
