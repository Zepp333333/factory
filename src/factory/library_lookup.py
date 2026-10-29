"""The Library finds Roles, prompts, schemas, skills and Pipelines by name across its Layers (ADR 0010).

A name is looked up from the top Layer down.
A prompt or schema that declares `extends` gets its base's content with its own added,
and the result keeps track of the Layer each part came from.
"""

import hashlib
import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from factory import front_matter

LAYOUT = {
    "pipelines": ".yaml",
    "roles": ".yaml",
    "prompts": ".md",
    "schemas": ".json",
    "skills": "",
}
SCHEMA_STRUCTURE = ("x-extends", "properties", "required")
JSON_TYPES = ("string", "integer", "number", "boolean", "array", "object")
READS_KEYS = ("required", "optional")


class LibraryError(Exception):
    pass


@dataclass(frozen=True)
class Layer:
    name: str
    root: Path


@dataclass(frozen=True)
class Found:
    layer: int
    path: Path


@dataclass(frozen=True)
class Prompt:
    """Prompt is the final text of a prompt and the Layers its parts came from, base first."""

    text: str
    parts: list[tuple[str, str]]


@dataclass(frozen=True)
class Schema:
    """Schema is the final schema, the Layer each property came from, and the Layers of `required`."""

    document: dict[str, Any]
    properties: dict[str, str]
    required: dict[str, str]
    parts: list[tuple[str, str]]


@dataclass(frozen=True)
class Read:
    """Read is an output field a Role expects from the Stages it reads, with the JSON type it expects."""

    type: str
    optional: bool


@dataclass(frozen=True)
class Role:
    name: str
    layer: str
    prompt: str
    schema: str
    skills: list[str]
    reads: dict[str, Read] = field(default_factory=dict)


class Library:
    """Library holds its Layers from the top one down and resolves items by name."""

    def __init__(
        self, layers: list[Layer], kit_skills: frozenset[str] = frozenset(), resolved: Path | None = None
    ) -> None:
        self.layers = layers
        self.kit_skills = kit_skills
        self.resolved = resolved
        self._pending: dict[Path, str] = {}

    def find(self, kind: str, name: str, start: int = 0) -> Found | None:
        for index in range(start, len(self.layers)):
            path = self.layers[index].root / kind / f"{name}{LAYOUT[kind]}"
            if path.is_dir() if kind == "skills" else path.is_file():
                return Found(index, path)
        return None

    def names(self, kind: str, layer: int) -> list[str]:
        directory = self.layers[layer].root / kind
        if not directory.is_dir():
            return []
        suffix = LAYOUT[kind]
        return sorted(
            p.name.removesuffix(suffix)
            for p in directory.iterdir()
            if p.name.endswith(suffix) and not p.name.startswith(".") and p.is_dir() == (kind == "skills")
        )

    def prompt(self, name: str, start: int = 0) -> Prompt:
        return self._prompt(name, start, [])

    def schema(self, name: str, start: int = 0) -> Schema:
        return self._schema(name, start, [])

    def role(self, name: str, start: int = 0) -> Role:
        found = self.find("roles", name, start)
        if found is None:
            raise LibraryError(f"role {name} is in no Layer")
        layer = self.layers[found.layer].name
        try:
            data = yaml.safe_load(found.path.read_text())
        except yaml.YAMLError as error:
            raise LibraryError(f"role {name} in {layer} is not valid YAML: {error}") from error
        skills = data.get("skills", []) if isinstance(data, dict) else None
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("prompt"), str)
            or not isinstance(data.get("schema"), str)
            or not isinstance(skills, list)
            or not all(isinstance(skill, str) for skill in skills)
        ):
            raise LibraryError(
                f"role {name} in {layer} must be a mapping with a `prompt` name, a `schema` name and a list of `skills`"
            )
        return Role(name, layer, data["prompt"], data["schema"], skills, self._reads(name, layer, data.get("reads")))

    def _reads(self, name: str, layer: str, value: Any) -> dict[str, Read]:
        """Reads the `reads` of a Role: `required` and `optional` mappings from an output field to its JSON type."""
        if value is None:
            return {}
        shape = f"role {name} in {layer}: `reads` must map `required` and `optional` to fields with a JSON type"
        if not isinstance(value, dict) or not set(value) <= set(READS_KEYS):
            raise LibraryError(shape)
        reads: dict[str, Read] = {}
        for key in READS_KEYS:
            fields = value.get(key) or {}
            if not isinstance(fields, dict):
                raise LibraryError(shape)
            for field_name, kind in fields.items():
                if kind not in JSON_TYPES:
                    types = ", ".join(JSON_TYPES)
                    raise LibraryError(
                        f"role {name} in {layer}: read {field_name} has type {kind!r}, not one of {types}"
                    )
                if field_name in reads:
                    raise LibraryError(f"role {name} in {layer}: read {field_name} is both required and optional")
                reads[str(field_name)] = Read(kind, key == "optional")
        return reads

    def final_role(self, name: str, start: int = 0) -> tuple[Role, Prompt, Schema]:
        """Returns the Role with its final prompt and schema, or raises LibraryError."""
        role = self.role(name, start)
        for skill in role.skills:
            if self.find("skills", skill) is None and skill not in self.kit_skills:
                raise LibraryError(f"role {name} in {role.layer} names skill {skill}, which is in no Layer")
        return role, self._named("prompt", role), self._named("schema", role)

    def materialize(self, name: str) -> tuple[Role, Path, Path, Schema]:
        """Returns the Role, the paths of its final prompt and schema, and the schema, whose files `flush` writes later.

        A path is named by the hash of its content, so Runs and Roles that agree on a content share the file.
        """
        assert self.resolved is not None, "a Library that materializes needs a directory for the files"
        role, prompt, schema = self.final_role(name)
        paths = []
        for text, suffix in ((prompt.text, ".md"), (json.dumps(schema.document, indent=2) + "\n", ".json")):
            path = self.resolved / f"{hashlib.sha256(text.encode()).hexdigest()[:16]}{suffix}"
            self._pending[path] = text
            paths.append(path)
        return role, paths[0], paths[1], schema

    def flush(self) -> None:
        """Writes the files of the Roles materialized so far that are not on disk yet."""
        for path, text in self._pending.items():
            if not path.is_file():
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
                temporary.write_text(text)
                temporary.replace(path)
        self._pending.clear()

    def _named(self, kind: str, role: Role) -> Any:
        name = role.prompt if kind == "prompt" else role.schema
        if self.find(f"{kind}s", name) is None:
            raise LibraryError(f"role {role.name} in {role.layer} names {kind} {name}, which is in no Layer")
        return self.prompt(name) if kind == "prompt" else self.schema(name)

    def check(self) -> list[str]:
        """Resolves every item of every Layer and returns what fails, one message per error."""
        errors: list[str] = []
        for index in range(len(self.layers)):
            for kind in ("prompts", "schemas", "roles"):
                for name in self.names(kind, index):
                    try:
                        if kind == "prompts":
                            self.prompt(name, index)
                        elif kind == "schemas":
                            self.schema(name, index)
                        else:
                            self.final_role(name, index)
                    except LibraryError as error:
                        if str(error) not in errors:
                            errors.append(str(error))
        return errors

    def _base_start(self, name: str, base: str, layer: int) -> int:
        """Returns the Layer to look up the base of an item from: one down for its own name, else its own."""
        return layer + 1 if base == name else layer

    def _prompt(self, name: str, start: int, chain: list[tuple[int, str]]) -> Prompt:
        found = self._item("prompt", "prompts", name, start, chain)
        layer = self.layers[found.layer].name
        try:
            meta, body = front_matter.split(found.path.read_text())
        except front_matter.FrontMatterError as error:
            raise LibraryError(f"prompt {name} in {layer}: {error}") from error
        base = meta.get("extends")
        if base is None:
            return Prompt(body, [(layer, name)])
        if not isinstance(base, str):
            raise LibraryError(f"prompt {name} in {layer}: `extends` must be a name")
        base_start = self._base_start(name, base, found.layer)
        self._need("prompt", "prompts", name, layer, base, base_start)
        below = self._prompt(base, base_start, [*chain, (found.layer, name)])
        text = f"{below.text.rstrip(chr(10))}\n\n{body.strip(chr(10))}\n"
        return Prompt(text, [*below.parts, (layer, name)])

    def _schema(self, name: str, start: int, chain: list[tuple[int, str]]) -> Schema:
        found = self._item("schema", "schemas", name, start, chain)
        layer = self.layers[found.layer].name
        try:
            document = json.loads(found.path.read_text())
        except json.JSONDecodeError as error:
            raise LibraryError(f"schema {name} in {layer} is not valid JSON: {error}") from error
        if not isinstance(document, dict):
            raise LibraryError(f"schema {name} in {layer} must be a JSON object")
        properties = document.get("properties", {})
        required = document.get("required", [])
        if not isinstance(properties, dict) or not isinstance(required, list):
            raise LibraryError(f"schema {name} in {layer}: `properties` must be an object and `required` a list")
        base = document.pop("x-extends", None)
        own = Schema(document, {p: layer for p in properties}, {r: layer for r in required}, [(layer, name)])
        if base is None:
            return own
        if not isinstance(base, str):
            raise LibraryError(f"schema {name} in {layer}: `x-extends` must be a name")
        base_start = self._base_start(name, base, found.layer)
        self._need("schema", "schemas", name, layer, base, base_start)
        below = self._schema(base, base_start, [*chain, (found.layer, name)])
        return self._merge(name, layer, below, own)

    def _merge(self, name: str, layer: str, below: Schema, own: Schema) -> Schema:
        document = dict(below.document)
        properties = dict(below.document.get("properties", {}))
        sources = dict(below.properties)
        for prop, definition in own.document.get("properties", {}).items():
            if prop in properties and properties[prop] != definition:
                raise LibraryError(
                    f"schema {name} in {layer} defines property {prop} differently than {below.properties[prop]} does"
                )
            properties.setdefault(prop, definition)
            sources.setdefault(prop, layer)
        for key, value in own.document.items():
            if key in SCHEMA_STRUCTURE:
                continue
            if key in document and document[key] != value:
                base_layer, base_name = below.parts[-1]
                raise LibraryError(
                    f"schema {name} in {layer} sets {key} differently than schema {base_name} in {base_layer} does"
                )
            document[key] = value
        required = dict(below.required)
        for field_name in own.required:
            required.setdefault(field_name, layer)
        document["properties"] = properties
        if required:
            document["required"] = list(required)
        return Schema(document, sources, required, [*below.parts, (layer, name)])

    def _item(self, kind: str, directory: str, name: str, start: int, chain: list[tuple[int, str]]) -> Found:
        found = self.find(directory, name, start)
        if found is None:
            raise LibraryError(f"{kind} {name} is in no Layer")
        if (found.layer, name) in chain:
            cycle = [*chain[chain.index((found.layer, name)) :], (found.layer, name)]
            steps = " -> ".join(f"{n} in {self.layers[i].name}" for i, n in cycle)
            raise LibraryError(f"{kind} extension cycle: {steps}")
        return found

    def _need(self, kind: str, directory: str, name: str, layer: str, base: str, start: int) -> None:
        if self.find(directory, base, start) is None:
            below = self.layers[start].name if start < len(self.layers) else "the bottom"
            raise LibraryError(f"{kind} {name} in {layer} extends {base}, which is in no Layer from {below} down")

    def show(self, name: str) -> str:
        """Returns the final prompt and schema of a Role and the Layer each part came from."""
        role, prompt, schema = self.final_role(name)
        lines = [f"role {role.name} ({role.layer})"]
        lines.append(f"prompt {role.prompt}: " + ", ".join(f"{n} from {layer}" for layer, n in prompt.parts))
        lines.append(prompt.text.rstrip("\n"))
        lines.append(f"schema {role.schema}: " + ", ".join(f"{n} from {layer}" for layer, n in schema.parts))
        lines.append(json.dumps(schema.document, indent=2))
        for prop, layer in schema.properties.items():
            lines.append(f"  property {prop} from {layer}")
        for prop, layer in schema.required.items():
            lines.append(f"  required {prop} from {layer}")
        return "\n".join(lines)
