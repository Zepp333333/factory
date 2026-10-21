"""Front matter is the YAML mapping between two `---` lines at the top of a Markdown file."""

import re
from typing import Any

import yaml

FRONT_MATTER = re.compile(r"---\n(?:(.*?)\n)?---(?:\n|\Z)", re.DOTALL)


class FrontMatterError(ValueError):
    pass


def split(text: str) -> tuple[dict[str, Any], str]:
    """Returns the front matter and the body after it, or an empty mapping and the whole text when there is none."""
    match = FRONT_MATTER.match(text)
    if match is None:
        return {}, text
    try:
        meta = yaml.safe_load(match.group(1) or "")
    except yaml.YAMLError as error:
        raise FrontMatterError(f"front matter is not valid YAML: {error}") from error
    if meta is None:
        meta = {}
    if not isinstance(meta, dict):
        raise FrontMatterError("front matter must be a YAML mapping")
    return meta, text[match.end() :]
