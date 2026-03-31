import json
import time
import logging
from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

MODEL_NAME = "gemini-2.5-pro"

client = genai.Client(vertexai=True)


def generate_metadata(prompt: str, retries: int = 2) -> dict:
    last_error = None

    for attempt in range(retries + 1):
        try:
            response = client.models.generate_content(
                model=MODEL_NAME,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.4,        # bajo para respuestas deterministas
                    response_mime_type="application/json",
                ),
            )

            text = response.text.strip()
            logger.debug(f"LLM raw response: {text}")

            return json.loads(text)

        except Exception as e:
            last_error = str(e)
            logger.warning(f"LLM attempt {attempt + 1}/{retries + 1} failed: {last_error}")
            time.sleep(1)

    raise RuntimeError(f"LLM failed after {retries + 1} attempts: {last_error}")