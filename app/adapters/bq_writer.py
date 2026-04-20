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
        field_dict = _strip_policy_tags(field.to_api_repr())

        if field.field_type != "RECORD" and field.name in col_descriptions:
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


def _strip_policy_tags(field_dict: dict) -> dict:
    """
    Elimina policyTags recursivamente en todos los niveles del schema.
    Cubre campos simples, RECORD y subfields anidados a cualquier profundidad.
    """
    field_dict.pop("policyTags", None)

    if "fields" in field_dict:
        field_dict["fields"] = [
            _strip_policy_tags(subfield) for subfield in field_dict["fields"]
        ]

    return field_dict
