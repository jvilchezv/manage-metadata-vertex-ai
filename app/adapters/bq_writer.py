from google.cloud import bigquery
import logging

logger = logging.getLogger(__name__)


def update_table_schema(metadata: dict, client: bigquery.Client) -> None:
    """
    Aplica las descripciones generadas por el LLM al schema de la tabla en BigQuery.

    - metadata: dict validado con la estructura de TableMetadata
    """
    table_fqn = metadata["table_fqn"]
    logger.info(f"Actualizando descripciones para: {table_fqn}")

    table = client.get_table(table_fqn)

    new_table_description = metadata["table_description"]["description"]
    table_desc_changed = (table.description or "") != new_table_description
    table.description = new_table_description

    col_descriptions: dict[str, str] = {
        col["name"]: col["description"] for col in metadata.get("columns", [])
    }

    new_schema = []
    schema_changed = False

    for field in table.schema:
        if field.field_type != "RECORD" and field.name in col_descriptions:
            new_desc = col_descriptions[field.name]
            if (field.description or "") != new_desc:
                field = bigquery.SchemaField(
                    name=field.name,
                    field_type=field.field_type,
                    mode=field.mode,
                    description=new_desc,
                    fields=field.fields,
                )
                schema_changed = True

        new_schema.append(field)

    if not schema_changed and not table_desc_changed:
        logger.info(f"Sin cambios detectados para: {table_fqn}")
        return

    if schema_changed:
        table.schema = new_schema

    update_fields = []
    if schema_changed:
        update_fields.append("schema")
    if table_desc_changed:
        update_fields.append("description")

    client.update_table(table, update_fields)
    logger.info(f"Descripciones actualizadas correctamente para: {table_fqn}")
