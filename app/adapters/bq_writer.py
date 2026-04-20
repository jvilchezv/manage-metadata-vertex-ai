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
    col_descriptions: dict[str, str] = {
        col["name"]: col["description"] for col in metadata.get("columns", [])
    }

    new_schema = []
    schema_changed = False

    for field in table.schema:
        if field.field_type == "RECORD":
            new_schema.append(field)
            continue

        field_dict = field.to_api_repr()

        # Eliminar policyTags del payload — BigQuery los preserva en backend
        # sin necesidad de incluirlos en el update.
        field_dict.pop("policyTags", None)

        if field.name in col_descriptions:
            new_desc = col_descriptions[field.name]
            if (field.description or "") != new_desc:
                field_dict["description"] = new_desc
                schema_changed = True

        new_schema.append(bigquery.SchemaField.from_api_repr(field_dict))

    table_desc_changed = (table.description or "") != new_table_description

    if not schema_changed and not table_desc_changed:
        logger.info(f"Sin cambios detectados para: {table_fqn}")
        return

    table.schema = new_schema
    table.description = new_table_description
    client.update_table(table, ["schema", "description"])
    logger.info(f"Descripciones actualizadas para: {table_fqn}")
