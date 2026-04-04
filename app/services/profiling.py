from typing import Any, Dict, List, Optional
from google.cloud import bigquery
import logging
import base64
import datetime
import json
from decimal import Decimal

from app.adapters.bq_reader import get_partition_field, get_max_partition

logger = logging.getLogger(__name__)


def _is_missing(value: Any) -> bool:
    """Considera ausente None o lista vacía (REPEATED sin elementos)."""
    if value is None:
        return True
    if isinstance(value, list) and len(value) == 0:
        return True
    return False


def _normalize_for_hash(value: Any) -> Any:
    """
    Convierte cualquier valor a algo hasheable para comparación/deduplicación.
    """
    if isinstance(value, dict):
        return tuple(sorted((k, _normalize_for_hash(v)) for k, v in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_normalize_for_hash(v) for v in value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime.datetime, datetime.date)):
        if isinstance(value, datetime.datetime) and value.tzinfo:
            return value.astimezone(datetime.timezone.utc).isoformat()
        return value.isoformat()
    return value


def _to_display(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    except TypeError:
        norm = _normalize_for_hash(value)
        return json.dumps(norm, ensure_ascii=False, separators=(",", ":"), default=str)


def _get_partition_bq_type(
    table: bigquery.Table, partition_field_name: Optional[str]
) -> Optional[str]:
    """Obtiene el tipo BigQuery del campo de partición directamente desde el schema."""
    if not partition_field_name:
        return None
    for f in table.schema:
        if f.name == partition_field_name:
            return f.field_type.upper()
    return None


def _build_profile_query(
    fq_table: str,
    cols_select_str: str,
    partition_field: Optional[str],
    partition_bq_type: Optional[str],
    max_partition: Any,
    sample_percent: int,
) -> tuple:
    """
    Construye la query de perfilado con:
    - TABLESAMPLE SYSTEM para muestrear ~sample_percent% de la tabla.
    - Filtro DATE_TRUNC al mes más reciente si la tabla tiene partición por fecha.

    Retorna (query_str, job_config_o_None).
    """
    sample_clause = f"TABLESAMPLE SYSTEM ({sample_percent} PERCENT)"

    if partition_field and partition_bq_type in ("TIMESTAMP", "DATE", "DATETIME"):
        cast_type = partition_bq_type
        query = f"""
        SELECT {cols_select_str}
        FROM `{fq_table}` {sample_clause}
        WHERE DATE_TRUNC(CAST(`{partition_field}` AS {cast_type}), MONTH)
            = DATE_TRUNC(CAST(@max_partition AS {cast_type}), MONTH)
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("max_partition", cast_type, max_partition)
            ]
        )
        return query, job_config

    query = f"SELECT {cols_select_str} FROM `{fq_table}` {sample_clause}"
    return query, None


# ---------------------------------------------------------------------------
# Acumulador por columna — toda la lógica de estadísticas en un solo objeto
# para evitar múltiples dicts paralelos.
# ---------------------------------------------------------------------------

class _ColStats:
    """
    Acumula estadísticas de una columna de forma incremental.

    Memoria usada por columna:
      - col_examples   : máximo max_examples valores crudos  →  O(max_examples)
      - seen_keys      : set de claves normalizadas para dedup de ejemplos  →  O(max_examples)
      - distinct_keys  : set de TODOS los valores normalizados vistos  →  puede crecer
      - missing_count  : int
      - non_null_count : int

    Para tablas con cardinalidad muy alta (IDs, UUIDs), distinct_keys puede
    llegar a ser grande. Si eso es un problema, reemplaza el set por un
    HyperLogLog (e.g. paquete `hyperloglog`). Por ahora el set es la opción
    más sencilla y fiel.
    """

    __slots__ = (
        "missing_count",
        "non_null_count",
        "examples",        # valores crudos (hasta max_examples)
        "seen_keys",       # keys normalizadas de examples (dedup de ejemplos)
        "distinct_keys",   # keys normalizadas de TODOS los valores (distinct_ratio)
        "max_examples",
    )

    def __init__(self, max_examples: int) -> None:
        self.max_examples = max_examples
        self.missing_count: int = 0
        self.non_null_count: int = 0
        self.examples: List[Any] = []
        self.seen_keys: set = set()
        self.distinct_keys: set = set()

    def feed(self, value: Any) -> None:
        if _is_missing(value):
            self.missing_count += 1
            return

        self.non_null_count += 1
        key = _normalize_for_hash(value)

        # Acumula para distinct_ratio (siempre)
        self.distinct_keys.add(key)

        # Acumula ejemplos deduplicados (hasta max_examples)
        if len(self.examples) < self.max_examples and key not in self.seen_keys:
            self.examples.append(value)
            self.seen_keys.add(key)

    def null_ratio(self, total_rows: int) -> float:
        return round(self.missing_count / total_rows, 3) if total_rows else 0.0

    def distinct_ratio(self) -> float:
        if self.non_null_count == 0:
            return 0.0
        return round(len(self.distinct_keys) / self.non_null_count, 3)

    def example_values(self) -> List[str]:
        return [_to_display(v) for v in self.examples]


# ---------------------------------------------------------------------------
# Función principal
# ---------------------------------------------------------------------------

def build_profile(
    table: bigquery.Table,
    bq_client: bigquery.Client,
    max_examples: int = 10,
    sample_percent: int = 10,
    page_size: int = 5_000,
    max_distinct_keys: int = 100_000,
) -> Dict[str, Dict]:
    """
    Perfila una tabla BigQuery usando todos sus campos (incluye bq_description
    de cada SchemaField) y un muestreo aproximado del sample_percent% de los
    datos del mes más reciente (si la tabla tiene partición por fecha) o de
    toda la tabla.

    Optimizado para tablas grandes (15M+ filas):
      - Itera el resultado como un generador (no materializa la lista completa).
      - Usa page_size para controlar el buffer interno de la API de BQ.
      - Acumula estadísticas in-place con _ColStats (sin dicts paralelos).
      - distinct_keys se limita a max_distinct_keys para evitar OOM en columnas
        de cardinalidad extrema (UUIDs, hashes). Cuando se supera el límite,
        distinct_ratio se marca como ">límite" en los logs y se aproxima.

    Parámetros
    ----------
    table            : objeto bigquery.Table con schema ya cargado.
    bq_client        : cliente BigQuery autenticado.
    max_examples     : máximo de valores de ejemplo por columna.
    sample_percent   : porcentaje de TABLESAMPLE (1-100).
    page_size        : filas por página que devuelve la API de BQ.
    max_distinct_keys: límite de cardinalidad para el set de distintos.
                       Reduce si sigues teniendo presión de memoria.

    Retorna un dict keyed por nombre de columna con:
        - type            : tipo BQ del campo
        - mode            : NULLABLE / REQUIRED / REPEATED
        - bq_description  : descripción registrada en BQ (puede ser "")
        - example_values  : lista de strings JSON deduplicados
        - null_ratio      : proporción de nulos/vacíos sobre total de filas
        - distinct_ratio  : proporción de valores únicos sobre no-nulos
    """
    fq_table = f"{table.project}.{table.dataset_id}.{table.table_id}"
    logger.info(f"Profiling table: {fq_table} | sample={sample_percent}%")

    all_cols = [f.name for f in table.schema]
    if not all_cols:
        logger.error(f"El schema de {fq_table} está vacío.")
        return {}

    cols_select_str = ", ".join([f"`{c}`" for c in all_cols])

    partition_field = get_partition_field(table)
    partition_bq_type = _get_partition_bq_type(table, partition_field)
    max_partition = None

    if partition_field and partition_bq_type in ("TIMESTAMP", "DATE", "DATETIME"):
        max_partition = get_max_partition(bq_client, fq_table, partition_field)
        logger.info(
            f"Partición detectada: '{partition_field}' ({partition_bq_type}). "
            f"Max partition: {max_partition}"
        )
    elif partition_field:
        logger.info(
            f"Partición no-fecha detectada: '{partition_field}' ({partition_bq_type}). "
            "Solo se aplica TABLESAMPLE."
        )

    query, job_config = _build_profile_query(
        fq_table=fq_table,
        cols_select_str=cols_select_str,
        partition_field=partition_field,
        partition_bq_type=partition_bq_type,
        max_partition=max_partition,
        sample_percent=sample_percent,
    )

    logger.debug(f"Profiling query:\n{query}")

    # ------------------------------------------------------------------
    # Ejecutar la query y configurar el tamaño de página para controlar
    # cuántos datos se descargan a RAM en cada iteración.
    # ------------------------------------------------------------------
    query_job = bq_client.query(query, job_config=job_config)
    result_iter = query_job.result(page_size=page_size)

    # ------------------------------------------------------------------
    # Inicializar acumuladores — un _ColStats por columna
    # ------------------------------------------------------------------
    stats: Dict[str, _ColStats] = {
        c: _ColStats(max_examples) for c in all_cols
    }
    total_rows: int = 0

    # ------------------------------------------------------------------
    # Iterar fila a fila SIN materializar en memoria
    # ------------------------------------------------------------------
    log_every = 500_000
    for row in result_iter:
        total_rows += 1

        for c in all_cols:
            col_stat = stats[c]
            value = row[c]

            # Límite de cardinalidad para columnas de alta cardinalidad
            if (
                not _is_missing(value)
                and len(col_stat.distinct_keys) >= max_distinct_keys
            ):
                # Solo actualizamos missing/non_null, no el set de distintos
                col_stat.non_null_count += 1
                # Seguimos acumulando ejemplos si aún no tenemos suficientes
                if len(col_stat.examples) < max_examples:
                    key = _normalize_for_hash(value)
                    if key not in col_stat.seen_keys:
                        col_stat.examples.append(value)
                        col_stat.seen_keys.add(key)
                continue

            col_stat.feed(value)

        if total_rows % log_every == 0:
            logger.info(f"  ...procesadas {total_rows:,} filas de {fq_table}")

    if total_rows == 0:
        logger.warning(
            f"Sin filas para {fq_table}. El sample puede ser demasiado pequeño."
        )
        return {}

    # ------------------------------------------------------------------
    # Construir el perfil final
    # ------------------------------------------------------------------
    profile: Dict[str, Dict] = {}

    for field in table.schema:
        name = field.name
        col_stat = stats[name]

        capped = len(col_stat.distinct_keys) >= max_distinct_keys
        if capped:
            logger.warning(
                f"Columna '{name}': distinct_keys alcanzó el límite "
                f"({max_distinct_keys:,}). distinct_ratio es una cota inferior."
            )

        profile[name] = {
            "type": field.field_type,
            "mode": field.mode,
            "bq_description": (field.description or "").strip(),
            "example_values": col_stat.example_values(),
            "null_ratio": col_stat.null_ratio(total_rows),
            "distinct_ratio": col_stat.distinct_ratio(),
            "distinct_capped": capped,  # bandera útil para el consumidor
        }

    logger.info(
        f"Perfilado completado: {len(profile)} columnas | {total_rows:,} filas muestreadas"
    )
    return profile