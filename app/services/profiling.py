"""
Adapter para perfilamiento de tablas BigQuery usando Dataplex Data Profile Scans.

Parámetros clave:
  - dataplex_project : proyecto transversal donde viven los DataScans
  - project          : proyecto donde vive la tabla BQ (puede ser diferente)

Flujo:
    1. find_scan_for_table()         → busca DataScan en proyecto transversal
    2. create_profile_scan()         → crea si no existe (ID automático)
    3. get_latest_successful_job()   → obtiene último job exitoso
    4. run_and_wait()                → lanza nuevo job si es necesario
    5. parse_profile_to_dict()       → convierte resultado al formato del prompt
"""

from __future__ import annotations

import datetime
import logging
import time
from typing import Any, Dict, List, Optional

from google.cloud import dataplex_v1
from google.cloud.dataplex_v1.types import (
    DataScan,
    DataScanJob,
    DataProfileResult,
    DataProfileSpec,
)
from google.api_core.exceptions import AlreadyExists

logger = logging.getLogger(__name__)

JOB_TIMEOUT_SECONDS = 600
JOB_POLL_INTERVAL_SECONDS = 15
PROFILE_MAX_AGE_DAYS = 7


def _bq_resource(project: str, dataset: str, table_id: str) -> str:
    return (
        f"//bigquery.googleapis.com/projects/{project}"
        f"/datasets/{dataset}/tables/{table_id}"
    )


def _bq_export_uri(project: str, dataset: str, table_id: str) -> str:
    return (
        f"//bigquery.googleapis.com/projects/{project}"
        f"/datasets/{dataset}/tables/{table_id}"
    )


def _safe_float(value: Any) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


# Busca un DataScan existente para la tabla BQ dada, sin depender de un ID fijo.
def find_scan_for_table(
    client: dataplex_v1.DataScanServiceClient,
    dataplex_project: str,  # proyecto transversal donde viven los DataScans
    location: str,
    table_project: str,  # proyecto donde vive la tabla BQ
    dataset: str,
    table_id: str,
) -> Optional[DataScan]:
    """
    Lista los DataScans del proyecto transversal y devuelve el primero
    que apunte a la tabla BQ indicada y sea de tipo DATA_PROFILE.
    """
    parent = f"projects/{dataplex_project}/locations/{location}"
    resource = _bq_resource(table_project, dataset, table_id)

    logger.info(f"Buscando DataScan en [{dataplex_project}] para recurso: {resource}")

    for scan in client.list_data_scans(parent=parent):
        if (
            scan.data.resource == resource
            and scan.type_ == DataScan.DataScanType.DATA_PROFILE
        ):
            logger.info(f"DataScan encontrado: {scan.name}")
            return scan

    logger.info("No se encontró DataScan existente.")
    return None


# Crea un DataScan de perfilamiento para la tabla dada, con ID automático y export opcional a BQ.
def create_profile_scan(
    client: dataplex_v1.DataScanServiceClient,
    dataplex_project: str,  # proyecto transversal donde se crea el DataScan
    location: str,
    table_project: str,  # proyecto donde vive la tabla BQ
    dataset: str,
    table_id: str,
    sample_percent: float = 5.0,
    results_project: Optional[str] = None,
    results_dataset: Optional[str] = None,
    results_table: Optional[str] = None,
) -> DataScan:
    """
    Crea un DataScan de perfilamiento en el proyecto transversal,
    apuntando a la tabla BQ del proyecto origen.
    ID automático generado por Dataplex.
    """
    parent = f"projects/{dataplex_project}/locations/{location}"
    resource = _bq_resource(table_project, dataset, table_id)

    # Export a BQ (opcional)
    post_scan_actions = None
    if results_project and results_dataset and results_table:
        export_uri = _bq_export_uri(results_project, results_dataset, results_table)
        logger.info(f"Resultados se exportarán a: {export_uri}")
        post_scan_actions = DataProfileSpec.PostScanActions(
            bigquery_export=DataProfileSpec.PostScanActions.BigQueryExport(
                results_table=export_uri
            )
        )

    new_scan = DataScan(
        data=dataplex_v1.DataSource(resource=resource),
        data_profile_spec=DataProfileSpec(
            sampling_percent=sample_percent,
            **({"post_scan_actions": post_scan_actions} if post_scan_actions else {}),
        ),
        description=f"Auto profile scan for {dataset}.{table_id}",
    )

    try:
        # Sin data_scan_id → Dataplex genera el ID automáticamente
        operation = client.create_data_scan(
            parent=parent,
            data_scan=new_scan,
        )
        scan = operation.result()
        logger.info(
            f"DataScan creado en [{dataplex_project}] con ID automático: {scan.name}"
        )
        return scan

    except AlreadyExists:
        logger.warning("AlreadyExists en create — reintentando búsqueda.")
        scan = find_scan_for_table(
            client, dataplex_project, location, table_project, dataset, table_id
        )
        if scan:
            return scan
        raise RuntimeError(
            "AlreadyExists pero no se encontró el scan en list_data_scans."
        )


# Obtiene el último DataScanJob exitoso para el scan dado, o None si no hay ninguno.
def get_latest_successful_job(
    client: dataplex_v1.DataScanServiceClient,
    scan_name: str,
) -> Optional[DataScanJob]:
    latest: Optional[DataScanJob] = None

    for job in client.list_data_scan_jobs(parent=scan_name):
        if job.state != DataScanJob.State.SUCCEEDED:
            continue
        if latest is None or job.end_time > latest.end_time:
            latest = job

    if latest:
        logger.info(f"Último job exitoso: {latest.name} | fin: {latest.end_time}")
    else:
        logger.info("No hay jobs exitosos previos.")

    return latest


# Lanza un job de perfilamiento y hace polling hasta que termine, devolviendo el resultado o lanzando error.
def run_and_wait(
    client: dataplex_v1.DataScanServiceClient,
    scan_name: str,
    timeout: int = JOB_TIMEOUT_SECONDS,
    poll_interval: int = JOB_POLL_INTERVAL_SECONDS,
) -> DataScanJob:
    logger.info(f"Lanzando job para: {scan_name}")
    response = client.run_data_scan(name=scan_name)
    job_name = response.job.name
    logger.info(f"Job lanzado: {job_name}")

    elapsed = 0
    while elapsed < timeout:
        time.sleep(poll_interval)
        elapsed += poll_interval

        job = client.get_data_scan_job(
            name=job_name,
            view=dataplex_v1.GetDataScanJobRequest.DataScanJobView.FULL,
        )
        logger.info(f"  Estado: {job.state.name} | {elapsed}s elapsed")

        if job.state == DataScanJob.State.SUCCEEDED:
            logger.info(f"Job completado: {job_name}")
            return job

        if job.state in (DataScanJob.State.FAILED, DataScanJob.State.CANCELLED):
            raise RuntimeError(
                f"Job Dataplex terminó con estado {job.state.name}: {job_name}"
            )

    raise TimeoutError(f"Job no terminó en {timeout}s: {job_name}")


# Convierte el DataProfileResult al formato estándar del prompt.
def parse_profile_to_dict(job: DataScanJob) -> Dict[str, Dict]:
    """
    Convierte el DataProfileResult al formato estándar del prompt:

        {
            "col": {
                "type":           "STRING",
                "mode":           "NULLABLE",
                "bq_description": "",
                "null_ratio":     0.02,
                "distinct_ratio": 0.98,
                "example_values": ["val1", "val2"],
                "min":            None,
                "max":            None,
                "mean":           None,
                "std_dev":        None,
                "quartiles":      [],
                "top_n_values":   [{"value": "X", "count": 10, "ratio": 0.05}],
            }
        }
    """
    result: DataProfileResult = job.data_profile_result
    profile: Dict[str, Dict] = {}

    if not result or not result.fields:
        logger.warning("DataProfileResult vacío o sin campos.")
        return profile

    row_count = result.row_count or 1

    for field in result.fields:
        name = field.name
        profile_info = field.profile

        null_count = getattr(profile_info, "null_count", 0) or 0
        distinct_count = getattr(profile_info, "distinct_count", 0) or 0
        non_null = max(row_count - null_count, 0)
        null_ratio = round(null_count / row_count, 3) if row_count else 0.0
        distinct_ratio = round(distinct_count / non_null, 3) if non_null > 0 else 0.0

        numeric = getattr(profile_info, "numeric_statistics", None)
        min_val = _safe_float(getattr(numeric, "min_value", None))
        max_val = _safe_float(getattr(numeric, "max_value", None))
        mean = _safe_float(getattr(numeric, "average", None))
        std_dev = _safe_float(getattr(numeric, "standard_deviation", None))

        quartiles: List[Optional[float]] = []
        if numeric and getattr(numeric, "quartiles", None):
            quartiles = [_safe_float(q) for q in numeric.quartiles]

        top_n_values: List[Dict] = []
        for item in getattr(profile_info, "top_n_values", []) or []:
            count = getattr(item, "count", 0) or 0
            top_n_values.append(
                {
                    "value": str(getattr(item, "value", "")),
                    "count": count,
                    "ratio": round(count / row_count, 4) if row_count else 0.0,
                }
            )

        profile[name] = {
            "type": getattr(field, "type_", "UNKNOWN"),
            "mode": getattr(field, "mode", "NULLABLE"),
            "bq_description": "",
            "null_ratio": null_ratio,
            "distinct_ratio": distinct_ratio,
            "example_values": [t["value"] for t in top_n_values[:10]],
            "min": min_val,
            "max": max_val,
            "mean": mean,
            "std_dev": std_dev,
            "quartiles": quartiles,
            "top_n_values": top_n_values,
        }

    logger.info(f"Perfil parseado: {len(profile)} columnas | {row_count:,} filas")
    return profile


# main
def get_table_profile(
    project: str,  # proyecto de la tabla BQ
    dataset: str,
    table_id: str,
    dataplex_project: str,  # proyecto transversal de Dataplex
    location: str = "us-central1",
    sample_percent: float = 5.0,
    force_rerun: bool = False,
    max_age_days: int = PROFILE_MAX_AGE_DAYS,
    results_project: Optional[str] = None,
    results_dataset: Optional[str] = None,
    results_table: Optional[str] = None,
) -> Dict[str, Dict]:
    """
    Punto de entrada principal.

    dataplex_project : proyecto transversal donde se crean/leen los DataScans
    project          : proyecto donde vive la tabla BQ a perfilar
    """
    client = dataplex_v1.DataScanServiceClient()

    # 1. Buscar scan en el proyecto transversal
    scan = find_scan_for_table(
        client=client,
        dataplex_project=dataplex_project,
        location=location,
        table_project=project,
        dataset=dataset,
        table_id=table_id,
    )

    # 2. Crear si no existe — en el proyecto transversal
    if scan is None:
        scan = create_profile_scan(
            client=client,
            dataplex_project=dataplex_project,
            location=location,
            table_project=project,
            dataset=dataset,
            table_id=table_id,
            sample_percent=sample_percent,
            results_project=results_project,
            results_dataset=results_dataset,
            results_table=results_table,
        )

    # 3. Último job exitoso
    latest_job = get_latest_successful_job(client, scan.name)

    # 4. Decidir si relanzar
    needs_rerun = force_rerun or latest_job is None

    if not needs_rerun and latest_job is not None:
        age = datetime.datetime.now(datetime.timezone.utc) - latest_job.end_time
        if age.days > max_age_days:
            logger.info(
                f"Perfil desactualizado ({age.days}d > {max_age_days}d). Relanzando."
            )
            needs_rerun = True
        else:
            logger.info(
                f"Perfil vigente (hace {age.days}d). Usando resultado existente."
            )

    if needs_rerun:
        latest_job = run_and_wait(client, scan.name)

    # 5. Parsear y devolver
    return parse_profile_to_dict(latest_job)
