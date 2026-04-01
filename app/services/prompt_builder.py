DOMAIN_CONTEXT = """
==================================================
CONTEXTO DE DOMINIO (SOLO PARA DESAMBIGUAR NOMBRES)
==================================================
La tabla pertenece a Rímac Seguros (Perú).
Los datos pueden corresponder a: pólizas, siniestros, asegurados,
primas, coberturas, endosos, productos de salud, vida o vehicular.

Usa este contexto SOLO para interpretar nombres de columna ambiguos.
NO asumas procesos, sistemas, eventos, estados, ni usos de negocio
que no estén evidenciados explícitamente en el esquema, desc_bq o ejemplos.
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
    table_desc = (table.description or "").strip() or "Sin descripción previa"

    schema_lines = []
    for field in table.schema:
        col_profile = profile.get(field.name, {}) or {}

        examples = col_profile.get("example_values", []) or []
        null_ratio = col_profile.get("null_ratio", None)
        dist_ratio = col_profile.get("distinct_ratio", None)
        bq_description = (col_profile.get("bq_description", "") or "").strip()

        examples_str = ", ".join(map(str, examples[:3])) if examples else "sin ejemplos"
        null_str = f"{null_ratio:.0%} nulos" if null_ratio is not None else ""
        dist_str = f"{dist_ratio:.0%} distintos" if dist_ratio is not None else ""
        stats_str = " | ".join(filter(None, [null_str, dist_str]))
        desc_str = f' | desc_bq: "{bq_description}"' if bq_description else ""

        schema_lines.append(
            f"- {field.name} [{field.field_type}, {field.mode}]"
            f"{' | ' + stats_str if stats_str else ''}"
            f"{desc_str}"
            f" | ejemplos: {examples_str}"
        )

    prompt = f"""
Eres un experto en gobierno de datos y catalogación empresarial.

Tu tarea es ANALIZAR la tabla proporcionada y generar metadatos de NEGOCIO.
Debes devolver únicamente un JSON VALIDO que siga EXACTAMENTE la estructura especificada.

==================================================
CONTEXTO DE LA TABLA
==================================================
FQN            : {fq_table}
Descripción BQ : {table_desc}

Columnas (tipo, modo, estadísticas, desc_bq y ejemplos reales):
{chr(10).join(schema_lines)}

{DOMAIN_CONTEXT}

==================================================
ACLARACIONES DE ALCANCE (CRÍTICAS)
==================================================
- Las tablas pueden representar información transversal del negocio de salud,
  pero NO se debe asumir pertenencia exclusiva a un producto, póliza o régimen.
- Campos de tipo código o identificador representan valores opacos cuyo
  significado depende de tablas maestras externas NO provistas.
- NO infieras estados, vigencia, clasificación, condición ni significado
  desde valores constantes o letras individuales (por ejemplo: "S", "N", "V").
- Si una columna no tiene evidencia suficiente, debe quedar SIN descripción.

==================================================
REGLAS ESTRICTAS (NO NEGOCIABLES)
==================================================

1) EVIDENCIA ÚNICAMENTE
Describe SOLO lo que se demuestra de forma directa mediante:
- nombre de columna
- tipo y modo
- desc_bq (si existe)
- estadísticas (nulos/distintos)
- ejemplos reales
Si algo no está explícitamente evidenciado, NO lo menciones.

2) PROHIBIDO ABSOLUTAMENTE
a) Lenguaje especulativo o interpretativo:
   "podría", "parece", "probablemente", "posiblemente", "sugiere",
   "aparentemente", "indica que", "significa", "representa un estado".
b) Función, propósito o rol implícito (aunque no use verbos de uso):
   "sirve como", "permite", "confirma su función", "para control",
   "para seguimiento", "para análisis", "metadato de auditoría".
c) Inferencias de negocio no evidenciadas:
   EPS, cliente activo/inactivo, broker, afiliación, cobertura, continuidad,
   segmentación comercial, estado contractual.
d) Frases vacías:
   "información relevante", "datos importantes", "registro completo".

3) TOQUE DE NEGOCIO SIN USO
Las descripciones deben responder únicamente:
¿QUÉ ES ESTE DATO?
NO responder:
¿PARA QUÉ SIRVE?, ¿QUÉ PERMITE?, ¿CÓMO SE USA?

4) AMBIGÜEDAD
Si describir el campo requiere interpretación, hipótesis o alternativas:
→ description = ""
→ accuracy = 0.0
Sin excepciones. No rellenes.

5) USO DE desc_bq
Si desc_bq está presente:
- úsala como base factual
- puedes reformular para cumplir longitud

6) NULOS ALTOS
Si null_ratio >= 0.50 (solo para campos), la descripción debe terminar EXACTAMENTE con:
"Utilizada principalmente para segmentos u agrupaciones específicas."

No expliques causas ni contexto.

7) ESTILO Y LONGITUD
- Tabla: 40–80 palabras (máx. 100).
- Columna: 20–50 palabras (máx. 80).
- Si NO alcanzas el mínimo con evidencia, deja description = "".
- Español neutro, factual, sin adjetivos valorativos.
- No incluir tipos técnicos (STRING, INT64, etc.).

8) COBERTURA
Genera exactamente UNA entrada por cada columna listada,
usando el mismo nombre y sin duplicados.

==================================================
REGLAS DE METADATOS
==================================================
sensitivity:
- true  si el contenido demuestra datos personales o sensibles
  (nombre, documento, contacto, dirección, datos financieros).
- false en cualquier otro caso.

is_computed:
- true  si el nombre indica valor derivado:
  rate, pct, flag, total, avg, sum, count, ratio, amount_final.
- false en cualquier otro caso.

accuracy — escala fija
- 1.0 : evidencia completa e inequívoca
- 0.8 : desc_bq clara, ejemplos limitados
- 0.7 : nombre claro + ejemplos suficientes
- 0.4 : inferencia parcial
- 0.0 : ambigüedad → description vacío

==================================================
FORMATO EXACTO (JSON ESTRICTO)
==================================================
Devuelve SOLO JSON válido.
Sin markdown, sin texto adicional, sin comentarios.

{{
  "table_fqn": "{fq_table}",
  "table_description": {{
    "description": "texto",
    "accuracy": 0.0
  }},
  "columns": [
    {{
      "name": "nombre_columna",
      "description": "texto",
      "accuracy": 0.0,
      "is_computed": false,
      "sensitivity": false
    }}
  ]
}}

==================================================
TAREA
==================================================
Devuelve ÚNICAMENTE el JSON. Nada más.
"""
    return prompt