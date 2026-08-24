from pathlib import Path

from okb.classify import classify, mime_of


def test_office_formats_are_readable():
    for name in ["a.docx", "b.xlsx", "c.pptx", "d.md", "e.txt", "f.DOC", "g.rtf"]:
        assert classify(Path(name)) == "readable", name


def test_images():
    for name in ["photo.jpg", "scan.PNG", "x.webp"]:
        assert classify(Path(name)) == "image", name


def test_unknown_is_other():
    assert classify(Path("archive.zip")) == "other"
    assert classify(Path("noext")) == "other"


def test_mime_fallback():
    assert mime_of(Path("weird.zzz")) == "application/octet-stream"
    assert "wordprocessingml" in mime_of(Path("a.docx"))
