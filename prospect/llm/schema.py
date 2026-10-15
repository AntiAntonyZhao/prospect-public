"""Pydantic model → strict tool `input_schema`.

Strict tool use needs every object closed (`additionalProperties: false`) with
all properties required; optional fields stay nullable via `anyOf [..., null]`.
$refs are inlined and validation-only keywords dropped — pydantic re-validates
the tool input anyway (`Model.model_validate`), so nothing is lost.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

_DROP = {
    "title",
    "default",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "minLength",
    "maxLength",
    "pattern",
    "format",
    "minItems",
    "maxItems",
    "examples",
}


def tool_input_schema(model: type[BaseModel]) -> dict[str, Any]:
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, list):
            return [resolve(x) for x in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            target = dict(defs[node["$ref"].rsplit("/", 1)[-1]])
            extra = {k: v for k, v in node.items() if k != "$ref"}
            return resolve({**target, **extra})
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key in _DROP:
                continue
            if key == "properties":  # property *names* are data, never keywords
                out[key] = {name: resolve(sub) for name, sub in value.items()}
            else:
                out[key] = resolve(value)
        if out.get("type") == "object" and "properties" in out:
            out["additionalProperties"] = False
            out["required"] = list(out["properties"])
        return out

    return resolve(schema)


def tool_definition(
    name: str, description: str, model: type[BaseModel], strict: bool = True
) -> dict[str, Any]:
    tool: dict[str, Any] = {
        "name": name,
        "description": description,
        "input_schema": tool_input_schema(model),
    }
    if strict:
        tool["strict"] = True
    return tool
