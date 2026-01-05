from jsonschema import Draft7Validator

METADATA_SCHEMA = {
    "type": "object",
    "required": ["table_fqn", "table_description", "columns", "model", "generated_at"],
    "properties": {
        "table_fqn": {"type": "string"},
        "table_description": {
            "type": "object",
            "required": ["description", "confidence"]
        },
        "columns": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["name", "description", "confidence", "is_confidencial"]
            }
        },
        "model": {
            "type": "object",
            "required": ["name", "version"]
        },
        "generated_at": {"type": "string"}
    }
}

validator = Draft7Validator(METADATA_SCHEMA)

def validate_metadata(payload: dict) -> list[str]:
    return [e.message for e in validator.iter_errors(payload)]
