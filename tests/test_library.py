"""`factory check` resolves the Library across its three Layers and names the Layers of every problem (ADR 0010)."""

import json
from pathlib import Path

import pytest
import yaml

from harness import Harness


def put(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def schema(properties: list[str], required: list[str], **extra: object) -> str:
    return json.dumps(
        {"type": "object", "required": required, "properties": {p: {"type": "string"} for p in properties}, **extra}
    )


def layers(factory: Harness) -> dict[str, Path]:
    return {"project": factory.repo / ".factory", "user": factory.home / "library", "factory": factory.factory_library}


def role(prompt: str = "p", schema: str = "s", skills: str = "[]") -> str:
    return f"prompt: {prompt}\nschema: {schema}\nskills: {skills}\n"


def show(factory: Harness, name: str = "r") -> str:
    result = factory.cli("check", "--project", str(factory.project_file), "--show", name)
    assert result.code == 0, result.err
    return result.out


def errors_of(factory: Harness, *flags: str) -> list[str]:
    result = factory.cli("check", "--project", str(factory.project_file), *flags)
    assert result.code != 0, result.out
    return [line.removeprefix("factory: ") for line in result.err.splitlines()]


def yaml_error(text: str) -> str:
    with pytest.raises(yaml.YAMLError) as caught:
        yaml.safe_load(text)
    return str(caught.value)


def test_each_item_comes_from_the_highest_layer_that_has_it_and_an_unknown_role_is_in_none(factory: Harness) -> None:
    where = layers(factory)
    put(where["factory"], "roles/r.yaml", role(prompt="p2"))
    put(where["factory"], "prompts/p2.md", "factory p2\n")
    put(where["project"], "roles/r.yaml", role())
    put(where["factory"], "prompts/p.md", "from factory\n")
    for name in ("project", "factory"):
        put(where[name], "schemas/s.json", schema([f"{name}_field"], []))

    assert factory.check().code == 0
    out = show(factory)

    assert not where["user"].exists()
    assert out.startswith("role r (project)\nprompt p: p from factory\nfrom factory\n")
    assert "factory p2" not in out
    assert "property project_field from project" in out and "factory_field" not in out
    assert errors_of(factory, "--show", "nope") == ["role nope is in no Layer"]


def test_prompt_and_schema_extend_the_ones_below_them_and_another_item_resolves_from_its_own_layer_down(
    factory: Harness,
) -> None:
    where = layers(factory)
    put(where["factory"], "prompts/p.md", "Base text.\n")
    put(where["user"], "prompts/p.md", "---\nextends: p\n---\nUser text.\n")
    put(where["project"], "prompts/p.md", "---\nextends: p\n---\nProject text.\n")
    put(where["factory"], "schemas/s.json", schema(["summary"], ["summary"], additionalProperties=False))
    extension = schema(["summary", "risk"], ["risk"]).replace("{", '{"x-extends": "s", ', 1)
    put(where["project"], "schemas/s.json", extension)
    put(where["factory"], "roles/r.yaml", role())
    put(where["project"], "prompts/q.md", "---\nextends: shared\n---\nMine.\n")
    put(where["user"], "prompts/shared.md", "---\nextends: base\n---\nUser shared.\n")
    put(where["factory"], "prompts/shared.md", "Factory shared.\n")
    put(where["project"], "prompts/base.md", "Project base.\n")
    put(where["factory"], "prompts/base.md", "Factory base.\n")
    put(where["factory"], "roles/q.yaml", role(prompt="q"))

    out = show(factory)
    other = show(factory, "q")

    assert "Base text.\n\nUser text.\n\nProject text." in out
    assert "p from factory, p from user, p from project" in out
    assert "property summary from factory" in out
    assert "property risk from project" in out
    assert "required summary from factory" in out and "required risk from project" in out
    assert '"additionalProperties": false' in out and "x-extends" not in out
    assert "Factory base.\n\nUser shared.\n\nMine." in other
    assert "Factory shared." not in other and "Project base." not in other


def test_check_names_the_layers_of_every_broken_item(factory: Harness) -> None:
    where = layers(factory)
    put(where["project"], "prompts/a.md", "---\nextends: b\n---\nA\n")
    put(where["project"], "prompts/b.md", "---\nextends: a\n---\nB\n")
    put(where["user"], "prompts/lonely.md", "---\nextends: lonely\n---\nMine.\n")
    put(where["factory"], "schemas/s.json", schema(["a"], []))
    put(where["user"], "schemas/s.json", json.dumps({"x-extends": "s", "properties": {"a": {"type": "integer"}}}))
    put(where["factory"], "schemas/t.json", schema([], [], additionalProperties=False))
    put(where["user"], "schemas/t.json", json.dumps({"x-extends": "t", "additionalProperties": True}))
    put(where["factory"], "prompts/p.md", "x\n")
    put(where["factory"], "schemas/plain.json", schema([], []))
    put(where["project"], "roles/r.yaml", role(schema="plain"))
    put(where["factory"], "roles/r.yaml", role(prompt="missing", schema="plain"))
    put(where["project"], "prompts/base-number.md", "---\nextends: 1\n---\nBody\n")
    put(where["project"], "prompts/unmapped.md", "---\n- a\n---\nBody\n")
    put(where["project"], "prompts/unread.md", "---\nextends: [\n---\nBody\n")
    put(where["project"], "schemas/base-number.json", json.dumps({"x-extends": 1}))
    put(where["project"], "schemas/not-json.json", "not json")
    put(where["project"], "schemas/not-object.json", "[]")
    put(where["project"], "schemas/properties-list.json", json.dumps({"properties": []}))
    put(where["project"], "schemas/required-text.json", json.dumps({"required": "a"}))
    put(where["project"], "roles/not-a-mapping.yaml", "- a\n")
    put(where["project"], "roles/not-yaml.yaml", "prompt: [\n")
    put(
        where["project"], "roles/reads-both.yaml", role() + "reads:\n  required: {a: string}\n  optional: {a: string}\n"
    )
    put(where["project"], "roles/reads-list.yaml", role() + "reads:\n  required: [a]\n")
    put(where["project"], "roles/reads-text.yaml", role() + "reads: a\n")
    put(where["project"], "roles/reads-unknown.yaml", role() + "reads:\n  needed: {}\n")
    put(where["project"], "roles/skills-numbers.yaml", role(skills="[1]"))
    put(where["project"], "roles/skills-text.yaml", role(skills="a"))
    put(where["project"], "roles/without-schema.yaml", "prompt: p\n")

    mapping = "must be a mapping with a `prompt` name, a `schema` name and a list of `skills`"
    reads = "`reads` must map `required` and `optional` to fields with a JSON type"
    # A YAML error spans several lines, so the lines of `factory check` are compared as one text.
    assert "\n".join(errors_of(factory)) == "\n".join(
        [
            "prompt extension cycle: a in project -> b in project -> a in project",
            "prompt extension cycle: b in project -> a in project -> b in project",
            "prompt base-number in project: `extends` must be a name",
            "prompt unmapped in project: front matter must be a YAML mapping",
            f"prompt unread in project: front matter is not valid YAML: {yaml_error('extends: [')}",
            "schema base-number in project: `x-extends` must be a name",
            "schema not-json in project is not valid JSON: Expecting value: line 1 column 1 (char 0)",
            "schema not-object in project must be a JSON object",
            "schema properties-list in project: `properties` must be an object and `required` a list",
            "schema required-text in project: `properties` must be an object and `required` a list",
            f"role not-a-mapping in project {mapping}",
            f"role not-yaml in project is not valid YAML: {yaml_error('prompt: [' + chr(10))}",
            "role reads-both in project: read a is both required and optional",
            f"role reads-list in project: {reads}",
            f"role reads-text in project: {reads}",
            f"role reads-unknown in project: {reads}",
            f"role skills-numbers in project {mapping}",
            f"role skills-text in project {mapping}",
            f"role without-schema in project {mapping}",
            "prompt lonely in user extends lonely, which is in no Layer from factory down",
            "schema s in user defines property a differently than factory does",
            "schema t in user sets additionalProperties differently than schema t in factory does",
            "role r in factory names prompt missing, which is in no Layer",
        ]
    )
