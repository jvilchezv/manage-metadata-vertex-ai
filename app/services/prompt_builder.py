def build_prompt(table, profile: dict) -> str:
    """
    Construye un prompt para que el modelo genere SOLO el JSON indicado por el contrato:
    table: bigquery.Table - Metadatos de la tabla
    profile: dict - Perfil de la tabla con ejemplos por columna
    Retorna: str - Prompt formateado
    """
    fq_table = f"{table.project}.{table.dataset_id}.{table.table_id}"

    schema_lines = []
    for field in table.schema:
        col_profile = profile.get(field.name, {})
        examples = col_profile.get("example_values", []) or []
        examples_str = ", ".join(map(str, examples[:3])) if examples else "sin ejemplos"

        schema_lines.append(f"- {field.name}: {examples_str}")

    table_desc = (table.description or "").strip() or "Sin descripción previa"

    prompt = f"""
Eres un experto en gobierno de datos y catalogación empresarial.

Se te proporciona información de una tabla de BigQuery (descripción actual, columnas y ejemplos).
Tu tarea es generar descripciones de NEGOCIO (no técnicas) claras y concisas.

========================================
CONTEXTO
========================================

Tabla:
- FQN: {fq_table}
- Descripción actual: {table_desc}

Columnas y ejemplos:
{chr(10).join(schema_lines)}

========================================
SALIDA OBLIGATORIA
========================================

Devuelve **EXCLUSIVAMENTE** un JSON válido y parseable (sin texto adicional, sin comentarios, sin Markdown)
con la **estructura EXACTA** siguiente:


{{
  "table_fqn": "{fq_table}",
  "table_description": {{
    "description": "Texto entre 300 y 700 caracteres",
    "accuracy": 0.0
  }},
  "columns": [
    {{
      "name": "columna",
      "description": "Texto entre 300 y 700 caracteres",
      "accuracy": 0.0,
      "is_confidencial": false
    }}
  ],
  "model": {{
    "name": "perfilador-ml-gemini",
    "version": "1.0.0"
  }},
  "generated_at": "YYYY-MM-DDThh:mm:ssZ"
}}

========================================
REGLAS ESTRICTAS
========================================

- Devuelve SOLO el JSON (nada antes, nada después).
- Usa ÚNICAMENTE las columnas listadas en el contexto (no inventes columnas).
- Genera exactamente un objeto por columna.
- "accuracy" debe ser un número entre 0.0 y 1.0.
- "is_confidencial" debe ser true si la columna contiene:
  - Identificadores personales (nombre, email, teléfono, documento, dirección)
  - Información sensible de negocio
  En caso contrario, false.
- Redacta en lenguaje de negocio (evita repetir tipos técnicos como STRING, INT64).
- Limita todas las descripciones a máximo 1000 caracteres.
- Si no puedes cumplir alguna regla, devuelve el JSON con:
  - table_description.description = ""
  - accuracy = 0.0
  - columnas con accuracy = 0.0

========================================
TAREA
========================================
Con la información proporcionada, genera el JSON solicitado cumpliendo estrictamente el contrato.


"""
    return prompt
