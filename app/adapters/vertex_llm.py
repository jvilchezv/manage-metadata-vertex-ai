import json
import time
import logging
from google import genai
from google.genai import types
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

MODEL_NAME = "gemini-1.5-flash"

client = genai.Client(vertexai=True)


def generate_metadata(prompt: str, retries: int = 2) -> dict:
    last_error = None

    for attempt in range(retries + 1):
        try:
            response = client.models.generate_content(
                model=MODEL_NAME,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.4,
                    response_mime_type="application/json",
                ),
            )

            raw_text = response.text.strip()

            data = json.loads(raw_text)

            data["model"] = {"name": "manage-metadata-gemini", "version": MODEL_NAME}
            data["generated_at"] = datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            print(data["generated_at"])
            logger.debug(f"LLM raw response: {data}")

            return data

        except Exception as e:
            last_error = str(e)
            logger.warning(
                f"LLM attempt {attempt + 1}/{retries + 1} failed: {last_error}"
            )
            time.sleep(1)

    raise RuntimeError(f"LLM failed after {retries + 1} attempts: {last_error}")
