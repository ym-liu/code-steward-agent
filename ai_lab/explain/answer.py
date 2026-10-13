"""The answer format (eight short fields) and the program's format and line-number checks."""
import json

SCHEMA_VERSION = "2"
TEXT_LIST = {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 180}, "maxItems": 3}
SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "purpose": {"type": ["string", "null"], "minLength": 1, "maxLength": 240},
        "inputs": TEXT_LIST, "outputs": TEXT_LIST,
        "usage": {**TEXT_LIST, "maxItems": 2},
        "dependencies": {**TEXT_LIST, "maxItems": 5},
        "relationships": TEXT_LIST,
        "unknowns": {**TEXT_LIST, "maxItems": 2},
        "evidence": {"type": "array", "maxItems": 3, "items": {
            "type": "object", "additionalProperties": False,
            "properties": {"line": {"type": "integer", "minimum": 1},
                           "claim": {"type": "string", "minLength": 1, "maxLength": 180}},
            "required": ["line", "claim"],
        }},
    },
    "required": ["purpose", "inputs", "outputs", "usage", "dependencies", "relationships", "unknowns", "evidence"],
}


def validate_analysis(raw, line_count):
    """Validate shape AND line bounds. Semantic correctness still needs a human."""
    try:
        def unique_keys(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError(f"Duplicate JSON key: {key}")
                result[key] = value
            return result
        value = json.loads(raw, object_pairs_hook=unique_keys,
                           parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
    except (ValueError, TypeError) as error:
        return None, False, False, [f"Invalid JSON: {error}"]
    errors = []
    if not isinstance(value, dict) or set(value) != set(SCHEMA["required"]):
        return value, True, False, ["Output must contain exactly the eight required fields."]
    if value["purpose"] is not None and (not isinstance(value["purpose"], str) or not value["purpose"].strip()):
        errors.append("purpose must be a non-empty string or null.")
    elif isinstance(value["purpose"], str) and len(value["purpose"]) > 240:
        errors.append("purpose is too long; give a short description.")
    for key in ("inputs", "outputs", "usage", "dependencies", "relationships", "unknowns"):
        items = value[key]
        limit = SCHEMA["properties"][key]["maxItems"]
        if (not isinstance(items, list) or len(items) > limit
                or not all(isinstance(s, str) and s.strip() and len(s) <= 180 for s in items)):
            errors.append(f"{key} must contain at most {limit} short non-empty strings.")
    evidence = value["evidence"]
    if not isinstance(evidence, list) or len(evidence) > 3:
        errors.append("evidence must be an array of at most 3 items.")
    else:
        for item in evidence:
            if not isinstance(item, dict) or set(item) != {"line", "claim"}:
                errors.append("Evidence items need exactly line and claim.")
            elif (type(item["line"]) is not int or item["line"] < 1
                  or not isinstance(item["claim"], str) or not item["claim"].strip() or len(item["claim"]) > 180):
                errors.append("Evidence line/claim has an invalid type or value.")
    schema_valid = not errors
    if schema_valid:
        if any(item["line"] > line_count for item in evidence):
            errors.append("Evidence cites a line outside the input file.")
        if value["purpose"] is not None and not evidence:
            errors.append("A stated purpose needs at least one evidence item.")
        if value["purpose"] is None and not value["unknowns"]:
            errors.append("Unknown purpose must explain what is missing in unknowns.")
    return value, True, schema_valid, errors
