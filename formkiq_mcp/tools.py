"""Endpoint tools with OpenAPI-derived schemas and explicit argument routing."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any

from mcp.types import Tool, ToolAnnotations

from .openapi import OpenApiCatalog, OpenApiProxyError, json_schema
from .operations import OPERATION_POLICIES


def tool_name(operation_id: str) -> str:
    # Keep the established document search name; other names follow operationId.
    if operation_id == "DocumentSearch":
        return "search_documents"
    name = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", operation_id)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name).lower()


def local_schema(catalog: OpenApiCatalog, schema: dict[str, Any]) -> dict[str, Any]:
    """Include reachable definitions, retaining recursive references without expansion."""
    definitions: dict[str, Any] = {}

    def convert(value):
        if isinstance(value, list):
            return [convert(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {}
        for key, item in value.items():
            if key == "$ref":
                name = item[2:]
                if name not in definitions:
                    definitions[name] = {}  # Mark visited before following recursive references.
                    definitions[name] = convert(json_schema(catalog.reference(item)))
                result[key] = "#/$defs/" + name.replace("~", "~0").replace("/", "~1")
            else:
                result[key] = convert(item)
        return result

    result = convert(json_schema(schema))
    if definitions:
        result["$defs"] = definitions
    return result


@dataclass(frozen=True)
class OperationTool:
    operation_id: str
    definition: Tool
    parameters: dict[str, tuple[str, str]]

    def request_args(self, catalog: OpenApiCatalog, arguments: dict[str, Any]) -> dict[str, Any]:
        catalog.validate(self.definition.inputSchema, arguments, "tool arguments")
        result: dict[str, Any] = {"path": {}, "query": {}, "headers": {}}
        for key, value in arguments.items():
            if key == "body":
                result["body"] = value
            else:
                container, name = self.parameters[key]
                result[container][name] = value
        return result


def operation_tool(catalog: OpenApiCatalog, operation_id: str, annotations: ToolAnnotations) -> OperationTool:
    operation = catalog.operations[operation_id]
    policy = OPERATION_POLICIES[operation_id]
    parameters = operation["parameters"]
    properties: dict[str, Any] = {}
    required = []
    routing = {}
    for parameter in parameters:
        name, location = parameter["name"], parameter["in"]
        if location not in {"path", "query", "header"}:
            raise OpenApiProxyError(f"Unsupported parameter location for {operation_id}: {location}")
        if location == "header" and name.lower() in {
            "authorization",
            "host",
            "cookie",
            "content-type",
            "content-length",
        }:
            raise OpenApiProxyError(f"Managed header cannot be exposed by {operation_id}: {name}")
        # A folder query named 'path' can be a normal tool field. Ambiguous names
        # (including an API parameter named 'body') receive a location prefix.
        field = name
        if name == "body" or sum(p["name"] == name for p in parameters) > 1:
            field = f"{location}_{name}"
        if field in properties:
            raise OpenApiProxyError(f"Conflicting tool parameter for {operation_id}: {field}")
        schema = copy.deepcopy(parameter.get("schema", {}))
        if parameter.get("description"):
            schema["description"] = parameter["description"]
        if "example" in parameter:
            schema["examples"] = [parameter["example"]]
        properties[field] = schema
        routing[field] = ({"header": "headers"}.get(location, location), name)
        if location == "path" or parameter.get("required"):
            required.append(field)

    request_body = operation["requestBody"]
    if request_body:
        media = request_body.get("content", {}).get("application/json")
        if media is None:
            raise OpenApiProxyError(f"Only application/json request bodies are supported: {operation_id}")
        properties["body"] = copy.deepcopy(media.get("schema", {}))
        if request_body.get("description"):
            properties["body"]["description"] = request_body["description"]
        if request_body.get("required"):
            required.append("body")

    # The API spec makes deletion mode optional; our policy requires an explicit choice.
    if operation_id == "DeleteDocument" and "deleteType" not in required:
        required.append("deleteType")

    schema = local_schema(
        catalog,
        {"type": "object", "properties": properties, "required": required, "additionalProperties": False},
    )
    description = "\n\n".join(filter(None, (operation["summary"], operation["description"])))
    description += f"\n\nFormKiQ operation: {operation_id}. API group: {policy.api_group}. Access: {policy.access}."
    if operation_id == "DocumentSearch":
        description += " Continue pagination while next is present, including after empty pages. COUNT can be truncated; count_documents reports exactness."
    if operation_id == "DeleteDocument":
        description += " Requires explicit deleteType: SOFT_DELETE, HARD_DELETE, or CONTENT_ONLY. Historical purge uses purge_document."
    if operation_id == "PurgeDocument":
        description += " Permanently removes the document and its versions."
    return OperationTool(
        operation_id,
        Tool(
            name=tool_name(operation_id), description=description.strip(), inputSchema=schema, annotations=annotations
        ),
        routing,
    )
