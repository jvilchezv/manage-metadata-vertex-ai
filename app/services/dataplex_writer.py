"""
dataplex_writer.py
==================
Ubicación: app/services/dataplex_writer.py

Responsabilidad:
    Recibe el JSON de metadata aprobado (generado por Gemini y validado por
    metadata_schema.py) y publica los 3 Aspect Types en Dataplex Catalog
    mediante la API REST de Dataplex.

¿Qué es un Aspect en Dataplex?
    Un Aspect es un bloque de metadata estructurada que se adjunta a un Entry
    (en este caso, una tabla de BigQuery). Cada Aspect sigue una plantilla
    predefinida llamada Aspect Type, que ya fue creada en el proyecto de
    gobierno de datos.

Los 3 Aspect Types que se publican:
    1. table-description  → descripción general de la tabla
    2. column-description → descripción de cada columna
    3. sensibility        → clasificación de sensibilidad de datos

Prerequisitos:
    - Los 3 Aspect Types ya deben existir en Dataplex (creados previamente).
    - Las tablas de BigQuery ya deben estar registradas en Dataplex como
      Entries (el auto-discovery de Dataplex las registra automáticamente).
    - La Service Account del proceso debe tener rol:
      roles/dataplex.catalogEditor en el proyecto de gobierno.

Configuración (variables de entorno recomendadas para Cloud Run):
    GOVERNANCE_PROJECT      → proyecto GCP donde viven los Aspect Types
    DATAPLEX_LOCATION       → región del lago Dataplex (ej: "us-central1")
    ASPECT_TYPE_TABLE       → ID del Aspect Type de descripción de tabla
    ASPECT_TYPE_COLUMN      → ID del Aspect Type de descripción de columnas
    ASPECT_TYPE_SENSIBILITY → ID del Aspect Type de sensibilidad (confirmado: "sensibility")

Flujo general:
    JSON aprobado
        → _build_table_aspect()       → aspect tabla
        → _build_column_aspect()      → aspect columnas
        → _build_sensibility_aspect() → aspect sensibilidad
        → upsert_dataplex_aspects()   → PATCH a la API de Dataplex
"""

import logging
import os
from dataclasses import dataclass, field

import google.auth
import google.auth.transport.requests
import requests

logger = logging.getLogger(__name__)


# =============================================================================
# SECCIÓN 1: CONSTANTES DE CONFIGURACIÓN
# =============================================================================
# Estas constantes identifican dónde viven los Aspect Types en GCP.
# Se leen desde variables de entorno para facilitar el despliegue en
# Cloud Run sin modificar el código. Si no están definidas, usan el
# valor por defecto indicado (útil para pruebas locales).
#
# IMPORTANTE: Actualizar GOVERNANCE_PROJECT, ASPECT_TYPE_TABLE y
# ASPECT_TYPE_COLUMN con los valores reales antes de ejecutar.
# =============================================================================

# Proyecto GCP donde están creados los Aspect Types (proyecto de gobierno)
GOVERNANCE_PROJECT = os.getenv("GOVERNANCE_PROJECT", "your-governance-project-id")

# Región de Dataplex donde está el lago y los Aspect Types
DATAPLEX_LOCATION = os.getenv("DATAPLEX_LOCATION", "us-central1")

# IDs de los 3 Aspect Types — deben coincidir exactamente con los creados en Dataplex
ASPECT_TYPE_TABLE       = os.getenv("ASPECT_TYPE_TABLE",       "table-description")
ASPECT_TYPE_COLUMN      = os.getenv("ASPECT_TYPE_COLUMN",      "column-description")
ASPECT_TYPE_SENSIBILITY = os.getenv("ASPECT_TYPE_SENSIBILITY", "sensibility")

# URL base de la API REST de Dataplex Catalog
_DATAPLEX_API_BASE = "https://dataplex.googleapis.com/v1"

# Jerarquía de clasificación de sensibilidad, ordenada de mayor a menor riesgo.
# Se usa para determinar el nivel más restrictivo entre todas las columnas.
_SENSITIVITY_HIERARCHY = ["Highly sensitive", "Confidential", "Internal", "Public"]


# =============================================================================
# SECCIÓN 2: MODELO DE RESULTADO
# =============================================================================

@dataclass
class DataplexWriteResult:
    """
    Resultado de la operación de upsert en Dataplex.

    Attributes:
        success:          True si los 3 aspects se publicaron correctamente.
        table_fqn:        FQN de la tabla procesada (project.dataset.table).
        entry_name:       Resource name completo del Entry en Dataplex.
        aspects_updated:  Lista de Aspect Type IDs que se actualizaron.
        errors:           Lista de mensajes de error si algo falló.
    """
    success: bool
    table_fqn: str
    entry_name: str
    aspects_updated: list[str] = field(default_factory=list)
    errors: list[str]          = field(default_factory=list)


# =============================================================================
# SECCIÓN 3: HELPERS INTERNOS
# =============================================================================

def _parse_fqn(table_fqn: str) -> tuple[str, str, str]:
    """
    Extrae los 3 componentes del Fully Qualified Name (FQN) de BigQuery.

    El FQN viene directamente del campo 'table_fqn' del JSON aprobado,
    por lo que el writer funciona con cualquier proyecto GCP sin
    necesitar parámetros adicionales.

    Acepta:
        "project.dataset.table"       → formato estándar
        "project:dataset.table"       → formato legacy de BigQuery

    Returns:
        Tupla (bq_project, dataset, table)

    Raises:
        ValueError: si el FQN no tiene exactamente 3 partes.
    """
    # Normalizar el separador legacy ":" al estándar "."
    normalized = table_fqn.replace(":", ".")
    parts = normalized.split(".")

    if len(parts) != 3:
        raise ValueError(
            f"table_fqn inválido: '{table_fqn}'. "
            f"Formato esperado: 'project.dataset.table'"
        )
    return parts[0], parts[1], parts[2]


def _build_entry_name(bq_project: str, dataset: str, table: str) -> str:
    """
    Construye el resource name del Entry en Dataplex para una tabla de BQ.

    Dataplex auto-discovery registra cada tabla de BigQuery como un Entry
    dentro del EntryGroup especial '@bigquery'. Este EntryGroup siempre
    vive en el proyecto de gobierno, aunque la tabla esté en otro proyecto.

    Formato resultante:
        projects/{governance_project}/locations/{location}
        /entryGroups/@bigquery
        /entries/bigquery.googleapis.com/projects/{bq_project}
        /datasets/{dataset}/tables/{table}

    Args:
        bq_project: proyecto GCP donde vive la tabla en BigQuery
        dataset:    dataset de BigQuery
        table:      nombre de la tabla
    """
    # Ruta que identifica a la tabla dentro del sistema de Dataplex
    fqn_path = (
        f"bigquery.googleapis.com/projects/{bq_project}"
        f"/datasets/{dataset}/tables/{table}"
    )

    # El EntryGroup @bigquery siempre está en el proyecto de gobierno
    entry_group = (
        f"projects/{GOVERNANCE_PROJECT}/locations/{DATAPLEX_LOCATION}"
        f"/entryGroups/@bigquery"
    )

    return f"{entry_group}/entries/{fqn_path}"


def _aspect_type_resource(aspect_type_id: str) -> str:
    """
    Construye el resource name completo de un Aspect Type.

    La API de Dataplex requiere el resource name completo como key
    en el body del PATCH, no solo el ID corto.

    Ejemplo:
        "sensibility"
        →  "projects/gov-project/locations/us-central1/aspectTypes/sensibility"
    """
    return (
        f"projects/{GOVERNANCE_PROJECT}/locations/{DATAPLEX_LOCATION}"
        f"/aspectTypes/{aspect_type_id}"
    )


def _get_credentials():
    """
    Obtiene credenciales de autenticación usando ADC
    (Application Default Credentials).

    En local:     usa las credenciales de 'gcloud auth application-default login'
    En Cloud Run: usa automáticamente la Service Account asignada al servicio

    No requiere ninguna configuración adicional en el código.
    """
    credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    credentials.refresh(google.auth.transport.requests.Request())
    return credentials


# =============================================================================
# SECCIÓN 4: BUILDERS DE CADA ASPECT
# =============================================================================
# Cada función toma el JSON completo aprobado y extrae solo los campos
# que corresponden a su Aspect Type. El resultado es el dict que se
# envía directamente en el body del PATCH a la API de Dataplex.
# =============================================================================

def _build_table_aspect(payload: dict) -> dict:
    """
    Construye el Aspect de descripción general de la tabla.

    Aspect Type: 'table-description'

    Mapeo desde el JSON aprobado:
        payload.table_description.description  → data.description
        payload.table_description.accuracy     → data.accuracy
        payload.model.name                     → data.model_name
        payload.model.version                  → data.model_version
        payload.generated_at                   → data.generated_at

    El campo 'accuracy' indica qué tan confiable es la descripción
    generada por el LLM (0.0 = sin confianza, 1.0 = muy confiable).
    """
    td    = payload.get("table_description", {})
    model = payload.get("model", {})

    return {
        "aspectType": _aspect_type_resource(ASPECT_TYPE_TABLE),
        "data": {
            "description":   td.get("description", ""),
            "accuracy":      td.get("accuracy", 0.0),
            "model_name":    model.get("name", ""),
            "model_version": model.get("version", ""),
            "generated_at":  payload.get("generated_at", ""),
        },
    }


def _build_column_aspect(payload: dict) -> dict:
    """
    Construye el Aspect de descripción de columnas.

    Aspect Type: 'column-description'

    Mapeo desde el JSON aprobado (por cada columna en payload.columns):
        col.name          → columns[].name
        col.description   → columns[].description
        col.accuracy      → columns[].accuracy
        col.is_computed   → columns[].is_computed

    NOTA: La sensibilidad de cada columna NO va aquí, va en el
    Aspect 'sensibility' por separado (_build_sensibility_aspect).
    """
    columns = payload.get("columns", [])

    return {
        "aspectType": _aspect_type_resource(ASPECT_TYPE_COLUMN),
        "data": {
            "columns": [
                {
                    "name":        col.get("name", ""),
                    "description": col.get("description", ""),
                    "accuracy":    col.get("accuracy", 0.0),
                    "is_computed": col.get("is_computed", False),
                }
                for col in columns
            ]
        },
    }


def _build_sensibility_aspect(payload: dict) -> dict:
    """
    Construye el Aspect de sensibilidad de la tabla.

    Aspect Type: 'sensibility'

    Este aspect agrega la sensibilidad de todas las columnas en 3 campos:

        classification:
            El nivel de clasificación más restrictivo encontrado entre todas
            las columnas. Usa la jerarquía definida en _SENSITIVITY_HIERARCHY:
            "Highly sensitive" > "Confidential" > "Internal" > "Public"

            Ejemplo: si una tabla tiene 10 columnas "Internal" y 1 "Confidential",
            la tabla queda clasificada como "Confidential".

        has_sensitive_columns:
            True si al menos una columna tiene is_sensitive = True.

        sensitive_columns:
            Lista de nombres de columnas donde is_sensitive = True.
            Útil para que los data stewards identifiquen rápidamente
            qué columnas requieren controles de acceso.

    Mapeo desde el JSON aprobado (por cada columna en payload.columns):
        col.sensitivity.classification  → agrega para determinar el nivel más alto
        col.sensitivity.is_sensitive    → filtra las columnas sensibles
        col.name                        → si is_sensitive=True, se agrega a la lista
    """
    columns = payload.get("columns", [])

    # Recolectar las clasificaciones de todas las columnas
    classifications = [
        col.get("sensitivity", {}).get("classification", "Public")
        for col in columns
    ]

    # Determinar el nivel más restrictivo recorriendo la jerarquía de mayor a menor.
    # El primer nivel que aparezca en la lista de clasificaciones es el ganador.
    top_classification = "Public"
    for level in _SENSITIVITY_HIERARCHY:
        if level in classifications:
            top_classification = level
            break

    # Columnas que el LLM identificó como sensibles
    sensitive_columns = [
        col["name"]
        for col in columns
        if col.get("sensitivity", {}).get("is_sensitive", False)
    ]

    return {
        "aspectType": _aspect_type_resource(ASPECT_TYPE_SENSIBILITY),
        "data": {
            "classification":        top_classification,
            "has_sensitive_columns": len(sensitive_columns) > 0,
            "sensitive_columns":     sensitive_columns,
        },
    }


# =============================================================================
# SECCIÓN 5: FUNCIÓN PRINCIPAL — UPSERT EN DATAPLEX
# =============================================================================

def upsert_dataplex_aspects(payload: dict) -> DataplexWriteResult:
    """
    Publica los 3 Aspect Types en Dataplex para una tabla de BigQuery.

    Recibe el JSON aprobado (output de Gemini validado por metadata_schema.py),
    construye los 3 aspects y hace un único PATCH a la API de Dataplex.

    La operación es un UPSERT: si los aspects ya existen los sobreescribe,
    si no existen los crea. El parámetro updateMask="aspects" garantiza que
    solo se toquen los aspects declarados y no otros metadatos del Entry.

    Args:
        payload: JSON aprobado con la estructura validada por metadata_schema.py.
                 Debe contener 'table_fqn' con formato 'project.dataset.table'.

    Returns:
        DataplexWriteResult indicando éxito o fallo con detalle del error.

    Ejemplo de uso (desde main.py endpoint /approve, o desde job.py):
        from app.services.dataplex_writer import upsert_dataplex_aspects

        result = upsert_dataplex_aspects(payload)
        if not result.success:
            raise RuntimeError(f"Dataplex falló: {result.errors}")
    """

    # -------------------------------------------------------------------------
    # Paso 1: Extraer y validar el FQN desde el payload
    # -------------------------------------------------------------------------
    # El FQN viene del JSON generado por Gemini, tiene formato "project.dataset.table"
    # No se recibe como parámetro separado para mantener la función simple y
    # compatible con cualquier proyecto GCP (multi-proyecto).
    table_fqn = payload.get("table_fqn", "")

    try:
        bq_project, dataset, table = _parse_fqn(table_fqn)
    except ValueError as e:
        # Si el FQN está malformado, retornar error sin llamar a la API
        logger.error(f"[Dataplex] FQN inválido: {e}")
        return DataplexWriteResult(
            success=False,
            table_fqn=table_fqn,
            entry_name="",
            errors=[str(e)],
        )

    # -------------------------------------------------------------------------
    # Paso 2: Construir el Entry name del Entry en Dataplex
    # -------------------------------------------------------------------------
    # El Entry name identifica a la tabla dentro de Dataplex Catalog.
    # Dataplex auto-discovery ya creó este Entry; solo necesitamos su nombre
    # para saber a qué Entry adjuntar los aspects.
    entry_name = _build_entry_name(bq_project, dataset, table)
    logger.info(f"[Dataplex] Iniciando upsert para: {table_fqn}")
    logger.debug(f"[Dataplex] Entry name: {entry_name}")

    # -------------------------------------------------------------------------
    # Paso 3: Obtener credenciales de autenticación
    # -------------------------------------------------------------------------
    credentials = _get_credentials()
    headers = {
        "Authorization": f"Bearer {credentials.token}",
        "Content-Type":  "application/json",
    }

    # -------------------------------------------------------------------------
    # Paso 4: Construir el body con los 3 aspects
    # -------------------------------------------------------------------------
    # Cada key del dict 'aspects' es el resource name completo del Aspect Type.
    # La API requiere este formato completo, no solo el ID corto.
    aspects_body = {
        _aspect_type_resource(ASPECT_TYPE_TABLE):       _build_table_aspect(payload),
        _aspect_type_resource(ASPECT_TYPE_COLUMN):      _build_column_aspect(payload),
        _aspect_type_resource(ASPECT_TYPE_SENSIBILITY): _build_sensibility_aspect(payload),
    }

    # -------------------------------------------------------------------------
    # Paso 5: Llamar a la API de Dataplex con PATCH
    # -------------------------------------------------------------------------
    # Endpoint: PATCH https://dataplex.googleapis.com/v1/{entry_name}
    # updateMask=aspects → solo actualiza los aspects, no otros campos del Entry
    url    = f"{_DATAPLEX_API_BASE}/{entry_name}"
    params = {"updateMask": "aspects"}
    body   = {"aspects": aspects_body}

    try:
        response = requests.patch(
            url,
            headers=headers,
            params=params,
            json=body,
            timeout=30,  # segundos — suficiente para la API de Dataplex
        )
        # Lanza excepción automáticamente si el status code es 4xx o 5xx
        response.raise_for_status()

        aspects_updated = [
            ASPECT_TYPE_TABLE,
            ASPECT_TYPE_COLUMN,
            ASPECT_TYPE_SENSIBILITY,
        ]
        logger.info(
            f"[Dataplex] Upsert exitoso para {table_fqn}. "
            f"Aspects publicados: {aspects_updated}"
        )

        return DataplexWriteResult(
            success=True,
            table_fqn=table_fqn,
            entry_name=entry_name,
            aspects_updated=aspects_updated,
        )

    except requests.exceptions.HTTPError:
        # Error de la API de Dataplex (ej: 403 sin permisos, 404 Entry no existe)
        error_msg = (
            f"HTTP {response.status_code} al hacer upsert en {table_fqn}: "
            f"{response.text}"
        )
        logger.error(f"[Dataplex] {error_msg}")
        return DataplexWriteResult(
            success=False,
            table_fqn=table_fqn,
            entry_name=entry_name,
            errors=[error_msg],
        )

    except requests.exceptions.Timeout:
        # La API tardó más de 30 segundos en responder
        error_msg = f"Timeout al llamar Dataplex API para {table_fqn}"
        logger.error(f"[Dataplex] {error_msg}")
        return DataplexWriteResult(
            success=False,
            table_fqn=table_fqn,
            entry_name=entry_name,
            errors=[error_msg],
        )

    except requests.exceptions.RequestException as e:
        # Error de red genérico (sin conectividad, DNS, etc.)
        error_msg = f"Error de conexión para {table_fqn}: {str(e)}"
        logger.error(f"[Dataplex] {error_msg}")
        return DataplexWriteResult(
            success=False,
            table_fqn=table_fqn,
            entry_name=entry_name,
            errors=[error_msg],
        )