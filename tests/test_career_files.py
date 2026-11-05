import pytest

from orchestrator.latex import escape_latex, latex_text, render_project, render_snippets
from orchestrator.resume_update import ResumeUpdateError, apply_changes, dump_resume
from orchestrator.website_entry import WebsiteEntryError, insert_entry, render_entry
from schemas import Link, ResumeAction, ResumeData, WebsiteEntry
from tests.test_career_rules import bullet, change, entry, resume

# --- LaTeX -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("M&A cases", r"M\&A cases"),
        ("74% coverage", r"74\% coverage"),
        ("$45M", r"\$45M"),
        ("snake_case #1", r"snake\_case \#1"),
        ("rendered in ~40 ms", r"rendered in $\sim$40 ms"),
        ("about ~ that", r"about \textasciitilde{} that"),
        ("{x}^y", r"\{x\}\textasciicircum{}y"),
        ("a\\b", r"a\textbackslash{}b"),
    ],
)
def test_special_characters_are_escaped(text: str, expected: str) -> None:
    assert escape_latex(text) == expected


def test_bold_markers_become_textbf_and_their_contents_are_escaped_too() -> None:
    assert latex_text("Cut **$4.20** of M&A") == r"Cut \textbf{\$4.20} of M\&A"
    assert latex_text("**a** and **b**") == r"\textbf{a} and \textbf{b}"


def test_a_project_entry_uses_only_the_templates_own_macros() -> None:
    c = change(
        ResumeAction.ADD,
        bullet("Built a **9-check** gate", ["qa"]),
        bullet("Cut M&A time", ["qa"]),
        entry="Agent Studio",
        tech="Python, Claude Agent SDK",
    )

    text = render_project(c)

    assert text == (
        "\\resumeProject\n"
        "  {\\textbf{Agent Studio} $|$ \\emph{Python, Claude Agent SDK}}{}\n"
        "\\resumeItemListStart\n"
        "  \\resumeItem{Built a \\textbf{9-check} gate}\n"
        "  \\resumeItem{Cut M\\&A time}\n"
        "\\resumeItemListEnd\n"
    )


def test_a_project_without_a_tech_line_has_no_emph() -> None:
    assert "\\emph" not in render_project(change(ResumeAction.ADD, tech=None))


def test_snippets_say_what_to_remove_and_other_sections_get_plain_items() -> None:
    cut = change(ResumeAction.REPLACE, replaces="Old project", tech="Go")
    other = change(ResumeAction.ADD, section="Experience", entry="CUBE")

    text = render_snippets([cut, other])

    assert "% REPLACE: Agent Studio (Projects)\n% Remove first: Old project" in text
    assert "\\resumeProject" in text
    assert "% ADD: CUBE (Experience)\n% Add these bullets under the matching entry:" in text
    assert text.count("\\resumeProject") == 1


# --- resume data -----------------------------------------------------------------------------


def test_a_replace_removes_the_named_entry_and_adds_the_new_one() -> None:
    new, notes = apply_changes(
        resume(max_bullets=2, bullets=2),
        [change(ResumeAction.REPLACE, replaces="Old project", tech="Go")],
    )

    section = new.section("projects")
    assert section is not None
    assert [e.title for e in section.entries] == ["Agent Studio"]
    assert section.entries[0].subtitle == "Go" and notes == []


def test_a_replace_naming_an_unknown_entry_adds_a_note_to_do_it_by_hand() -> None:
    new, notes = apply_changes(
        resume(max_bullets=3, bullets=2), [change(ResumeAction.REPLACE, replaces="Nope")]
    )

    section = new.section("projects")
    assert section is not None and len(section.entries) == 2
    assert any("'Nope' is not an entry" in n for n in notes)


def test_skips_change_nothing() -> None:
    original = resume()

    new, notes = apply_changes(original, [change(ResumeAction.SKIP, bullets=[])])

    assert new == original and notes == []


def test_a_change_that_would_break_the_page_budget_is_refused() -> None:
    with pytest.raises(ResumeUpdateError, match="page budget"):
        apply_changes(resume(max_bullets=2, bullets=2), [change(ResumeAction.ADD)])


def test_an_unknown_section_is_refused() -> None:
    with pytest.raises(ResumeUpdateError, match="no section"):
        apply_changes(resume(), [change(ResumeAction.ADD, section="Hobbies")])


def test_the_dumped_resume_keeps_the_header_comments_and_round_trips() -> None:
    import yaml

    original = "# keep me\n# and me\nsections: []\n"

    text = dump_resume(resume(), original)

    assert text.startswith("# keep me\n# and me\n")
    assert ResumeData.model_validate(yaml.safe_load(text)) == resume()


# --- the website entry -----------------------------------------------------------------------

SOURCE = """\
export const projects = [
    {
        slug: "jamtown",
        title: "Jamtown",
    },
];
"""


def test_the_entry_is_written_in_the_files_own_style() -> None:
    site = entry(
        scope=None,
        links=[Link(label="GitHub", href="https://github.com/me/x")],
        problem='He said "hi" & left',
    )

    text = render_entry(site)

    assert text.startswith('    {\n        slug: "agent-studio",\n')
    assert "        scope: null,\n" in text
    assert '        problem:\n            "He said \\"hi\\" & left",\n' in text
    assert '        links: [{ label: "GitHub", href: "https://github.com/me/x" }],\n' in text
    assert text.endswith("    },\n")


def test_a_scope_is_a_quoted_string_when_given() -> None:
    assert '        scope: "CUBE client work",\n' in render_entry(entry(scope="CUBE client work"))


def test_the_entry_goes_at_the_top_of_the_list_and_nothing_else_changes() -> None:
    out = insert_entry(SOURCE, entry())

    assert out.startswith('export const projects = [\n    {\n        slug: "agent-studio"')
    assert out.endswith(SOURCE[len("export const projects = [\n") :])


def test_a_duplicate_slug_is_refused() -> None:
    with pytest.raises(WebsiteEntryError, match="already has"):
        insert_entry(SOURCE, entry(slug="jamtown"))


def test_a_file_without_the_projects_array_is_refused() -> None:
    with pytest.raises(WebsiteEntryError, match="could not find"):
        insert_entry("export const other = [];\n", entry())


def test_the_website_entry_type_is_what_the_site_expects() -> None:
    assert WebsiteEntry.model_fields.keys() >= {
        "slug",
        "title",
        "hook",
        "metric",
        "scope",
        "problem",
        "approach",
        "result",
        "tags",
        "links",
    }
