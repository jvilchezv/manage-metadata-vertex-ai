from datetime import datetime, timezone
from app.adapters.vertex_llm import model

def build_prompt(table, profile: dict) -> str:
    """
    Construye un prompt para que el modelo genere SOLO el JSON indicado por el contrato.
    - table: bigquery.Table
    - profile: dict
    Retorna: str - Prompt formateado
    """
    fq_table = f"{table.project}.{table.dataset_id}.{table.table_id}"

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    schema_lines = []
    for field in table.schema:
        col_profile = profile.get(field.name, {}) or {}
        examples = col_profile.get("example_values", []) or []
        examples_str = ", ".join(map(str, examples[:3])) if examples else "sin ejemplos"
        schema_lines.append(f"- {field.name}: {examples_str}")

    table_desc = (table.description or "").strip() or "Sin descripción previa"

    # Nota: No usamos bloques ```json ni nada que el modelo imite
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
Devuelve EXCLUSIVAMENTE un JSON válido y parseable (sin texto adicional, sin comentarios, sin Markdown, sin bloques de código).
La estructura EXACTA debe ser:

{{
  "table_fqn": "{fq_table}",
  "table_description": {{
    "description": "Texto entre 300 y 700 caracteres",
    "accuracy": 0.0
  }},
  "columns": [
    {{
      "name": "nombre_de_columna_existente",
      "description": "Texto entre 300 y 700 caracteres",
      "accuracy": 0.0,
      "is_confidencial": false
    }}
  ],
  "model": {{
    "name": "manage-metadata-gemini",
    "version": {model.version}
  }},
  "generated_at": "{generated_at}"
}}

========================================
REGLAS ESTRICTAS
========================================
- Devuelve SOLO el JSON (nada antes, nada después).
- NO uses bloques Markdown (no uses ``` de ningún tipo).
- NO uses comillas simples. Usa comillas dobles en todo el JSON.
- NO agregues campos distintos a los definidos.
- Usa ÚNICAMENTE las columnas listadas en el contexto (no inventes columnas).
- Genera exactamente un objeto por cada columna del contexto, respetando el nombre exacto.
- "accuracy" debe ser un número entre 0.0 y 1.0.
- "is_confidencial" = true si la columna contiene identificadores personales (nombre, email, teléfono, documento, dirección) o información sensible de negocio; en caso contrario, false.
- Redacta descripciones en lenguaje de negocio (evita tipos técnicos como STRING, INT64).
- Limita todas las descripciones a máximo 1000 caracteres.
- Si no puedes cumplir alguna regla, devuelve el JSON con:
  - table_description.description = ""
  - accuracy = 0.0
  - todas las columnas con accuracy = 0.0

========================================
TAREA
========================================
Con la información proporcionada, genera el JSON solicitado cumpliendo estrictamente el contrato.
"""
    return prompt
