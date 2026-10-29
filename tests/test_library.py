import json
from pathlib import Path

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


def test_a_missing_layer_is_empty(factory: Harness) -> None:
    assert factory.check().code == 0


def test_project_replaces_user_and_factory(factory: Harness) -> None:
    where = layers(factory)
    for name, root in where.items():
        put(root, "prompts/p.md", f"from {name}\n")
        put(root, "schemas/s.json", schema([f"{name}_field"], []))
    put(where["factory"], "roles/r.yaml", role())
    put(where["user"], "roles/r.yaml", role(prompt="p2"))
    put(where["user"], "prompts/p2.md", "user p2\n")
    put(where["project"], "roles/r.yaml", role())

    out = show(factory)

    assert out.startswith("role r (project)")
    assert "from project" in out and "from user" not in out and "from factory" not in out.split("\n", 3)[2]
    assert "project_field" in out and "user_field" not in out


def test_lookup_falls_through_to_lower_layers(factory: Harness) -> None:
    where = layers(factory)
    put(where["factory"], "prompts/p.md", "base\n")
    put(where["user"], "schemas/s.json", schema(["a"], ["a"]))
    put(where["factory"], "roles/r.yaml", role())

    out = show(factory)

    assert out.startswith("role r (factory)")
    assert "property a from user" in out


def test_prompt_extends_the_prompt_below_it(factory: Harness) -> None:
    where = layers(factory)
    put(where["factory"], "prompts/p.md", "Base text.\n")
    put(where["user"], "prompts/p.md", "---\nextends: p\n---\nUser text.\n")
    put(where["project"], "prompts/p.md", "---\nextends: p\n---\nProject text.\n")
    put(where["factory"], "schemas/s.json", schema([], []))
    put(where["factory"], "roles/r.yaml", role())

    out = show(factory)

    assert "Base text.\n\nUser text.\n\nProject text." in out
    assert "p from factory, p from user, p from project" in out


def test_schema_extends_by_merging_properties_and_required(factory: Harness) -> None:
    where = layers(factory)
    put(where["factory"], "schemas/s.json", schema(["summary"], ["summary"], additionalProperties=False))
    put(
        where["project"], "schemas/s.json", schema(["summary", "risk"], ["risk"]).replace("{", '{"x-extends": "s", ', 1)
    )
    put(where["factory"], "prompts/p.md", "x\n")
    put(where["factory"], "roles/r.yaml", role())

    out = show(factory)

    assert "property summary from factory" in out
    assert "property risk from project" in out
    assert "required summary from factory" in out and "required risk from project" in out
    assert '"additionalProperties": false' in out and "x-extends" not in out


def test_an_extension_naming_another_item_resolves_from_its_own_layer(factory: Harness) -> None:
    where = layers(factory)
    put(where["user"], "prompts/shared.md", "User shared.\n")
    put(where["factory"], "prompts/shared.md", "Factory shared.\n")
    put(where["project"], "prompts/p.md", "---\nextends: shared\n---\nMine.\n")
    put(where["factory"], "schemas/s.json", schema([], []))
    put(where["factory"], "roles/r.yaml", role())

    out = show(factory)

    assert "User shared.\n\nMine." in out and "Factory shared." not in out


def test_an_extension_cycle_names_the_layers(factory: Harness) -> None:
    where = layers(factory)
    put(where["project"], "prompts/a.md", "---\nextends: b\n---\nA\n")
    put(where["project"], "prompts/b.md", "---\nextends: a\n---\nB\n")

    assert errors_of(factory) == [
        "prompt extension cycle: a in project -> b in project -> a in project",
        "prompt extension cycle: b in project -> a in project -> b in project",
    ]


def test_a_missing_base_names_the_layers(factory: Harness) -> None:
    put(layers(factory)["user"], "prompts/p.md", "---\nextends: p\n---\nMine.\n")

    assert errors_of(factory) == ["prompt p in user extends p, which is in no Layer from factory down"]


def test_a_conflicting_property_names_the_layers(factory: Harness) -> None:
    where = layers(factory)
    put(where["factory"], "schemas/s.json", schema(["a"], []))
    put(where["user"], "schemas/s.json", json.dumps({"x-extends": "s", "properties": {"a": {"type": "integer"}}}))

    assert errors_of(factory) == ["schema s in user defines property a differently than factory does"]


def test_show_of_an_unknown_role_is_an_error(factory: Harness) -> None:
    assert errors_of(factory, "--show", "nope") == ["role nope is in no Layer"]


def test_an_extension_naming_another_item_skips_the_layers_above_its_own(factory: Harness) -> None:
    where = layers(factory)
    put(where["project"], "prompts/shared.md", "Project shared.\n")
    put(where["factory"], "prompts/shared.md", "Factory shared.\n")
    put(where["user"], "prompts/p.md", "---\nextends: shared\n---\nMine.\n")
    put(where["factory"], "schemas/s.json", schema([], []))
    put(where["factory"], "roles/r.yaml", role())

    out = show(factory)

    assert "Factory shared.\n\nMine." in out and "Project shared." not in out


def test_a_conflicting_schema_key_names_the_layers(factory: Harness) -> None:
    where = layers(factory)
    put(where["factory"], "schemas/s.json", schema([], [], additionalProperties=False))
    put(where["user"], "schemas/s.json", json.dumps({"x-extends": "s", "additionalProperties": True}))

    assert errors_of(factory) == [
        "schema s in user sets additionalProperties differently than schema s in factory does"
    ]


def test_a_broken_role_fails_check_even_when_an_upper_layer_replaces_it(factory: Harness) -> None:
    where = layers(factory)
    put(where["factory"], "prompts/p.md", "x\n")
    put(where["factory"], "schemas/s.json", schema([], []))
    put(where["project"], "roles/r.yaml", role())
    put(where["factory"], "roles/r.yaml", role(prompt="missing"))

    assert errors_of(factory) == ["role r in factory names prompt missing, which is in no Layer"]
