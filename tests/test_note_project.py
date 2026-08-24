from okb.ingest import note_project

HEADER = """# Переход на новую модель

Author: alikhan (via add_note)
Date: 2026-08-24
Client: claude-code/2.1 from 1.2.3.4
Project: инфраструктура/vLLM

Текст заметки. Project: не считается — он не в шапке? (внутри первых 500 символов считается)
"""


def test_note_project_parsed_from_header():
    assert note_project(HEADER) == "инфраструктура/vLLM"


def test_note_without_project():
    assert note_project("# t\n\nAuthor: x\nDate: y\n\nтекст") is None


def test_project_line_deep_in_body_is_ignored():
    md = "# t\n\n" + ("слово " * 200) + "\nProject: поздно"
    assert note_project(md) is None
