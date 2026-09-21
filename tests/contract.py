"""Validates HTTP responses against specs/openapi.yaml, as AC-API-01 specifies."""

import json
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml
from jsonschema import Draft202012Validator, FormatChecker

SPEC_PATH = Path(__file__).resolve().parents[1] / "specs" / "openapi.yaml"
DOCUMENTATION_PATHS = {"/openapi.json", "/docs"}
METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}

RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")

format_checker = FormatChecker()


def is_date_time(value: object) -> bool:
    if not isinstance(value, str):
        return True
    return bool(RFC3339.match(value)) and _parses(value)


def _parses(value: str) -> bool:
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def is_uri(value: object) -> bool:
    if not isinstance(value, str):
        return True
    parsed = urlparse(value)
    return bool(parsed.scheme and parsed.netloc)


def is_int32(value: object) -> bool:
    return not isinstance(value, int) or -(2**31) <= value < 2**31


def is_int64(value: object) -> bool:
    return not isinstance(value, int) or -(2**63) <= value < 2**63


format_checker.checks("date-time")(is_date_time)
format_checker.checks("uri")(is_uri)
format_checker.checks("int32")(is_int32)
format_checker.checks("int64")(is_int64)


@dataclass(frozen=True)
class Operation:
    operation_id: str
    method: str
    template: str
    pattern: re.Pattern[str]
    responses: dict[str, Any]


@dataclass
class ContractChecker:
    """Checks each response it is given and remembers the (operation, status) pairs it saw."""

    document: dict[str, Any] = field(
        default_factory=lambda: yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))
    )
    observed: set[tuple[str, int]] = field(default_factory=set)
    violations: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self.operations: list[Operation] = []
        for template, item in self.document["paths"].items():
            regex = "^" + re.sub(r"\\\{[^/]+?\\\}", "[^/]+", re.escape(template)) + "$"
            for method, operation in item.items():
                if method in METHODS:
                    self.operations.append(
                        Operation(
                            operation_id=operation["operationId"],
                            method=method.upper(),
                            template=template,
                            pattern=re.compile(regex),
                            responses=operation["responses"],
                        )
                    )

    @property
    def declared(self) -> set[tuple[str, int]]:
        return {
            (operation.operation_id, int(status))
            for operation in self.operations
            for status in operation.responses
        }

    def validator(self, schema: dict[str, Any]) -> Draft202012Validator:
        root = {**schema, "components": self.document["components"]}
        return Draft202012Validator(root, format_checker=format_checker)

    def resolve(self, node: dict[str, Any]) -> dict[str, Any]:
        while "$ref" in node:
            target: Any = self.document
            for part in node["$ref"].removeprefix("#/").split("/"):
                target = target[part]
            node = target
        return node

    def check(
        self, method: str, path: str, status: int, headers: dict[str, str], body: bytes
    ) -> list[str]:
        """Return the contract violations of one response; an empty list means it conforms."""
        if path in DOCUMENTATION_PATHS:
            return []
        where = f"{method} {path} -> {status}"
        media_type = headers.get("content-type", "").split(";")[0].strip()
        matches = [operation for operation in self.operations if operation.pattern.match(path)]
        operation = next((match for match in matches if match.method == method), None)

        if operation is None:
            problems = []
            if media_type != "application/problem+json":
                problems.append(f"{where}: undeclared route answered with {media_type!r}")
            problems += self._validate_body(where, body, {"$ref": "#/components/schemas/Problem"})
            return self._record(problems)

        with self.lock:
            self.observed.add((operation.operation_id, status))
        declared = operation.responses.get(str(status))
        if declared is None:
            return self._record([f"{where}: status not declared for {operation.operation_id}"])
        response = self.resolve(declared)
        problems = []
        for name, header in response.get("headers", {}).items():
            value = headers.get(name.lower())
            if value is None:
                problems.append(f"{where}: missing header {name}")
                continue
            schema = self.resolve(header)["schema"]
            if schema.get("type") == "integer":
                if not value.isdigit():
                    problems.append(f"{where}: header {name}={value!r} is not an integer")
                    continue
                problems += self._validate(where, int(value), schema)
        content = response.get("content", {})
        if media_type not in content:
            problems.append(f"{where}: content type {media_type!r} not in {sorted(content)}")
        else:
            problems += self._validate_body(where, body, content[media_type]["schema"])
        return self._record(problems)

    def _validate_body(self, where: str, body: bytes, schema: dict[str, Any]) -> list[str]:
        try:
            instance = json.loads(body)
        except ValueError:
            return [f"{where}: body is not JSON: {body[:200]!r}"]
        return self._validate(where, instance, schema)

    def _validate(self, where: str, instance: object, schema: dict[str, Any]) -> list[str]:
        return [
            f"{where}: {'/'.join(map(str, error.absolute_path)) or '<root>'}: {error.message}"
            for error in self.validator(schema).iter_errors(instance)
        ]

    def _record(self, problems: list[str]) -> list[str]:
        if problems:
            with self.lock:
                self.violations.extend(problems)
        return problems
