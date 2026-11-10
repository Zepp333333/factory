---
name: factory-setup
description: "Set the Software Factory up in a new repository: check the machine, write the project file, prove the Gates on the base branch, create the labels and offer a trial Run. Use when the owner wants to start using the factory in a repository, or `factory doctor` fails."
---

You set the Software Factory up in the owner's repository, so the first Run works.
Do not rely on what you remember of the factory, because it changes between versions.
Read what `factory --help`, `factory doctor` and `factory check` print.
`docs/guide/06-operating.md` of the factory, at https://github.com/Zepp333333/factory/blob/main/docs/guide/06-operating.md, is the reference.

Work in this order, and do not skip a step.

1. **Check the machine.**
   Run `factory doctor`.
   Walk the owner through each failed check, one at a time, and run its `fix:` when you can.
   A step that needs the owner at a terminal, such as `sbx login` or signing the agent in inside a Sandbox, is the owner's: hand it over with the exact command, wait, and run `factory doctor` again.
   A failed check of the project file is expected while the repository has none, and the next step writes it.
2. **Write the project file.**
   Run `factory init`, or `factory init --yes` when the owner leaves the values to you.
   It never overwrites a `factory.yaml`, and `--force` writes `factory.yaml.new` next to it for the owner to compare.
   Read the repository to fill what detection missed and to correct what it guessed: the build command, the test command, and where the test command writes its JUnit and coverage output.
   Each report must land in `$GATE_OUT`.
   Keep the comment on each value true, saying where you found it.
   Run `factory check` and fix what it names until it passes.
3. **Prove the Gates.**
   Run `factory doctor --gates`, which runs the Gates once in a Sandbox on the base branch.
   A failure from a command you wrote is yours to correct.
   When a Gate fails because the code on the base branch is red, tell the owner which Gate and why, and leave the choice to them.
   Do not loosen the Gate, skip a test or lower a threshold to get a pass.
   A Run on a red base branch fails in every Ticket.
4. **Create the labels.**
   `factory doctor` names each label the Run graph needs and the Tracker lacks, with the command that creates it.
   List them, ask the owner, and create them only after the owner agrees.
5. **Offer a trial.**
   Offer a trial Spec with one small Ticket, such as a one-line change to the README, so the first Run proves the setup end to end.
   With the owner's agreement, write it the way the guide's section on Specs says: an issue with one sub-issue on the Tracker, or a folder with `spec.md` and `tickets/<id>.md`.
   Run `factory check <the Spec>` on it, and give the owner the `factory run <the Spec>` command, which is theirs to start.
6. **Hand over.**
   Tell the owner what is set up and what you left open.
   For the Pipeline, the Run graph and the finer settings, hand over to the skill `factory-config`, and name what the owner may want to change first.
   Leave the commit to the owner.
