"""Документ в текст — чтобы ИИ-клиент мог его прочитать.

Поддерживает .pdf, .docx, .xlsx, .pptx, .csv и обычный текст. Пишет результат
в stdout или в файл рядом с исходником.

Текст не кладётся в репозиторий: это промежуточный результат, а исходный
документ может содержать то, чему в git не место.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SUPPORTED = {".pdf", ".docx", ".xlsx", ".pptx", ".csv", ".txt", ".md"}


def from_pdf(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    pages = []
    for number, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        # Номер страницы нужен, чтобы на извлечённый факт можно было сослаться.
        pages.append(f"\n--- страница {number} ---\n{text}")
    return "\n".join(pages)


def from_docx(path: Path) -> str:
    from docx import Document

    doc = Document(str(path))
    parts = [p.text for p in doc.paragraphs if p.text.strip()]
    for number, table in enumerate(doc.tables, start=1):
        parts.append(f"\n--- таблица {number} ---")
        for row in table.rows:
            parts.append(" | ".join(cell.text.strip() for cell in row.cells))
    return "\n".join(parts)


def from_xlsx(path: Path) -> str:
    from openpyxl import load_workbook

    # data_only: нужны значения формул, а не сами формулы.
    book = load_workbook(str(path), data_only=True, read_only=True)
    parts = []
    for sheet in book.worksheets:
        parts.append(f"\n--- лист: {sheet.title} ---")
        for row in sheet.iter_rows(values_only=True):
            if any(cell is not None and str(cell).strip() for cell in row):
                parts.append(" | ".join("" if c is None else str(c) for c in row))
    book.close()
    return "\n".join(parts)


def from_pptx(path: Path) -> str:
    from pptx import Presentation

    deck = Presentation(str(path))
    parts = []
    for number, slide in enumerate(deck.slides, start=1):
        parts.append(f"\n--- слайд {number} ---")
        for shape in slide.shapes:
            if shape.has_text_frame and shape.text_frame.text.strip():
                parts.append(shape.text_frame.text)
    return "\n".join(parts)


READERS = {
    ".pdf": from_pdf,
    ".docx": from_docx,
    ".xlsx": from_xlsx,
    ".pptx": from_pptx,
}


def convert(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED:
        raise SystemExit(
            f"{path.name}: формат {suffix or 'без расширения'} не поддерживается. "
            f"Поддерживаются: {', '.join(sorted(SUPPORTED))}"
        )
    reader = READERS.get(suffix)
    if reader is None:
        return path.read_text(encoding="utf-8", errors="replace")
    try:
        return reader(path)
    except ImportError as exc:
        raise SystemExit(
            f"Не хватает библиотеки для {suffix}: {exc.name}. "
            f"Поставь зависимости: pip install -e '.[documents]'"
        ) from exc


def main() -> None:
    parser = argparse.ArgumentParser(description="Документ в текст для чтения ИИ-клиентом.")
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("-o", "--out-dir", type=Path, help="куда положить .txt (по умолчанию stdout)")
    args = parser.parse_args()

    for path in args.files:
        if not path.is_file():
            print(f"нет файла: {path}", file=sys.stderr)
            continue

        text = convert(path)
        if args.out_dir:
            args.out_dir.mkdir(parents=True, exist_ok=True)
            target = args.out_dir / f"{path.stem}.txt"
            target.write_text(text, encoding="utf-8")
            print(f"{path.name} → {target} ({len(text)} символов)")
        else:
            print(f"===== {path.name} =====")
            print(text)


if __name__ == "__main__":
    main()
