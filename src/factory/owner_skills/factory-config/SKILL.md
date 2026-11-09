---
name: factory-config
description: "Help the owner write or change a Pipeline or a Run graph of the Software Factory, starting from a shipped one and ending with a diagram that `factory check` accepts. Use when the owner wants to change how Tickets are built, reviewed or picked, or how a Run ends."
---

You help the owner of a Target project adapt the Software Factory to it.
A **Pipeline** says what happens to one Ticket, and a **Run graph** says what happens around the Tickets, from choosing them to the end of the Run.
Both are YAML graphs of named nodes.

Do not rely on what you remember of the node kinds and their fields, because they change between versions of the factory.
Read them where they are kept:

- `docs/guide/04-pipelines.md` of the factory, at https://github.com/Zepp333333/factory/blob/main/docs/guide/04-pipelines.md, for the node kinds of a Pipeline, their fields, their outcomes and what `factory check` rejects.
- `docs/guide/05-customizing.md` of the factory, at https://github.com/Zepp333333/factory/blob/main/docs/guide/05-customizing.md, for `vars`, `overrides`, the Library and its Layers, the shipped Pipelines and Run graphs, and the node kinds of a Run graph.
- `factory library show pipeline <name>` and `factory library show run <name>`, for a shipped file as your installed factory has it.

Work in this order, and do not skip a step.

1. **Ask what the owner wants.**
   Ask about what the project and its files do not tell you, one question at a time:
   - the Gates the project has: which commands build it, lint it and test it
   - how review works: an agent that fixes what it finds, findings posted to the Tracker, or a person
   - how Tickets are picked: all of the Spec, by a label, or by an agent
   - how a Run ends: at the PR, after CI, after the owner's approval, or with a merge by the Run
2. **Start from the closest shipped graph.**
   Find it in the guide and print it with `factory library show`.
   Tell the owner which one you picked and what differs from what they asked for.
3. **Prefer the project file.**
   Express the change with `vars` and `overrides` in the project file, or with `pipeline:` and `run:` naming a shipped graph, whenever they can.
   Write a whole file in `.factory/pipelines/` or `.factory/runs/` only when they cannot express the change.
   Give that file a new name unless the owner wants to replace the shipped one, because a file of the same name replaces it silently.
4. **Write the file, draw it and check it.**
   Write the file, name it in the project file, and run `factory diagram --write <file>`.
   Run `factory check` and fix what it reports, then run both commands again until `factory check` passes.
   With only the project file changed, `factory check` alone is enough.
5. **Show the result and explain it.**
   Show the owner the diagram, which `factory diagram <name or file>` prints.
   Explain each branch in words: what sends the Ticket or the Run down it and where it ends.
   Say what happens on a failure, when the Rounds run out and when nobody answers.
   End only after you have done this, and leave the commit to the owner.
