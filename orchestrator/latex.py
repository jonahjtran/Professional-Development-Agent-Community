"""Turning approved resume bullets into paste-ready LaTeX, in the person's own template.

The resume lives in Overleaf, so there is no file to edit. These snippets use only the macros
that template already defines (`\\resumeProject`, `\\resumeItemListStart`, `\\resumeItem`,
`\\resumeItemListEnd`), so they paste straight in. Bullet text is plain text with **bold**.
"""

import re

from schemas import ResumeChange

_SPECIAL = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "^": r"\textasciicircum{}",
}
_BOLD = re.compile(r"\*\*(.+?)\*\*")


def escape_latex(text: str) -> str:
    """Escape LaTeX's special characters; `~` before a digit becomes `$\\sim$`, as in the resume."""
    out = []
    for index, char in enumerate(text):
        if char == "~":
            nxt = text[index + 1 : index + 2]
            out.append(r"$\sim$" if nxt.isdigit() else r"\textasciitilde{}")
        else:
            out.append(_SPECIAL.get(char, char))
    return "".join(out)


def latex_text(text: str) -> str:
    """Plain text with **bold** as LaTeX: each piece is escaped, bold pieces get \\textbf."""
    parts: list[str] = []
    last = 0
    for match in _BOLD.finditer(text):
        parts.append(escape_latex(text[last : match.start()]))
        parts.append(rf"\textbf{{{escape_latex(match.group(1))}}}")
        last = match.end()
    parts.append(escape_latex(text[last:]))
    return "".join(parts)


def render_items(bullets: list[str]) -> str:
    items = "\n".join(rf"  \resumeItem{{{latex_text(b)}}}" for b in bullets)
    return f"\\resumeItemListStart\n{items}\n\\resumeItemListEnd"


def render_project(change: ResumeChange) -> str:
    """One Projects entry, ready to paste inside the Projects list."""
    heading = rf"\textbf{{{escape_latex(change.entry)}}}"
    if change.tech:
        heading += rf" $|$ \emph{{{escape_latex(change.tech)}}}"
    items = render_items([b.text for b in change.bullets])
    return f"\\resumeProject\n  {{{heading}}}{{}}\n{items}\n"


def render_snippets(changes: list[ResumeChange]) -> str:
    """Every approved resume change as LaTeX, with a comment saying what to cut, if anything."""
    blocks = []
    for change in changes:
        lines = [f"% {change.action.value.upper()}: {change.entry} ({change.section})"]
        if change.replaces:
            lines.append(f"% Remove first: {change.replaces}")
        if change.section.strip().lower() == "projects":
            lines.append(render_project(change))
        else:
            lines.append("% Add these bullets under the matching entry:")
            lines.append(render_items([b.text for b in change.bullets]))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"
