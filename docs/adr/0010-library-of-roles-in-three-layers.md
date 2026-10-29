# Roles, prompts, schemas, skills and Pipelines come from a Library of three Layers

A Pipeline for a new Target project must be assembled from ready Roles instead of a copy of another Pipeline that is then edited.
So Roles, prompts, schemas, skills and Pipelines form a Library of three Layers: `factory` in `src/factory/library/`, `user` in `$FACTORY_HOME/library/` and `project` in `.factory/` of the Target project.
Every Layer has the same layout: `pipelines/`, `roles/`, `prompts/`, `schemas/` and `skills/`.
An item is found by name from `project` down to `factory`.
A file without `extends` replaces the item of the same name below it whole, and a file with `extends` extends its base.
`extends` that names the item's own name finds the base from one Layer down; any other name is found from the top down, starting at the item's own Layer.
A prompt declares `extends` in its front matter and a schema in the top-level key `"x-extends"`, and each has at most one base.
A schema extension only adds: properties merge by name, `required` is the union, and properties that disagree between Layers are a check error.
A prompt extension appends its text to the base, and a prompt stays a static file without templating.
A Role, a skill and a Pipeline are replaced only whole.
The engine relies only on its own output contract, and everything else in an output is an agreement between the Roles of one Pipeline.

This changes two points of ADR 0004.
A Pipeline has no `roles:` section: a Stage, a model Decider and `describer:` name a Role, and the Role is found in the Library.
The project file may assign a Stage any Role of the Library through `overrides`, and the Role's own fields still belong to the Role.

## Considered Options

- Keep Roles inline in the Pipeline and add only prompts and schemas to the Library.
  Rejected because the project file could then assign a Stage only a Role its Pipeline already declares.
- Extend a schema through JSON Schema `allOf`.
  Rejected because the result is hard to read and the check cannot print one final schema.
- Let an extension remove a field or loosen a constraint.
  Rejected because a Role that reads the field breaks far from the change; to loosen, a Layer replaces the schema whole.
- Extend implicitly by name, with a flag for replacement.
  Rejected because a replacement and an extension would look the same on disk.
- Declare the base on the Role instead of the file.
  Rejected because one prompt would then extend different bases under different Roles, and the check could not show it alone.
- Several bases per schema.
  Rejected because shared fields chain through one base, such as `merge-danger` to `reviewer-single` to `reviewer`, and several bases add a merge order and a second kind of conflict.
- Extend Roles, skills and Pipelines too.
  Rejected because a Role is a few names, a skill is a directory with no clean way to append to it, and merging Pipeline graphs makes routes hard to reason about; Pipelines reuse Roles, `vars` and `overrides` instead.
- Key a Role's reads by the name of the Role it reads from.
  Rejected because a Pipeline that replaces `reviewer` with `reviewer-single` would fail the check even when the fields are there.
- Read the Library live on every Attempt.
  Rejected because a `user` Layer edit between `factory run` and `factory resume` would give one Run two prompts under one name.
- Keep the project Layer next to the project file, as `pipelines/` is now.
  Rejected because a hidden `.factory/` says that the engine owns its layout and does not mix it with the project's own files.

## Consequences

- The project file names a Pipeline with `pipeline: <name>` or `pipeline: {file: <path>}`, and a Pipeline given by file finds its Roles through the Library too.
- The engine's output contract is checked by `factory check` on the final schema of every Role:
  - the `summary` of a Stage, if present, is a string, and for `workspace: rw` it becomes the first line of the Stage boundary commit (ADR 0009);
  - the Describer outputs `title` and `body`;
  - a model Decider outputs `outcome` as an `enum` whose values are a subset of the Decision's outcomes, and may output `reasons`.
  `handoff`, `findings` and the fields of Merge Danger are not part of the contract.
- A Role declares `reads`, the output fields it expects, each required or optional.
  A required field must come from the Stage's `inputs` on every path to it, in the spirit of ADR 0003; an optional field only has to match in type where it comes.
  The Describer has no `inputs`, so a required field of its reads must be in the output of at least one Stage of the Pipeline.
- `factory check --show <role>` prints the final prompt and schema of a Role after all Layers.
- At its start a Run resolves the Library once and keeps the final prompts, schemas and skills as Artifacts, and `factory resume` uses them.
  `prompt_sha` and `schema_sha` hash the final content, and `kit_sha` covers the Agent kit assembled with the resolved skills.
- Skills of the Library reach the agent through the assembled kit plugin, under `profile: hermetic` too.
  The Target project's `.claude/skills` stay a separate thing that loads only under `profile: project`.
- The `factory` Layer holds what fits any Target project: `implementer`, `reviewer`, `reconciler`, `describer`, `reviewer-single` with its skill `review-single`, the schema `merge-danger`, and the Pipeline `tdd-review`.
  The skill `review` stays two-axis, and `reviewer-single` does not branch inside it.
- In this repository the factory is also the Target project, and what sets `self` apart lives in `.factory/`.
  A self run takes its `factory` Layer from the stable install of `main`, so a Ticket's edit of `src/factory/library/` does not reach that Run, while an edit of `.factory/` reaches the Runs started from that branch.
- Out of scope: choosing a Pipeline by Ticket label, variables in prompts, and the Pipeline for the private C++ repository.
