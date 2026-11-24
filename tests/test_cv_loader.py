"""Unit tests for najdi_praci.cv_loader."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from najdi_praci.cv_loader import CVLoadError, build_cv_summary, load_cv_text


def test_load_cv_text_markdown_and_txt(tmp_path: Path) -> None:
    md_file = tmp_path / "cv.md"
    md_file.write_text("  # Jan Novák\nPython Developer  \n", encoding="utf-8")
    assert load_cv_text(md_file) == "# Jan Novák\nPython Developer"

    txt_file = tmp_path / "cv.TXT"
    txt_file.write_text("\ufeffSenior QA Engineer\nPraha", encoding="utf-8")
    assert load_cv_text(txt_file) == "Senior QA Engineer\nPraha"


def test_load_cv_text_pdf_extracts_all_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf_file = tmp_path / "resume.pdf"
    pdf_file.write_bytes(b"%PDF-1.4 dummy")

    fake_pages = [
        SimpleNamespace(extract_text=lambda: "Page 1: Backend Engineer"),
        SimpleNamespace(extract_text=lambda: None),
        SimpleNamespace(extract_text=lambda: "Page 3: Python, SQL"),
    ]
    monkeypatch.setattr(
        "najdi_praci.cv_loader.PdfReader",
        lambda path: SimpleNamespace(pages=fake_pages),
    )

    result = load_cv_text(pdf_file)
    assert "Page 1: Backend Engineer" in result
    assert "Page 3: Python, SQL" in result


def test_load_cv_text_pdf_parse_error_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf_file = tmp_path / "broken.pdf"
    pdf_file.write_bytes(b"not a real pdf")

    def _boom(_path: str) -> object:
        raise ValueError("corrupt PDF")

    monkeypatch.setattr("najdi_praci.cv_loader.PdfReader", _boom)

    with pytest.raises(CVLoadError, match="Failed to parse PDF"):
        load_cv_text(pdf_file)


def test_load_cv_text_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(CVLoadError, match="not found"):
        load_cv_text(tmp_path / "missing.md")


def test_load_cv_text_directory_raises(tmp_path: Path) -> None:
    with pytest.raises(CVLoadError, match="not a file"):
        load_cv_text(tmp_path)


def test_load_cv_text_unsupported_extension_raises(tmp_path: Path) -> None:
    docx_file = tmp_path / "cv.docx"
    docx_file.write_text("content", encoding="utf-8")

    with pytest.raises(CVLoadError, match="Unsupported CV file extension"):
        load_cv_text(docx_file)


def test_load_cv_text_empty_file_raises(tmp_path: Path) -> None:
    empty_file = tmp_path / "empty.md"
    empty_file.write_text("   \n\t  \n", encoding="utf-8")

    with pytest.raises(CVLoadError, match="no extractable text"):
        load_cv_text(empty_file)


def test_build_cv_summary_normalizes_whitespace() -> None:
    raw = "  Senior   Python\tDeveloper  \n\n\n  Skills:   FastAPI,   SQLite \n  "
    assert build_cv_summary(raw) == "Senior Python Developer\nSkills: FastAPI, SQLite"


def test_build_cv_summary_truncates_cleanly() -> None:
    text = " ".join(["python"] * 50)
    summary = build_cv_summary(text, max_chars=40)
    assert len(summary) <= 40
    assert summary.endswith("...")
    assert build_cv_summary(text, max_chars=0) == ""
