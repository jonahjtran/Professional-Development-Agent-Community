"""Adding a project to the portfolio site's `src/data/projects.js`, deterministically.

Code writes the entry in the file's own style and inserts it; a model never edits the JavaScript.
Strings are written as JSON strings, which are valid JavaScript, so no text can break the file.
"""

import json
import re

from schemas import WebsiteEntry

_ARRAY_START = re.compile(r"^export const projects = \[\s*\n", re.MULTILINE)


class WebsiteEntryError(Exception):
    """The entry cannot be added to the file."""


def _string(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)


def render_entry(entry: WebsiteEntry) -> str:
    tags = ", ".join(_string(t) for t in entry.tags)
    links = ", ".join(
        f"{{ label: {_string(link.label)}, href: {_string(link.href)} }}" for link in entry.links
    )
    scope = _string(entry.scope) if entry.scope else "null"
    return (
        "    {\n"
        f"        slug: {_string(entry.slug)},\n"
        f"        title: {_string(entry.title)},\n"
        f"        hook: {_string(entry.hook)},\n"
        f"        metric: {_string(entry.metric)},\n"
        f"        scope: {scope},\n"
        f"        problem:\n            {_string(entry.problem)},\n"
        f"        approach:\n            {_string(entry.approach)},\n"
        f"        result:\n            {_string(entry.result)},\n"
        f"        tags: [{tags}],\n"
        f"        links: [{links}],\n"
        "    },\n"
    )


def insert_entry(source: str, entry: WebsiteEntry) -> str:
    """The file with `entry` added at the top of the projects list."""
    if re.search(rf"""slug:\s*["']{re.escape(entry.slug)}["']""", source):
        raise WebsiteEntryError(f"the site already has a project with the slug {entry.slug!r}")
    start = _ARRAY_START.search(source)
    if start is None:
        raise WebsiteEntryError("could not find `export const projects = [` in projects.js")
    return source[: start.end()] + render_entry(entry) + source[start.end() :]
