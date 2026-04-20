import json
import time
import logging
import itertools
from datetime import datetime, timezone
from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

MODEL_NAME = "gemini-2.5-flash"

REGIONS = [
    "us-central1",
    "us-east1",
    "us-west1",
]

CLIENTS = {region: genai.Client(vertexai=True, location=region) for region in REGIONS}

region_cycle = itertools.cycle(REGIONS)


def get_next_region():
    return next(region_cycle)


def generate_metadata(prompt: str, retries: int = 3) -> dict:
    last_error = None

    for attempt in range(retries + 1):
        region = get_next_region()
        client = CLIENTS[region]

        try:
            logger.info(f"[LLM] Attempt {attempt + 1} using region: {region}")

            response = client.models.generate_content(
                model=MODEL_NAME,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.4,
                    response_mime_type="application/json",
                    top_p=0.85,
                ),
            )

            raw_text = response.text.strip()
            data = json.loads(raw_text)

            data["model"] = {
                "name": "manage-metadata-gemini",
                "version": MODEL_NAME,
            }

            data["generated_at"] = datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )

            return data

        except Exception as e:
            last_error = str(e)

            is_rate_limit = (
                "429" in last_error
                or "ResourceExhausted" in last_error
                or "quota" in last_error.lower()
            )

            logger.warning(
                f"[LLM ERROR] region={region} attempt={attempt + 1} "
                f"type={'RATE_LIMIT' if is_rate_limit else 'OTHER'} "
                f"error={last_error}"
            )

            if is_rate_limit:
                wait = 2 * (attempt + 1)
            else:
                wait = 1

            time.sleep(wait)

    raise RuntimeError(f"LLM failed after {retries + 1} attempts: {last_error}")
