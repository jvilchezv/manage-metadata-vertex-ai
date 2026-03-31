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

    prompt = f"""
Eres un experto en gobierno de datos y catalogación empresarial.

Tu tarea es ANALIZAR la tabla proporcionada y generar metadatos de NEGOCIO.
Debes devolver únicamente un JSON VALIDO que siga EXACTAMENTE la estructura especificada.

==================================================
CONTEXTO DE LA TABLA
==================================================
FQN: {fq_table}
Descripción actual: {table_desc}

Columnas y ejemplos:
{chr(10).join(schema_lines)}

==================================================
INSTRUCCIONES ESTRICTAS
==================================================
1. NO inventes columnas. Usa solo las columnas listadas arriba.
2. NO agregues texto fuera del JSON final.
3. NO agregues comentarios dentro del JSON (no uses // ni #).
4. Usa SOLO comillas dobles.
5. Usa descripciones de negocio entre 30 y 60 palabras (evita tipos técnicos como STRING, INT64).
6. Usa los siguientes catálogos:
   - sensitivity.classification: "Highly sensitive", "Confidential", "Internal"

7. Reglas de sensibilidad:
   - is_sensitive = true si contiene: DNI, nombre, email, teléfono, dirección, coordenadas, datos personales, financieros o confidenciales.
   - si is_sensitive = true => classification = "Highly sensitive", "Confidential", "Internal"
    - si is_sensitive = false => classification = "Internal"
    
8. Reglas para is_computed:
   - true si el nombre sugiere cálculo: rate, pct, flag, total, avg, sum, count, ratio, amount_final.
   
9. Reglas de accuracy:
   - número entre 0.0 y 1.0
   - mayor si la descripción es clara por el contexto.

10. Si no puedes cumplir alguna regla:
   - table_description.description = ""
   - table_description.accuracy = 0.0
   - columns[*].description = ""
   - columns[*].accuracy = 0.0


==================================================
FORMATO EXACTO DEL JSON DE SALIDA
==================================================

{{
  "table_fqn": "{fq_table}",
  "table_description": {{
    "description": "texto en 30–60 palabras",
    "accuracy": 0.0
  }},
  "columns": [
    {{
      "name": "nombre_columna",
      "description": "texto en 30–60 palabras",
      "accuracy": 0.0,
      "is_computed": fa lse,
      "sensitivity": {{
        "is_sensitive": false,
        "classification": "Internal"
      }}
    }}
  ],
  "model": {{
    "name": "manage-metadata-gemini",
    "version": "{model._model_name.split("/")[-1]}"
  }},
  "generated_at": "{generated_at}"
}}

==================================================
TAREA
==================================================
Genera SOLO el JSON válido según las reglas.

"""
    return prompt
