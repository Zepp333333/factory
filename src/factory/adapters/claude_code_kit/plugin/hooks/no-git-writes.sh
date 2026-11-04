#!/bin/sh
# The factory commits at the end of a Stage, so an agent never runs a git command that writes.
# Claude Code gives the tool call as JSON on stdin, and exit code 2 blocks it with stderr as the reason.
if grep -Eq '"command"[[:space:]]*:[[:space:]]*"([^"\\]|\\.)*\bgit[[:space:]]+(-[Cc][[:space:]]+[^[:space:]]+[[:space:]]+|-[^[:space:]]+[[:space:]]+)*(commit|stash|reset|checkout|switch|rebase|merge|push|pull|am|cherry-pick|revert|clean|update-ref)([^[:alnum:]_-]|$)'; then
  echo "The factory commits for you at the end of the Stage. Do not run git commands that write." >&2
  exit 2
fi
