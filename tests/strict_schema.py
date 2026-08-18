"""STRUCTURAL strict-schema validation for the transported structured-output schemas.

Forge hands every phase's output schema to `codex exec --output-schema`, and the CLI registers it
as the provider's `response_format` JSON schema. The provider validates that schema in strict mode
before any model answer exists; a schema that violates strict mode fails the whole request with
`invalid_json_schema`, which is a transport failure, never a review. The 966cb29 defect was exactly
that: `review-result.schema.json` declared `binding_requirements` in `properties` without listing
it in `required`, so every review request was refused with
"'required' is required to be supplied and to be an array including every key in properties."

Scope — what `structural_strict_schema_violations` actually verifies, and nothing more:

- the root schema describes exactly an object;
- every object schema sets `additionalProperties: false`;
- every object schema supplies `required` as an array naming every key in `properties` and
  nothing that is not in `properties` (the required/property closure that 966cb29 broke);
- the object/array shape is walked recursively through `properties`, `items`, `anyOf` and
  `$defs`, so the closure rule holds at every nesting depth.

It deliberately does NOT verify which schema keywords the provider's strict mode supports. The
repo holds no authoritative, complete allowlist of supported keywords, and a partial denylist
would be a claim of completeness the code cannot honor — so no keyword-support claim is made at
all. A schema using a keyword the provider refuses fails live as a transport failure, which the
controller classifies and bounds safely (`test_review_transport.py` Part 2); that is the honest
boundary. It also does NOT model the provider's numeric limits — nesting depth, total property
count, enum value count, or total name/enum string length. Those are pinned separately and
statically: `schema_metrics` measures the current schema documents and the limit tests in
`test_review_transport.py` assert the measurements fit the documented budgets. Passing all of it
is a static fact about the schema documents, never a substitute for live-provider certification.

`instance_violations` is a deliberately small instance checker covering exactly the keyword subset
the transported schemas use (`type` incl. null unions, `enum`, `properties`/`required`/
`additionalProperties`, `items`), so tests can prove concrete results conform or are refused
without adding a schema-library dependency.
"""

from __future__ import annotations



def structural_strict_schema_violations(schema: object, path: str = "$") -> list[str]:
    """Every way `schema` breaks the STRUCTURAL strict rules above, as human-readable strings."""
    violations: list[str] = []
    if path == "$" and not (isinstance(schema, dict) and _declared_types(schema) == ["object"]):
        violations.append(f"{path}: the root schema must describe exactly an object")
    _walk(schema, path, violations)
    return violations


def _declared_types(schema: dict) -> list[str]:
    declared = schema.get("type")
    if isinstance(declared, list):
        return [item for item in declared if isinstance(item, str)]
    return [declared] if isinstance(declared, str) else []


def _walk(schema: object, path: str, violations: list[str]) -> None:
    if not isinstance(schema, dict):
        violations.append(f"{path}: a subschema must be an object, got {type(schema).__name__}")
        return
    if "object" in _declared_types(schema) or "properties" in schema:
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            violations.append(f"{path}: an object schema must declare 'properties' as an object")
            properties = {}
        if schema.get("additionalProperties") is not False:
            violations.append(f"{path}: an object schema must set 'additionalProperties': false")
        required = schema.get("required")
        if not isinstance(required, list):
            violations.append(
                f"{path}: 'required' is required to be supplied and to be an array "
                f"including every key in properties"
            )
        else:
            for key in properties:
                if key not in required:
                    violations.append(f"{path}: property '{key}' is missing from 'required'")
            for key in required:
                if key not in properties:
                    violations.append(f"{path}: 'required' names '{key}', which is not in 'properties'")
        for key, subschema in properties.items():
            _walk(subschema, f"{path}.properties.{key}", violations)
    if "items" in schema:
        _walk(schema["items"], f"{path}.items", violations)
    members = schema.get("anyOf")
    if members is not None:
        if isinstance(members, list):
            for index, member in enumerate(members):
                _walk(member, f"{path}.anyOf[{index}]", violations)
        else:
            violations.append(f"{path}: 'anyOf' must be an array")
    definitions = schema.get("$defs")
    if isinstance(definitions, dict):
        for name, member in definitions.items():
            _walk(member, f"{path}.$defs.{name}", violations)


def schema_metrics(schema: dict) -> dict[str, int]:
    """Measured size facts about a schema document, for static comparison against provider limits.

    - `max_nesting_depth`: schema nesting with the root as depth 1, counting both object property
      and array item levels — a deliberate overcount of the provider's object-nesting measure, so
      a passing budget check is conservative;
    - `total_properties`: object properties across every object schema;
    - `total_enum_values`: enum members across every `enum` keyword;
    - `total_string_chars`: combined length of every property name, `$defs` name, and string enum
      value — the combined string budget the provider documents.
    """
    metrics = {
        "max_nesting_depth": 0,
        "total_properties": 0,
        "total_enum_values": 0,
        "total_string_chars": 0,
    }

    def walk(node: object, depth: int) -> None:
        if not isinstance(node, dict):
            return
        metrics["max_nesting_depth"] = max(metrics["max_nesting_depth"], depth)
        properties = node.get("properties")
        if isinstance(properties, dict):
            for name, subschema in properties.items():
                metrics["total_properties"] += 1
                metrics["total_string_chars"] += len(name)
                walk(subschema, depth + 1)
        enum = node.get("enum")
        if isinstance(enum, list):
            metrics["total_enum_values"] += len(enum)
            metrics["total_string_chars"] += sum(len(v) for v in enum if isinstance(v, str))
        if "items" in node:
            walk(node["items"], depth + 1)
        members = node.get("anyOf")
        if isinstance(members, list):
            for member in members:
                walk(member, depth)
        definitions = node.get("$defs")
        if isinstance(definitions, dict):
            for name, member in definitions.items():
                metrics["total_string_chars"] += len(name)
                walk(member, depth)

    walk(schema, 1)
    return metrics

def instance_violations(instance: object, schema: dict, path: str = "$") -> list[str]:
    """Every way `instance` fails `schema`, for the keyword subset the transported schemas use."""
    problems: list[str] = []
    allowed = _declared_types(schema)
    if allowed and not any(_is_type(instance, name) for name in allowed):
        problems.append(f"{path}: expected {allowed}, got {type(instance).__name__}")
        return problems
    if "enum" in schema and instance not in schema["enum"]:
        problems.append(f"{path}: {instance!r} is not one of {schema['enum']}")
    if isinstance(instance, dict):
        properties = schema.get("properties") or {}
        for key in schema.get("required") or []:
            if key not in instance:
                problems.append(f"{path}: required key '{key}' is absent")
        if schema.get("additionalProperties") is False:
            for key in instance:
                if key not in properties:
                    problems.append(f"{path}: key '{key}' is not declared by the schema")
        for key, value in instance.items():
            if key in properties:
                problems.extend(instance_violations(value, properties[key], f"{path}.{key}"))
    if isinstance(instance, list) and "items" in schema:
        for index, item in enumerate(instance):
            problems.extend(instance_violations(item, schema["items"], f"{path}[{index}]"))
    return problems


def _is_type(value: object, name: str) -> bool:
    if name == "object":
        return isinstance(value, dict)
    if name == "array":
        return isinstance(value, list)
    if name == "string":
        return isinstance(value, str)
    if name == "boolean":
        return isinstance(value, bool)
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if name == "null":
        return value is None
    return False
