from dataclasses import dataclass
from io import BytesIO

import fitz
import pytesseract
from PIL import Image

MIN_TEXT_CHARS = 40
MIN_OCR_CONFIDENCE = 45.0


@dataclass(frozen=True)
class ParseResult:
    text: str
    quality: str
    pages: int
    used_ocr: bool
    ocr_confidence: float | None


def parse_pdf(payload: bytes) -> ParseResult:
    document = fitz.open(stream=payload, filetype="pdf")
    try:
        chunks: list[str] = []
        used_ocr = False
        confidences: list[float] = []
        for page in document:
            text = page.get_text("text").strip()
            if _alnum_count(text) < MIN_TEXT_CHARS:
                used_ocr = True
                ocr_text, confidence = _ocr_page(page)
                confidences.append(confidence)
                text = ocr_text.strip()
            chunks.append(text)
        combined = "\n".join(chunk for chunk in chunks if chunk).strip()
        confidence = sum(confidences) / len(confidences) if confidences else None
        return ParseResult(
            text=combined,
            quality=_quality(combined, used_ocr, confidence),
            pages=document.page_count,
            used_ocr=used_ocr,
            ocr_confidence=confidence,
        )
    finally:
        document.close()


def _ocr_page(page: fitz.Page) -> tuple[str, float]:
    pixmap = page.get_pixmap(dpi=200)
    image = Image.open(BytesIO(pixmap.tobytes("png")))
    data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)
    words: list[str] = []
    scores: list[float] = []
    for word, score in zip(data["text"], data["conf"], strict=True):
        if not str(word).strip():
            continue
        try:
            numeric = float(score)
        except ValueError:
            continue
        if numeric < 0:
            continue
        words.append(str(word))
        scores.append(numeric)
    confidence = sum(scores) / len(scores) if scores else 0.0
    return " ".join(words), confidence


def _quality(text: str, used_ocr: bool, confidence: float | None) -> str:
    if _alnum_count(text) < MIN_TEXT_CHARS:
        return "poor"
    if used_ocr and (confidence is None or confidence < MIN_OCR_CONFIDENCE):
        return "poor"
    if used_ocr:
        return "ocr"
    return "text"


def _alnum_count(text: str) -> int:
    return sum(1 for char in text if char.isalnum())
