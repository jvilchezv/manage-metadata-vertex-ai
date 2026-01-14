import json
import time
import vertexai
from vertexai.generative_models import GenerativeModel

vertexai.init()

model = GenerativeModel("gemini-2.5-pro")


def generate_metadata(prompt: str, retries: int = 2) -> dict:
    last_error = None

    for _ in range(retries + 1):
        try:
            response = model.generate_content(prompt)
            text = response.text.replace("```json", "").replace("```", "").strip()
            print(f"---Debug print: {text}")

            if not text.startswith("{") or not text.endswith("}"):
                raise ValueError("LLM did not return pure JSON")
            return json.loads(text)

        except Exception as e:
            last_error = str(e)
            time.sleep(1)

    raise RuntimeError(f"LLM failed after retries: {last_error}")

# app/adapters/vertex_llm.py
# import os, json, re
# from vertexai import init
# from vertexai.generative_models import GenerativeModel

# def _strict_json_from_text(raw: str) -> dict:
#     """
#     Intenta extraer el primer objeto JSON válido de la respuesta del LLM.
#     Evita fallar por texto "extra" antes/después.
#     """
#     match = re.search(r"\{.*\}", raw, re.DOTALL)
#     if not match:
#         raise RuntimeError("LLM did not return JSON")
#     try:
#         return json.loads(match.group(0))
#     except json.JSONDecodeError as e:
#         raise RuntimeError(f"LLM returned invalid JSON: {e}")

# def generate_metadata(prompt: str) -> dict:
#     project = os.getenv("GOOGLE_CLOUD_PROJECT")
#     location = os.getenv("GOOGLE_CLOUD_REGION", "us-east4")

#     init(project=project, location=location)

#     # Opción PRO (más fiel a instrucciones). Si priorizas costo/latencia, usa gemini-1.5-flash-001.
#     model = GenerativeModel(
#         "gemini-1.5-pro-001",
#         generation_config={
#             # Fuerza salida en JSON
#             "response_mime_type": "application/json",
#         },
#     )

#     resp = model.generate_content(prompt)

#     # Algunas versiones del SDK exponen .text, otras candidates. Mantenemos defensivo:
#     raw = getattr(resp, "text", None)
#     if not raw and hasattr(resp, "candidates") and resp.candidates:
#         # Extrae texto del primer candidato
#         parts = getattr(resp.candidates[0], "content", None) or getattr(resp.candidates[0], "output", None)
#         raw = getattr(parts, "parts", [None])[0].text if parts else None

#     if not raw:
#         raise RuntimeError("LLM returned empty response")

#     payload = _strict_json_from_text(raw)
#     return payload

