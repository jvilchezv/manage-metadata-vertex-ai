"""
Publica metadata estructurada en Dataplex Universal Catalog usando Aspects.

Este writer:
- Toma el payload aprobado generado por una API (Gemini)
- Construye un Aspect que sigue estrictamente el Aspect Type "custom-table-descriptions"
- Hace UPSERT del Aspect sobre el Entry de BigQuery en Dataplex

Aspect Type utilizado:
    - custom-table-descriptions

Requisitos:
- El Aspect Type debe existir previamente en Dataplex
- El Entry de BigQuery debe existir (auto-discovery)
"""

import logging
import os
from dataclasses import dataclass, field
from typing import List

import google.auth
import google.auth.transport.requests
import requests

logger = logging.getLogger(__name__)

GOVERNANCE_PROJECT = os.getenv("GOVERNANCE_PROJECT")

DATAPLEX_LOCATION = os.getenv("DATAPLEX_LOCATION", "us-east4")

ASPECT_TYPE_DESCRIPTIONS = os.getenv(
    "ASPECT_TYPE_DESCRIPTIONS", "custom-table-descriptions"
)

_DATAPLEX_API_BASE = "https://dataplex.googleapis.com/v1"


@dataclass
class DataplexWriteResult:
    success: bool
    table_fqn: str
    entry_name: str
    aspects_updated: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


def _parse_fqn(table_fqn: str) -> tuple[str, str, str]:
    normalized = table_fqn.replace(":", ".")
    parts = normalized.split(".")

    if len(parts) != 3:
        raise ValueError(
            f"table_fqn inválido: '{table_fqn}'. "
            "Formato esperado: project.dataset.table"
        )
    return parts[0], parts[1], parts[2]


def _build_entry_name(bq_project: str, dataset: str, table: str) -> str:
    fqn_path = (
        f"bigquery.googleapis.com/projects/{bq_project}"
        f"/datasets/{dataset}/tables/{table}"
    )

    entry_group = (
        f"projects/{GOVERNANCE_PROJECT}"
        f"/locations/{DATAPLEX_LOCATION}"
        f"/entryGroups/@bigquery"
    )

    return f"{entry_group}/entries/{fqn_path}"


def _aspect_type_resource(aspect_type_id: str) -> str:
    return (
        f"projects/{GOVERNANCE_PROJECT}"
        f"/locations/{DATAPLEX_LOCATION}"
        f"/aspectTypes/{aspect_type_id}"
    )


def _aspect_map_key(aspect_type_id: str) -> str:
    return f"{GOVERNANCE_PROJECT}.{DATAPLEX_LOCATION}.{aspect_type_id}"


def _get_credentials():
    credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    credentials.refresh(google.auth.transport.requests.Request())
    return credentials


def _build_descriptions_aspect(payload: dict) -> dict:
    table_desc = payload.get("table_description", {})
    columns = payload.get("columns", [])
    model = payload.get("model", {})

    return {
        "aspectType": _aspect_type_resource(ASPECT_TYPE_DESCRIPTIONS),
        "data": {
            "description": table_desc.get("description", ""),
            "fields": [
                {
                    "name": col.get("name", ""),
                    "description": col.get("description", ""),
                    "is_computed": col.get("is_computed", False),
                    "sensitivity": col.get("sensitivity", False),
                }
                for col in columns
            ],
            "user_managed": False,
            "job_details": {
                "job_name": (f"{model.get('name', '')}:{model.get('version', '')}"),
                "job_start_time": payload.get("generated_at", ""),
            },
        },
    }


def upsert_dataplex_aspects(payload: dict) -> DataplexWriteResult:
    table_fqn = payload.get("table_fqn", "")

    try:
        bq_project, dataset, table = _parse_fqn(table_fqn)
    except ValueError as e:
        logger.error(f"[Dataplex] FQN inválido: {e}")
        return DataplexWriteResult(
            success=False,
            table_fqn=table_fqn,
            entry_name="",
            errors=[str(e)],
        )

    entry_name = _build_entry_name(bq_project, dataset, table)
    logger.info(f"[Dataplex] Upsert Aspect para: {table_fqn}")
    logger.debug(f"[Dataplex] Entry: {entry_name}")

    credentials = _get_credentials()

    headers = {
        "Authorization": f"Bearer {credentials.token}",
        "Content-Type": "application/json",
    }

    aspects_body = {
        _aspect_map_key(ASPECT_TYPE_DESCRIPTIONS): _build_descriptions_aspect(payload)
    }

    url = f"{_DATAPLEX_API_BASE}/{entry_name}"
    params = {"updateMask": "aspects"}
    body = {"aspects": aspects_body}

    try:
        response = requests.patch(
            url,
            headers=headers,
            params=params,
            json=body,
            timeout=(5, 25),
        )
        response.raise_for_status()

        logger.info(f"[Dataplex] Aspect actualizado correctamente para {table_fqn}")

        return DataplexWriteResult(
            success=True,
            table_fqn=table_fqn,
            entry_name=entry_name,
            aspects_updated=[ASPECT_TYPE_DESCRIPTIONS],
        )

    except requests.exceptions.HTTPError:
        error_msg = f"HTTP {response.status_code} en Dataplex: {response.text}"
        logger.error(f"[Dataplex] {error_msg}")
        return DataplexWriteResult(
            success=False,
            table_fqn=table_fqn,
            entry_name=entry_name,
            errors=[error_msg],
        )

    except requests.exceptions.Timeout:
        error_msg = "Timeout al llamar Dataplex API"
        logger.error(f"[Dataplex] {error_msg}")
        return DataplexWriteResult(
            success=False,
            table_fqn=table_fqn,
            entry_name=entry_name,
            errors=[error_msg],
        )

    except requests.exceptions.RequestException as e:
        error_msg = f"Error de red Dataplex: {str(e)}"
        logger.error(f"[Dataplex] {error_msg}")
        return DataplexWriteResult(
            success=False,
            table_fqn=table_fqn,
            entry_name=entry_name,
            errors=[error_msg],
        )


def _publish_dataplex_background(payload: dict, table_fqn: str):
    """
    Publica metadata en Dataplex en segundo plano.
    Nunca lanza excepción hacia FastAPI.
    """
    try:
        logger.info(f"[Dataplex] (bg) Publicando metadata para {table_fqn}")
        result = upsert_dataplex_aspects(payload)

        if not result.success:
            logger.error(f"[Dataplex] (bg) Fallo para {table_fqn}: {result.errors}")
        else:
            logger.info(f"[Dataplex] (bg) Publicado correctamente para {table_fqn}")

    except Exception:
        logger.exception(f"[Dataplex] (bg) Error inesperado para {table_fqn}")
