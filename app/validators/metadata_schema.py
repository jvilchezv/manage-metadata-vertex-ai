from jsonschema import Draft7Validator

METADATA_SCHEMA = {
    "type": "object",
    "required": ["table_fqn", "table_description", "columns", "model", "generated_at"],
    "properties": {
        "table_fqn": {"type": "string"},
        "table_description": {
            "type": "object",
            "required": ["description", "accuracy", "glossary_terms"],
            "properties": {
                "description": {"type": "string", "minLength": 0, "maxLength": 2000},
                "accuracy": {"type": "number", "minimum": 0, "maximum": 1},
                "glossary_terms": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 0},
                    "uniqueItems": True,
                },
                "sensitivity": {
                    "type": "object",
                    "required": ["is_sensitive", "classification"],
                    "properties": {
                        "is_sensitive": {"type": "boolean"},
                        "classification": {
                            "type": "string",
                            "enum": [
                                "Highly sensitive",
                                "Confidential",
                                "Internal",
                                "Public",
                            ],
                        },
                        "rationale": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            },
            "additionalProperties": False,
        },
        "columns": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "name",
                    "description",
                    "accuracy",
                    "is_computed",
                    "sensitivity",
                    "glossary_terms",
                ],
                "properties": {
                    "name": {"type": "string", "minLength": 1},
                    "description": {
                        "type": "string",
                        "minLength": 0,
                        "maxLength": 2000,
                    },
                    "accuracy": {"type": "number", "minimum": 0, "maximum": 1},
                    "is_computed": {"type": "boolean"},
                    "sensitivity": {
                        "type": "object",
                        "required": ["is_sensitive", "classification"],
                        "properties": {
                            "is_sensitive": {"type": "boolean"},
                            "classification": {
                                "type": "string",
                                "enum": [
                                    "Highly sensitive",
                                    "Confidential",
                                    "Internal",
                                    "Public",
                                ],
                            },
                            "rationale": {"type": "string"},
                        },
                        "additionalProperties": False,
                    },
                    "glossary_terms": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 0},
                        "uniqueItems": True,
                    },
                },
                "additionalProperties": False,
            },
        },
        "model": {
            "type": "object",
            "required": ["name", "version"],
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "version": {"type": "string", "minLength": 1},
            },
            "additionalProperties": False,
        },
        "generated_at": {"type": "string"},
    },
    "additionalProperties": False,
}

validator = Draft7Validator(METADATA_SCHEMA)


def validate_metadata(payload: dict) -> list[str]:
    return [e.message for e in validator.iter_errors(payload)]
