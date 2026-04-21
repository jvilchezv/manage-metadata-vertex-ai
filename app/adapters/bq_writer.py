from __future__ import annotations

from google.cloud import bigquery
import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

MIN_ACCURACY = 0.7


def _clone_field_preserving_all(
    field: bigquery.SchemaField, *, description: str
) -> bigquery.SchemaField:
    d = field.to_api_repr()
    d["description"] = description
    return bigquery.SchemaField.from_api_repr(d)


def update_table_schema(metadata: Dict[str, Any], client: bigquery.Client) -> None:
    table_fqn = (metadata.get("table_fqn") or "").strip()
    if not table_fqn:
        raise ValueError(
            "metadata['table_fqn'] es requerido (ej: 'proyecto.dataset.tabla')."
        )

    logger.info("Actualizando descripciones para: %s", table_fqn)

    table = client.get_table(table_fqn)

    new_table_description = (
        (metadata.get("table_description") or {}).get("description") or ""
    ).strip()
    current_table_description = (table.description or "").strip()
    table_desc_changed = current_table_description != new_table_description
    print(table_desc_changed)

    if table_desc_changed:
        logger.info(
            "Tabla: description cambia (len %s -> %s)",
            len(current_table_description),
            len(new_table_description),
        )
        table.description = new_table_description

    columns = metadata.get("columns") or []
    if not isinstance(columns, list):
        raise ValueError(
            "metadata['columns'] debe ser una lista de objetos {name, description}."
        )

    col_descriptions: Dict[str, str] = {}
    for col in columns:
        name = (col.get("name") or "").strip()
        desc = (col.get("description") or "").strip()
        accuracy = col.get("accuracy", 0.0)

        if not name:
            continue

        if accuracy >= MIN_ACCURACY:
            col_descriptions[name] = desc
        else:
            logger.warning(
                "Columna '%s' omitida por baja accuracy (%.2f < %.2f)",
                name,
                accuracy,
                MIN_ACCURACY,
            )

    new_schema: List[bigquery.SchemaField] = []
    schema_changed = False

    for field in table.schema:
        if field.field_type != "RECORD" and field.name in col_descriptions:
            new_desc = col_descriptions[field.name]
            old_desc = (field.description or "").strip()

            if old_desc != new_desc:
                logger.info("Columna '%s': description cambia", field.name)
                field = _clone_field_preserving_all(field, description=new_desc)
                schema_changed = True

        new_schema.append(field)

    if not schema_changed and not table_desc_changed:
        logger.info("Sin cambios detectados para: %s", table_fqn)
        return

    update_fields: List[str] = []
    if schema_changed:
        table.schema = new_schema
        update_fields.append("schema")
    if table_desc_changed:
        update_fields.append("description")

    client.update_table(table, update_fields)
    logger.info("Descripciones actualizadas correctamente para: %s", table_fqn)
