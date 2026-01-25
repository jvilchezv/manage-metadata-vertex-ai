import json
import time
import vertexai
from vertexai.generative_models import GenerativeModel

model = "gemini-2.5-pro"
vertexai.init()

model = GenerativeModel(model_name=model)


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
