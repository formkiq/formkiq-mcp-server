"""Offline OpenAPI catalog, argument validation, and parameter serialization."""

from __future__ import annotations

import copy
import re
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import yaml
from jsonschema import Draft4Validator, FormatChecker

from .operations import ALLOWED_OPERATION_IDS, READ_OPERATION_IDS, WRITE_OPERATION_IDS  # noqa: F401

BUNDLED_SPEC_URL = (
    "https://raw.githubusercontent.com/formkiq/formkiq-core/"
    "2d72a01c04f153e4ccadf72164bd248b270d774e/docs/openapi/openapi-jwt.yaml"
)


class OpenApiProxyError(ValueError):
    """An actionable configuration or input error safe to show to the caller."""


def load_openapi_text(local_file: str | None = None, url: str | None = None) -> str:
    if local_file and url:
        raise OpenApiProxyError("Set only one of FORMKIQ_OPENAPI_FILE and FORMKIQ_OPENAPI_URL")
    if local_file:
        return Path(local_file).read_text(encoding="utf-8")
    if url:
        if httpx.URL(url).scheme != "https":
            raise OpenApiProxyError("FORMKIQ_OPENAPI_URL must use HTTPS")
        with httpx.Client(timeout=20, follow_redirects=False) as client, client.stream("GET", url) as response:
            response.raise_for_status()
            chunks = bytearray()
            for chunk in response.iter_bytes():
                chunks.extend(chunk)
                if len(chunks) > 5_000_000:
                    raise OpenApiProxyError("OpenAPI document exceeds 5 MB")
            return chunks.decode("utf-8")
    return files("formkiq_mcp.data").joinpath("openapi-jwt.yaml").read_text(encoding="utf-8")


def json_schema(value: Any) -> Any:
    """Translate OpenAPI 3.0 nullable schemas to JSON Schema draft 4."""
    if isinstance(value, list):
        return [json_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {
        key: json_schema(item) for key, item in value.items() if not (key == "nullable" and isinstance(item, bool))
    }
    if value.get("nullable") is True:
        return {"anyOf": [result, {"type": "null"}]}
    return result


class OpenApiCatalog:
    def __init__(self, text: str, allowed_ids: frozenset[str] = ALLOWED_OPERATION_IDS):
        self.text = text
        self.spec = yaml.safe_load(text)
        if not isinstance(self.spec, dict) or not isinstance(self.spec.get("paths"), dict):
            raise OpenApiProxyError("OpenAPI document must contain a paths object")
        if not str(self.spec.get("openapi", "")).startswith("3.0."):
            raise OpenApiProxyError("Only OpenAPI 3.0 documents are supported")
        self._check_refs(self.spec)
        self.schema_root = json_schema({"components": self.spec.get("components", {})})
        self.operations: dict[str, dict[str, Any]] = {}
        seen: set[str] = set()
        for path, raw_item in self.spec["paths"].items():
            item = self.resolve(raw_item)
            for method, raw_operation in item.items():
                if method not in {"get", "post", "put", "patch", "delete"}:
                    continue
                operation = self.resolve(raw_operation)
                operation_id = operation.get("operationId")
                if not operation_id:
                    continue
                if operation_id in seen:
                    raise OpenApiProxyError(f"Duplicate operationId: {operation_id}")
                seen.add(operation_id)
                if operation_id not in allowed_ids:
                    continue
                if not path.startswith("/") or path.startswith("//"):
                    raise OpenApiProxyError(f"Invalid operation path: {operation_id}")
                parameters = {}
                for parameter in [*item.get("parameters", []), *operation.get("parameters", [])]:
                    parameter = self.resolve(parameter)
                    name = parameter["name"]
                    location = parameter["in"]
                    key = (location, name.lower() if location == "header" else name)
                    parameters[key] = parameter
                self.operations[operation_id] = {
                    "operationId": operation_id,
                    "method": method.upper(),
                    "path": path,
                    "summary": operation.get("summary", ""),
                    "description": operation.get("description", ""),
                    "parameters": list(parameters.values()),
                    "requestBody": self.resolve(operation["requestBody"]) if "requestBody" in operation else None,
                    "requiredPermission": operation.get("x-formkiq-required-permission"),
                    "responseStatusCodes": [str(code) for code in operation.get("responses", {})],
                }
        missing = allowed_ids - self.operations.keys()
        if missing:
            raise OpenApiProxyError("OpenAPI document is missing allowlisted operations: " + ", ".join(sorted(missing)))

    def _check_refs(self, value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                self._check_refs(item)
        elif isinstance(value, dict):
            if "$ref" in value:
                self.reference(value["$ref"])
            for item in value.values():
                self._check_refs(item)

    def reference(self, ref: str) -> Any:
        if not isinstance(ref, str) or not ref.startswith("#/"):
            raise OpenApiProxyError("Only document-local OpenAPI references are supported")
        value = self.spec
        try:
            for part in ref[2:].split("/"):
                value = value[part.replace("~1", "/").replace("~0", "~")]
        except (KeyError, TypeError) as exc:
            raise OpenApiProxyError(f"Unresolved OpenAPI reference: {ref}") from exc
        return value

    def resolve(self, value: Any) -> dict[str, Any]:
        seen: set[str] = set()
        while isinstance(value, dict) and "$ref" in value:
            ref = value["$ref"]
            if ref in seen:
                raise OpenApiProxyError(f"Circular OpenAPI object reference: {ref}")
            seen.add(ref)
            value = self.reference(ref)
        if not isinstance(value, dict):
            raise OpenApiProxyError("Expected an OpenAPI object")
        return value

    def validate(self, schema: dict[str, Any], value: Any, label: str) -> None:
        validator = Draft4Validator(
            {**self.schema_root, **json_schema(schema)},
            format_checker=FormatChecker(),
        )
        error = next(validator.iter_errors(value), None)
        if error:
            # jsonschema's message can include entire input bodies or secrets.
            location = ".".join(map(str, error.absolute_path))
            raise OpenApiProxyError(
                f"Invalid {label}{'.' + location if location else ''}: violates {error.validator} constraint"
            )

    def describe(self, operation_id: str) -> dict[str, Any]:
        operation = copy.deepcopy(self.operations[operation_id])
        # Include only reachable definitions. Retaining local references supports recursive schemas.
        pending = [operation]
        references: dict[str, Any] = {}
        while pending:
            value = pending.pop()
            if isinstance(value, list):
                pending.extend(value)
            elif isinstance(value, dict):
                if "$ref" in value and value["$ref"] not in references:
                    ref = value["$ref"]
                    references[ref] = self.reference(ref)
                    pending.append(references[ref])
                pending.extend(item for key, item in value.items() if key != "$ref")
        components: dict[str, Any] = {}
        for ref, value in references.items():
            target = components
            parts = [part.replace("~1", "/").replace("~0", "~") for part in ref[2:].split("/")]
            for part in parts[:-1]:
                target = target.setdefault(part, {})
            target[parts[-1]] = value
        return {**operation, **components}

    def split_args(self, operation: dict[str, Any], args: dict[str, Any]) -> tuple[dict, dict, dict]:
        containers = {"path": "path", "query": "query", "headers": "header"}
        result: dict[str, dict[str, Any]] = {}
        for key, location in containers.items():
            value = args.get(key, {})
            if not isinstance(value, dict):
                raise OpenApiProxyError(f"args.{key} must be an object")
            result[location] = dict(value)
        known_names = {}
        for parameter in operation["parameters"]:
            known_names.setdefault(parameter["name"], []).append(parameter["in"])
        for name, value in args.items():
            if name in {*containers, "body"}:
                continue
            locations = known_names.get(name, [])
            if len(locations) != 1:
                raise OpenApiProxyError(f"Unknown or ambiguous argument: {name}; use nested arguments")
            target = result[locations[0]]
            if name in target and target[name] != value:
                raise OpenApiProxyError(f"Conflicting argument: {name}")
            target[name] = value
        for location, values in result.items():
            known = {
                p["name"].lower() if location == "header" else p["name"]: p
                for p in operation["parameters"]
                if p["in"] == location
            }
            normalized = {}
            for name, value in values.items():
                key = name.lower() if location == "header" else name
                if key not in known:
                    raise OpenApiProxyError(f"Unknown {location} parameter: {name}")
                if location == "header" and key in {
                    "authorization",
                    "host",
                    "cookie",
                    "content-type",
                    "content-length",
                }:
                    raise OpenApiProxyError(f"Header is managed by the server: {name}")
                if key in normalized:
                    raise OpenApiProxyError(f"Duplicate {location} parameter: {name}")
                if value is None and location != "path" and not known[key].get("required"):
                    continue
                self.validate(known[key].get("schema", {}), value, f"{location}.{name}")
                normalized[key] = value
            for key, parameter in known.items():
                if parameter.get("required") and key not in normalized:
                    raise OpenApiProxyError(f"Missing {location} parameter: {parameter['name']}")
            result[location] = normalized
        request_body = operation["requestBody"]
        if request_body and request_body.get("required") and "body" not in args:
            raise OpenApiProxyError("Missing required request body")
        if "body" in args:
            if not request_body:
                raise OpenApiProxyError("This operation does not accept a request body")
            media = request_body.get("content", {}).get("application/json")
            if media is None:
                raise OpenApiProxyError("Only application/json request bodies are supported")
            self.validate(media.get("schema", {}), args["body"], "body")
        return result["path"], result["query"], result["header"]


def scalar(value: Any) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return str(value)
    raise OpenApiProxyError("Parameter must be a string, number, or boolean")


def substitute_path_params(template: str, args: dict[str, Any]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in args:
            raise OpenApiProxyError(f"Missing path parameter: {name}")
        value = scalar(args[name])
        if value in {"", ".", ".."}:
            raise OpenApiProxyError(f"Invalid path parameter: {name}; empty and dot segments are forbidden")
        return quote(value, safe="")

    return re.sub(r"{([^}]+)}", replace, template)


def query_parameters(operation: dict[str, Any], args: dict[str, Any]) -> list[tuple[str, str]]:
    result = []
    parameters = {p["name"]: p for p in operation["parameters"] if p["in"] == "query"}
    for name, value in args.items():
        parameter = parameters[name]
        style = parameter.get("style", "form")
        explode = parameter.get("explode", style == "form")
        if isinstance(value, list):
            if style not in {"form", "spaceDelimited", "pipeDelimited"}:
                raise OpenApiProxyError(f"Unsupported query style: {style}")
            values = [scalar(item) for item in value]
            if style == "form" and explode:
                result.extend((name, item) for item in values)
            else:
                delimiter = {"form": ",", "spaceDelimited": " ", "pipeDelimited": "|"}[style]
                result.append((name, delimiter.join(values)))
        elif isinstance(value, dict):
            if style == "deepObject":
                result.extend((f"{name}[{key}]", scalar(item)) for key, item in value.items())
            elif style == "form" and explode:
                result.extend((key, scalar(item)) for key, item in value.items())
            elif style == "form":
                result.append((name, ",".join(part for key, item in value.items() for part in (key, scalar(item)))))
            else:
                raise OpenApiProxyError(f"Unsupported query style: {style}")
        else:
            result.append((name, scalar(value)))
    return result
