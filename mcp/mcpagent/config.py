"""
Module: config.py

Description:
    Loads the MCPAgent client and server configurations as JSON/JSONC/JSON5.
    Configs live in jsons; optional schemas live in jsons/schemas.
    A config uses the schema with the same filename stem: client.jsonc uses client.schema.
    Without a matching schema, the config loads without schema validation.
"""

from pathlib import Path
from typing import Any

import json5
from jsonschema import ValidationError, validate

# Configs and schemas share a location for both the client and server.
JSONS_DIR = Path(__file__).resolve().parent / "jsons"
SCHEMA_DIR = JSONS_DIR / "schemas"


def load_config(config_file: str | Path) -> dict[str, Any]:
    """
    Read a config and validate it against its matching schema when one exists.
    Args:
        config_file: Path to a JSON/JSONC/JSON5 configuration file.
            Custom configs also use the package's jsons/schemas directory.
    Returns:
        dict[str, Any]: The parsed configuration dictionary.
    Raises:
        RuntimeError: If the config is missing or is not an object, the matching schema
            cannot be loaded, or schema validation fails.
        ValueError: If the config cannot be parsed.
    """
    path = Path(config_file)
    if not path.is_file():
        raise RuntimeError(f"Configuration file not found: {path}")

    with path.open("r", encoding="utf-8") as config_stream:
        config_data = json5.load(config_stream)

    schema_path = SCHEMA_DIR / f"{path.stem}.schema"
    if schema_path.is_file():
        try:
            with schema_path.open("r", encoding="utf-8") as schema_stream:
                schema = json5.load(schema_stream)
            validate(instance=config_data, schema=schema)
        except ValidationError as error:
            raise RuntimeError(f"Schema validation failed for {path} against {schema_path}: {error.message}") from error
        except Exception as error:
            raise RuntimeError(f"Error loading schema {schema_path}: {error}") from error

    if not isinstance(config_data, dict):
        raise RuntimeError(f"Configuration must be a JSON object: {path}")
    return config_data
