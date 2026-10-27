---
name: tdd
description: "Build a Ticket test-first, one behavior at a time, in a red-green-refactor loop through the project's public interface. Use when implementing or fixing a Ticket."
---

Build the Ticket of the Brief one behavior at a time.

## Process

### 1. Plan the behaviors

Read the Ticket's acceptance criteria and list the behaviors they ask for, smallest first.
Find the public interface a test of each would use: the CLI, the API or the module a user calls.
Read the project's existing tests, and write yours the way they are written.

### 2. Red

Write one test for the next behavior and run it.
It must fail, and for the reason you expect: a missing behavior, not a typo or a broken import.
A test that passes before you write the code tests nothing.

### 3. Green

Write the least code that makes the test pass, and run the whole suite.
Do not write code for a behavior that has no failing test.

### 4. Refactor

With the suite green, remove duplication and fix names, then run the suite again.
Never refactor while a test is red.

### 5. Repeat

Go back to step 2 for the next behavior, until every acceptance criterion has a test that passes.

## Rules for the tests

- A test checks behavior that an owner or a caller sees, through the public interface.
  It does not know private functions, internal state or the order of calls inside.
  A refactor must not break it, and a change of behavior must.
- Use a real implementation or a fake with real behavior, not a mock that expects calls.
- One test, one behavior, with a name that says what happens.
- When a Gate reports a failure in your `cause`, first write or find the test that shows it, then fix.

## Before you finish

Run the project's full build and test commands and leave them green.
List in your `handoff` any criterion that has no test and why.
