"""CV loading and summarization utilities for najdi_praci."""

from __future__ import annotations

import re
from pathlib import Path

from pypdf import PdfReader

_SUPPORTED_EXTENSIONS = {".pdf", ".md", ".txt"}


class CVLoadError(Exception):
    """Raised when a CV file cannot be found, parsed, or has no extractable text."""


def load_cv_text(cv_path: Path) -> str:
    """Load and return text from a .pdf, .md, or .txt CV file."""
    if not cv_path.exists():
        raise CVLoadError(f"CV file not found: {cv_path}")
    if not cv_path.is_file():
        raise CVLoadError(f"CV path is not a file: {cv_path}")

    suffix = cv_path.suffix.lower()
    if suffix not in _SUPPORTED_EXTENSIONS:
        supported = ", ".join(sorted(_SUPPORTED_EXTENSIONS))
        raise CVLoadError(
            f"Unsupported CV file extension '{cv_path.suffix}'. Supported: {supported}"
        )

    if suffix == ".pdf":
        text = _extract_pdf_text(cv_path)
    else:
        try:
            text = cv_path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as exc:
            raise CVLoadError(f"Failed to read CV file {cv_path}: {exc}") from exc

    cleaned = text.strip()
    if not cleaned:
        raise CVLoadError(f"CV file contains no extractable text: {cv_path}")
    return cleaned


def _extract_pdf_text(cv_path: Path) -> str:
    """Extract text from all pages of a PDF file."""
    try:
        reader = PdfReader(str(cv_path))
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:
        raise CVLoadError(f"Failed to parse PDF CV {cv_path}: {exc}") from exc
    return "\n".join(pages)


def build_cv_summary(cv_text: str, max_chars: int = 2000) -> str:
    """Build a whitespace-normalized excerpt of the CV for Stage 1 title filtering."""
    if max_chars <= 0:
        return ""

    lines: list[str] = []
    for raw_line in cv_text.splitlines():
        normalized_line = re.sub(r"[ \t]+", " ", raw_line).strip()
        if normalized_line:
            lines.append(normalized_line)

    normalized = "\n".join(lines).strip()
    if len(normalized) <= max_chars:
        return normalized

    if max_chars <= 3:
        return normalized[:max_chars]

    cutoff = max_chars - 3
    truncated = normalized[:cutoff]
    last_space = truncated.rfind(" ")
    last_newline = truncated.rfind("\n")
    split_at = max(last_space, last_newline)
    if split_at > cutoff // 2:
        truncated = truncated[:split_at]

    return truncated.rstrip() + "..."
