from datetime import datetime, timezone
from app.adapters.vertex_llm import MODEL_NAME

DOMAIN_CONTEXT = """
==================================================
CONTEXTO DE DOMINIO
==================================================
La tabla pertenece a Rímac Seguros, compañía de seguros del Perú.
Los datos pueden corresponder a: pólizas, siniestros, asegurados,
primas, coberturas, endosos, productos de salud, vida o vehicular.

Usa este contexto SOLO para interpretar nombres de columna ambiguos.
NO asumas procesos, sistemas ni usos de negocio que no estén
evidenciados en los ejemplos o nombres de columna.
==================================================
"""


def build_prompt(table, profile: dict) -> str:
    """
    Construye un prompt para que el modelo genere SOLO el JSON indicado por el contrato.
    - table: bigquery.Table
    - profile: dict con bq_description, example_values, null_ratio, distinct_ratio
    Retorna: str - Prompt formateado
    """
    fq_table = f"{table.project}.{table.dataset_id}.{table.table_id}"
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    schema_lines = []
    for field in table.schema:
        col_profile    = profile.get(field.name, {}) or {}
        examples       = col_profile.get("example_values", []) or []
        null_ratio     = col_profile.get("null_ratio", None)
        dist_ratio     = col_profile.get("distinct_ratio", None)
        bq_description = col_profile.get("bq_description", "").strip()

        examples_str = ", ".join(map(str, examples[:3])) if examples else "sin ejemplos"
        null_str     = f"{null_ratio:.0%} nulos"     if null_ratio is not None else ""
        dist_str     = f"{dist_ratio:.0%} distintos" if dist_ratio is not None else ""
        stats_str    = " | ".join(filter(None, [null_str, dist_str]))
        desc_str     = f' | desc_bq: "{bq_description}"' if bq_description else ""

        schema_lines.append(
            f"- {field.name} [{field.field_type}, {field.mode}]"
            f"{' | ' + stats_str if stats_str else ''}"
            f"{desc_str}"
            f" | ejemplos: {examples_str}"
        )

    table_desc = (table.description or "").strip() or "Sin descripción previa"

    prompt = f"""
Eres un catalogador de datos. Tu única fuente de información es el esquema
y el perfilamiento estadístico que se te entrega a continuación.

==================================================
CONTEXTO DE LA TABLA
==================================================
FQN            : {fq_table}
Descripción BQ : {table_desc}

Columnas (tipo, modo, estadísticas, ejemplos de valores reales):
{chr(10).join(schema_lines)}

{DOMAIN_CONTEXT}

==================================================
REGLAS DE DESCRIPCIÓN — LEE CON ATENCIÓN
==================================================
A. Describe SOLO lo que los nombres de columna, tipos y ejemplos demuestran
   de forma directa. Si el dato no está en el perfilamiento, no lo menciones.

B. PROHIBIDO usar sin excepción:
   - Lenguaje especulativo: "podría", "parece", "probablemente", "sugiere",
     "posiblemente", "se podría usar", "indica que", "aparentemente".
   - Afirmaciones de uso o propósito del sistema: "esta tabla se usa en",
     "permite analizar", "es utilizada por", "se emplea para".
   - Frases vacías: "almacena información relevante", "contiene datos importantes".

C. Las descripciones deben ser afirmaciones factuales directas sobre
   QUÉ CONTIENE la columna o tabla. No describas para QUÉ SIRVE.
      "Registra el identificador único de la póliza en formato alfanumérico."
      "Esta columna podría usarse para rastrear pólizas en sistemas analíticos."

D. Columnas con null_ratio >= 0.50:
   - La descripción DEBE terminar con la frase:
     "Campo con alta tasa de nulos; su población aplica a casos específicos."
   - Esto es una observación factual del perfilamiento, no una suposición.

E. Si los ejemplos o el nombre no son suficientes para describir la columna
   con certeza → description = "" y accuracy = 0.0. No inventes, no supongas.

F. Si una columna tiene desc_bq, úsala como base factual para redactar la
   description. Puedes reformularla para cumplir el límite de palabras,
   pero no la contradigas ni la extiendas con suposiciones.
   Si desc_bq está ausente, describe solo desde el nombre y los ejemplos.

G. Longitud: entre 40 y 60 palabras por descripción (límite 1000 caracteres). 
   Sin tipos técnicos (STRING, INT64, FLOAT, etc.).

==================================================
REGLAS DE METADATOS
==================================================
sensitivity:
  - true  si la columna contiene: nombre, email, teléfono, dirección,
    coordenadas, DNI/documento de identidad, datos financieros o bancarios.
  - false en cualquier otro caso.

is_computed:
  - true si el nombre contiene alguno de estos términos: rate, pct, flag,
    total, avg, sum, count, ratio, amount_final, o cualquier otro que denote
    un valor derivado de un cálculo.
  - false en cualquier otro caso.

accuracy — escala fija, elige el valor más cercano:
  - 1.0 : nombre + desc_bq + ejemplos describen el contenido inequívocamente.
  - 0.8 : desc_bq presente pero ejemplos escasos o ambiguos.
  - 0.7 : sin desc_bq, nombre claro y ejemplos suficientes.
  - 0.4 : sin desc_bq, solo se puede inferir parcialmente del nombre.
  - 0.0 : sin certeza suficiente → description debe ser "".

==================================================
FORMATO EXACTO DEL JSON DE SALIDA
==================================================
{{
  "table_fqn": "{fq_table}",
  "table_description": {{
    "description": "texto entre 20 y 50 palabras",
    "accuracy": 0.0
  }},
  "columns": [
    {{
      "name": "nombre_columna",
      "description": "texto entre 20 y 50 palabras",
      "accuracy": 0.0,
      "is_computed": false,
      "sensitivity": false
    }}
  ],
  "model": {{
    "name": "manage-metadata-gemini",
    "version": "{MODEL_NAME}"
  }},
  "generated_at": "{generated_at}"
}}

==================================================
TAREA
==================================================
Devuelve ÚNICAMENTE el JSON. Sin explicaciones, sin markdown, sin comentarios.
"""
    return prompt