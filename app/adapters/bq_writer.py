from google.cloud import bigquery
import logging

logger = logging.getLogger(__name__)

client = bigquery.Client()


def update_table_schema(metadata: dict) -> None:
    """
    Aplica las descripciones generadas por el LLM al schema de la tabla en BigQuery.

    - metadata: dict validado con la estructura de TableMetadata
    """
    table_fqn = metadata["table_fqn"]
    logger.info(f"Updating schema for table: {table_fqn}")

    table = client.get_table(table_fqn)

    # Actualiza descripción de la tabla
    table.description = metadata["table_description"]["description"]

    # Construye un índice name -> descripción de columnas del LLM
    col_descriptions: dict[str, str] = {
        col["name"]: col["description"] for col in metadata.get("columns", [])
    }

    # Actualiza el schema campo por campo (preserva campos que el LLM no tocó)
    new_schema = []
    for field in table.schema:
        if field.name in col_descriptions:
            updated_field = bigquery.SchemaField(
                name=field.name,
                field_type=field.field_type,
                mode=field.mode,
                description=col_descriptions[field.name],
                fields=field.fields,  # preserva campos anidados (RECORD)
            )
            new_schema.append(updated_field)
        else:
            new_schema.append(field)

    table.schema = new_schema

    client.update_table(table, ["description", "schema"])
    logger.info(f"Schema updated successfully for: {table_fqn}")
