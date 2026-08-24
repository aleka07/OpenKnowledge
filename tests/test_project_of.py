from okb.ingest import project_of

MIRROR = "/home/gx10-1/kb-data/mirrors/pcf-cd-24-26"


def test_nested_folder_becomes_project():
    p = project_of(f"{MIRROR}/2025/Договор услуг!!!!/ISO/2025/file.docx")
    assert p == "2025/Договор услуг!!!!/ISO/2025"


def test_top_level_folder():
    assert project_of(f"{MIRROR}/2025/file.pdf") == "2025"


def test_file_in_mirror_root_has_no_project():
    assert project_of(f"{MIRROR}/file.pdf") is None


def test_non_mirror_paths_have_no_project():
    assert project_of("/home/gx10-1/kb-data/inbox/file.pdf") is None
    assert project_of("/home/gx10-1/kb-data/notes/2026-01-01-note.md") is None


def test_none_path():
    assert project_of(None) is None
