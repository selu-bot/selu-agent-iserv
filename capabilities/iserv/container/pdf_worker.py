"""One-shot PDF extraction worker. Receives bytes on stdin, emits bounded JSON."""
from __future__ import annotations

import json
import logging
import sys
from io import BytesIO

MAX_PDF_BYTES = 5 * 1024 * 1024
MAX_PDF_PAGES = 50
MAX_PDF_TEXT_CHARS = 100_000


def extract_pdf(data: bytes) -> dict:
    from pypdf import PdfReader

    reader = PdfReader(BytesIO(data), strict=False)
    if reader.is_encrypted and not reader.decrypt(""):
        return {"error": "PDF is encrypted and cannot be parsed"}
    total_pages = len(reader.pages)
    pages, remaining = [], MAX_PDF_TEXT_CHARS
    truncated = total_pages > MAX_PDF_PAGES
    for index in range(min(total_pages, MAX_PDF_PAGES)):
        if remaining == 0:
            truncated = True
            break
        text = (reader.pages[index].extract_text() or "").strip()
        if len(text) > remaining:
            truncated = True
        text = text[:remaining]
        remaining -= len(text)
        pages.append({"page": index + 1, "text": text})
    return {"page_count": total_pages, "parsed_pages": len(pages), "pages": pages,
            "text": "\n\n".join(f"[Page {p['page']}]\n{p['text']}" for p in pages if p["text"]),
            "text_truncated": truncated,
            "has_extractable_text": any(p["text"] for p in pages)}


def main() -> None:
    # These resource limits are enforced on the supported Linux container.
    # macOS development runs still have the parent's subprocess wall timeout.
    if sys.platform.startswith("linux"):
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (128 * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    logging.disable(logging.CRITICAL)
    data = sys.stdin.buffer.read(MAX_PDF_BYTES + 1)
    try:
        if len(data) > MAX_PDF_BYTES or not data.startswith(b"%PDF-"):
            result = {"error": "Attachment is not a supported PDF"}
        else:
            result = extract_pdf(data)
    except Exception:
        # Do not expose PDF metadata, content, or parser internals in errors.
        result = {"error": "PDF is invalid, encrypted, or exceeds parser resource limits"}
    sys.stdout.write(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
