"""In-place redaction of PII inside raster images (scanned ID cards, photos).

Pipeline per image:
1. Pillow load -> grayscale -> autocontrast/contrast/sharpen -> upscale
   (Tesseract is most accurate on ~300 DPI text, so small scans are enlarged).
2. ``pytesseract.image_to_data`` -> pandas DataFrame of words with boxes.
3. Words are grouped into visual lines; each line goes through the same
   Presidio detector used for document text (with all OCR words passed as
   context so "Aadhaar"/"DOB" labels anywhere on the card boost scores).
4. For identity documents, label/value rules cover fields NER cannot read
   reliably (e.g. the value printed under "Name" or after "Father :").
5. Faces (two Haar cascades must agree) and QR codes (decoded; non-URL
   payloads or undecodable codes on ID cards) are located with OpenCV.
6. Solid black rectangles are drawn on the *original* pixels; the image is
   re-encoded in its original format and dimensions, so the layout of the
   Word document does not change.
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field
from typing import Callable, Iterable

import numpy as np
import pandas as pd
import pytesseract
from PIL import Image, ImageDraw, ImageEnhance, ImageOps

from .analyzer import PIIDetector
from .config import RedactionConfig

log = logging.getLogger(__name__)

KnownMatcher = Callable[[str], list[tuple[int, int, str]]]

ID_DOCUMENT_CUES = (
    "income tax", "permanent account", "aadhaar", "aadhar", "unique identification", "government of india",
    "govt. of india", "आधार", "भारत सरकार", "आयकर", "date of birth", "dob", "election commission",
    "driving licence", "passport", "voter",
)
NAME_LABELS = {"name", "नाम"}
FATHER_LABELS = {"father", "father's", "fathers", "पिता", "s/o", "d/o", "w/o"}
DOB_LABELS = {"dob", "birth", "जन्म", "yob"}
ADDRESS_LABELS = {"address", "पता"}
SIGNATURE_LABELS = {"signature", "हस्ताक्षर"}
ALL_LABELS = NAME_LABELS | FATHER_LABELS | DOB_LABELS | ADDRESS_LABELS | SIGNATURE_LABELS | {
    "date", "of", "the", "की", "तारीख", "का", "तिथि", "/", "|", ":", "-",
}
_DEVANAGARI = re.compile(r"[\u0900-\u097F]")
_PIN = re.compile(r"(?<!\d)\d{6}(?!\d)|(?<!\d)\d{3}\s\d{3}(?!\d)")


@dataclass
class Word:
    text: str
    left: int
    top: int
    width: int
    height: int
    conf: float

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height

    @property
    def key(self) -> str:
        return self.text.casefold().strip(":;,.|/ःॱ")


@dataclass
class Line:
    words: list[Word] = field(default_factory=list)

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    def offsets(self) -> list[tuple[int, int]]:
        out, pos = [], 0
        for w in self.words:
            out.append((pos, pos + len(w.text)))
            pos += len(w.text) + 1
        return out

    @property
    def top(self) -> int:
        return min(w.top for w in self.words)

    @property
    def bottom(self) -> int:
        return max(w.bottom for w in self.words)

    @property
    def left(self) -> int:
        return min(w.left for w in self.words)

    @property
    def right(self) -> int:
        return max(w.right for w in self.words)

    @property
    def height(self) -> int:
        return int(np.median([w.height for w in self.words]))


@dataclass
class Box:
    entity_type: str
    left: int
    top: int
    right: int
    bottom: int
    source: str

    def as_dict(self) -> dict:
        return {
            "type": self.entity_type, "source": self.source,
            "bbox": [int(self.left), int(self.top), int(self.right - self.left), int(self.bottom - self.top)],
        }


def _union(words: Iterable[Word], entity_type: str, source: str) -> Box | None:
    words = list(words)
    if not words:
        return None
    return Box(entity_type, min(w.left for w in words), min(w.top for w in words),
               max(w.right for w in words), max(w.bottom for w in words), source)


def resolve_ocr_lang(requested: str) -> str:
    if requested != "auto":
        return requested
    try:
        langs = set(pytesseract.get_languages(config=""))
    except Exception:  # pragma: no cover - depends on local install
        return "eng"
    return "eng+hin" if "hin" in langs else "eng"


class ImageRedactor:
    def __init__(self, detector: PIIDetector, config: RedactionConfig, known_matcher: KnownMatcher | None = None):
        self.detector = detector
        self.config = config
        self.known_matcher = known_matcher or (lambda text: [])
        if config.tesseract_cmd:
            pytesseract.pytesseract.tesseract_cmd = config.tesseract_cmd
        self.ocr_lang = resolve_ocr_lang(config.ocr_lang)

    # ------------------------------------------------------------------ API
    def redact(self, blob: bytes) -> tuple[bytes | None, dict]:
        """Return (new image bytes or None if unchanged, report)."""
        try:
            image = Image.open(io.BytesIO(blob))
            image.load()
        except Exception as exc:
            return None, {"skipped": f"unreadable image ({exc.__class__.__name__})"}
        fmt = (image.format or "PNG").upper()
        if fmt not in {"PNG", "JPEG", "BMP", "GIF", "TIFF", "WEBP"}:
            return None, {"skipped": f"unsupported format {fmt}"}

        rgb = image.convert("RGB")
        words = self.ocr_words(rgb)
        lines = group_lines(words)
        full_text = " ".join(w.text for w in words)
        is_id = any(cue in full_text.casefold() for cue in ID_DOCUMENT_CUES)

        boxes: list[Box] = []
        boxes += self._text_pii_boxes(lines, words, use_ner=not is_id)
        if is_id:
            boxes += self._id_card_rule_boxes(lines, rgb.size)
        if self.config.redact_faces:
            boxes += detect_faces(rgb)
        if self.config.redact_qr_codes:
            boxes += detect_qr_codes(rgb, texture_fallback=is_id)

        report = {
            "format": fmt, "size": list(image.size), "ocr_lang": self.ocr_lang, "ocr_words": len(words),
            "is_id_document": is_id, "redactions": [b.as_dict() for b in boxes],
        }
        if not boxes:
            return None, report
        return self._paint(image, fmt, boxes), report

    # ------------------------------------------------------------ OCR stage
    @staticmethod
    def preprocess(rgb: Image.Image) -> tuple[Image.Image, float]:
        gray = ImageOps.grayscale(rgb)
        gray = ImageOps.autocontrast(gray, cutoff=1)
        gray = ImageEnhance.Contrast(gray).enhance(1.8)
        gray = ImageEnhance.Sharpness(gray).enhance(1.5)
        scale = min(4.0, max(1.0, 1600 / max(gray.size)))
        if scale > 1.0:
            gray = gray.resize((int(gray.width * scale), int(gray.height * scale)), Image.LANCZOS)
        return gray, scale

    def ocr_words(self, rgb: Image.Image) -> list[Word]:
        processed, scale = self.preprocess(rgb)
        df: pd.DataFrame = pytesseract.image_to_data(
            processed, lang=self.ocr_lang, config="--oem 3 --psm 11", output_type=pytesseract.Output.DATAFRAME
        )
        df = df.dropna(subset=["text"])
        df["text"] = df["text"].astype(str).str.strip()
        df = df[(df["conf"] >= self.config.ocr_min_confidence) & (df["text"] != "")]
        df = df[df["text"].str.contains(r"[\w\u0900-\u097F]", regex=True)]
        return [
            Word(
                text=row.text,
                left=int(row.left / scale), top=int(row.top / scale),
                width=max(1, int(row.width / scale)), height=max(1, int(row.height / scale)),
                conf=float(row.conf),
            )
            for row in df.itertuples()
        ]

    # ------------------------------------------------------- text PII stage
    def _text_pii_boxes(self, lines: list[Line], words: list[Word], use_ner: bool) -> list[Box]:
        """Presidio on each OCR line.

        On ID documents spaCy NER is skipped: OCR noise ("Mode! Coleay") is
        often tagged as PERSON, while the card's fixed layout is handled more
        reliably by the label rules. Pattern recognizers (PAN, Aadhaar, DOB,
        phone, e-mail) still run everywhere.
        """
        context = tuple(dict.fromkeys(w.key for w in words if len(w.key) > 1))[:300]
        boxes = []
        for line in lines:
            text, offsets = line.text, line.offsets()
            hits = [
                (s.start, s.end, s.entity_type, "presidio")
                for s in self.detector.detect(text, context)
                if use_ner or s.source != "SpacyRecognizer"
            ]
            hits += [(s, e, t, "known_entity") for s, e, t in self.known_matcher(text)]
            for start, end, etype, source in hits:
                covered = [w for w, (ws, we) in zip(line.words, offsets) if ws < end and start < we]
                box = _union(covered, etype, source)
                if box:
                    boxes.append(box)
        return boxes

    # -------------------------------------------------- ID-card rule stage
    def _id_card_rule_boxes(self, lines: list[Line], size: tuple[int, int]) -> list[Box]:
        boxes: list[Box] = []
        person_lines: list[Line] = []
        for li, line in enumerate(lines):
            keys = [w.key for w in line.words]
            has_father = any(k in FATHER_LABELS for k in keys)
            for wi, word in enumerate(line.words):
                k = word.key
                if k in FATHER_LABELS or (k in NAME_LABELS and not has_father):
                    value = self._value_for_label(lines, li, wi)
                    if value:
                        boxes.append(_union(value, "PERSON", "id_label"))
                        person_lines.append(Line(value))
                    if len(line.words) == 1 and _DEVANAGARI.search(word.text):
                        # Lone Devanagari label ("पिता :") whose value OCR could not read.
                        h = word.height
                        boxes.append(Box("PERSON", word.right, word.top, min(size[0], word.right + 8 * h),
                                         word.bottom, "id_label_blind"))
                    if k in FATHER_LABELS:
                        holder = self._holder_name_above(lines, li)
                        if holder:
                            boxes.append(_union(holder.words, "PERSON", "id_holder"))
                            person_lines.append(holder)
                elif k in DOB_LABELS:
                    value = self._value_for_label(lines, li, wi)
                    if value:
                        boxes.append(_union(value, "DATE_OF_BIRTH", "id_label"))
                elif k in ADDRESS_LABELS:
                    region = self._address_region(lines, word, size[0])
                    if region:
                        boxes.append(_union(region, "ADDRESS", "id_label"))
                elif k in SIGNATURE_LABELS:
                    h = word.height
                    boxes.append(Box("SIGNATURE", max(0, word.left - word.width), max(0, word.top - 3 * h),
                                     min(size[0], word.right + int(1.5 * word.width)), word.top, "id_label"))
        for pl in person_lines:
            twin = self._devanagari_twin(lines, pl)
            if twin:
                boxes.append(_union(twin, "PERSON", "id_script_twin"))
        return [b for b in boxes if b]

    @staticmethod
    def _value_for_label(lines: list[Line], li: int, wi: int) -> list[Word]:
        line, label = lines[li], lines[li].words[wi]
        h = label.height
        same, prev_right = [], label.right
        for w in line.words[wi + 1:]:
            if w.left - prev_right > 4 * h:
                break
            if w.key in ALL_LABELS or not w.key:
                if same:
                    break
                prev_right = w.right
                continue
            if w.conf < 50:
                break
            same.append(w)
            prev_right = w.right
        if same:
            return same
        below = [
            ln for ln in lines[li + 1:]
            if 0 <= ln.top - label.bottom <= 3 * h and ln.right > label.left - 3 * h and ln.left < label.right + 12 * h
        ]
        if not below:
            return []
        nearest = min(below, key=lambda ln: ln.top)
        return [w for w in nearest.words if w.key not in ALL_LABELS and w.right > label.left - 3 * h]

    @staticmethod
    def _holder_name_above(lines: list[Line], li: int) -> Line | None:
        label_line = lines[li]
        h = label_line.height
        above = [
            ln for ln in lines[:li]
            if 0 <= label_line.top - ln.bottom <= 3 * h and ln.right > label_line.left - 2 * h
            and all(re.fullmatch(r"[A-Za-z.'’-]+", w.text) for w in ln.words)
            and not any(w.key in ALL_LABELS for w in ln.words)
        ]
        return max(above, key=lambda ln: ln.bottom) if above else None

    @staticmethod
    def _address_region(lines: list[Line], label: Word, width: int) -> list[Word]:
        """Words of the address block that starts at ``label``, bounded by its column.

        Works on words rather than lines because OCR row detection is uneven
        for Devanagari. The union box of the returned words also covers
        characters OCR failed to read inside the block.
        """
        h = label.height
        words = [w for ln in lines for w in ln.words]
        same_row = [w for w in words if w is not label and abs((w.top + w.bottom) / 2 - (label.top + label.bottom) / 2) < 1.5 * h]
        other_labels = [w for w in same_row if w.key in ADDRESS_LABELS and w.left > label.right]
        col_left = label.left - h
        col_right = min((w.left for w in other_labels), default=width) - 1
        in_col = [w for w in words if w is not label and w.left >= col_left and w.right <= col_right + h
                  and w.top >= label.top - 1.5 * h]
        pins = [w for w in in_col if _PIN.search(w.text) and w.top <= label.top + 10 * h]
        limit = min((w.bottom for w in pins), default=label.bottom + 5 * h)
        return [w for w in in_col if w.top <= limit and not (w.key in ADDRESS_LABELS and w.right <= label.right + h)]

    @staticmethod
    def _devanagari_twin(lines: list[Line], person: Line) -> list[Word]:
        """Indian ID cards print the name in Devanagari directly above the English one."""
        h = person.height
        twins = []
        for ln in lines:
            if 0 <= person.top - ln.bottom <= 2.5 * h:
                twins += [w for w in ln.words if _DEVANAGARI.search(w.text)
                          and w.right > person.left - 2 * h and w.left < person.right + 2 * h
                          and w.key not in ALL_LABELS]
        return twins

    # ---------------------------------------------------------- paint stage
    def _paint(self, image: Image.Image, fmt: str, boxes: list[Box]) -> bytes:
        has_alpha = image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info)
        canvas = image.convert("RGBA" if has_alpha else "RGB")
        draw = ImageDraw.Draw(canvas)
        pad = self.config.box_padding
        fill = (0, 0, 0, 255) if has_alpha else (0, 0, 0)
        for b in boxes:
            draw.rectangle(
                [max(0, b.left - pad), max(0, b.top - pad),
                 min(canvas.width - 1, b.right + pad), min(canvas.height - 1, b.bottom + pad)],
                fill=fill,
            )
        out = io.BytesIO()
        params: dict = {}
        if "dpi" in image.info:
            params["dpi"] = image.info["dpi"]
        if fmt == "JPEG":
            canvas = canvas.convert("RGB")
            params.update(quality=95, subsampling=0)
        elif fmt == "GIF":
            canvas = canvas.convert("P")
        canvas.save(out, format=fmt, **params)
        return out.getvalue()


def group_lines(words: list[Word]) -> list[Line]:
    """Cluster OCR words into visual lines: rows by vertical overlap, then split at large gaps.

    A row is represented by the median top/bottom of its words, and a word
    joins the row it overlaps most (by at least half the larger height).
    Implausibly tall "words" (OCR noise spanning a whole card) are dropped
    first, otherwise they would glue every row together.
    """
    if not words:
        return []
    typical = float(np.median([w.height for w in words]))
    words = [w for w in words if w.height <= 4 * typical]
    rows: list[list[Word]] = []
    for w in sorted(words, key=lambda w: (w.top + w.bottom) / 2):
        best, best_ratio = None, 0.5
        for row in rows:
            top = float(np.median([x.top for x in row]))
            bottom = float(np.median([x.bottom for x in row]))
            ratio = (min(bottom, w.bottom) - max(top, w.top)) / max(w.height, bottom - top, 1)
            if ratio >= best_ratio:
                best, best_ratio = row, ratio
        if best is None:
            rows.append([w])
        else:
            best.append(w)
    lines: list[Line] = []
    for row in rows:
        row.sort(key=lambda x: x.left)
        current = Line([row[0]])
        for w in row[1:]:
            last = current.words[-1]
            if w.left - last.right > 2.5 * max(w.height, current.height):
                lines.append(current)
                current = Line([w])
            else:
                current.words.append(w)
        lines.append(current)
    return sorted(lines, key=lambda ln: (ln.top, ln.left))


# ----------------------------------------------------------------- OpenCV
def detect_faces(rgb: Image.Image) -> list[Box]:
    """Faces/ID photos. Two Haar cascades must agree to limit false positives."""
    import cv2

    if min(rgb.size) < 100:
        return []
    gray = cv2.cvtColor(np.asarray(rgb), cv2.COLOR_RGB2GRAY)
    gray = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
    root = cv2.data.haarcascades
    a = cv2.CascadeClassifier(root + "haarcascade_frontalface_default.xml").detectMultiScale(gray, 1.05, 5, minSize=(30, 30))
    b = cv2.CascadeClassifier(root + "haarcascade_frontalface_alt.xml").detectMultiScale(gray, 1.05, 5, minSize=(30, 30))
    boxes = []
    for (x, y, w, h) in a:
        agrees = any(_iou((x, y, w, h), tuple(o)) > 0.4 for o in b)
        if not agrees:
            continue
        # Expand the face box to cover the whole ID photograph (hair, neck, shoulders).
        ex, ey = int(0.6 * w), int(0.7 * h)
        boxes.append(Box("FACE", max(0, x - ex), max(0, y - ey), min(rgb.width, x + w + ex),
                         min(rgb.height, y + h + int(1.0 * h)), "opencv_haar"))
    return boxes


def _iou(a, b) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    return inter / float(aw * ah + bw * bh - inter) if inter else 0.0


def detect_qr_codes(rgb: Image.Image, texture_fallback: bool) -> list[Box]:
    """QR codes: keep those that decode to a public URL, redact the rest.

    The QR on Indian ID cards encodes the holder's details but is usually too
    degraded in scans to decode, so on ID documents a texture detector (dense
    black/white module transitions in a square region) is used as fallback.
    """
    import cv2

    arr = np.asarray(rgb)
    boxes, keep_regions = [], []
    ok, decoded, points, _ = cv2.QRCodeDetector().detectAndDecodeMulti(arr)
    if ok and points is not None:
        for payload, pts in zip(decoded, points):
            x, y, w, h = cv2.boundingRect(pts.astype(np.int32))
            if re.match(r"(?i)^(https?://|www\.)", payload or ""):
                keep_regions.append((x, y, w, h))
            else:
                boxes.append(Box("QR_CODE", x, y, x + w, y + h, "opencv_qr"))
    if texture_fallback:
        for (x, y, w, h) in _qr_texture_regions(arr):
            if not any(_iou((x, y, w, h), k) > 0.2 for k in keep_regions) and not any(
                _iou((x, y, w, h), (b.left, b.top, b.right - b.left, b.bottom - b.top)) > 0.2 for b in boxes
            ):
                boxes.append(Box("QR_CODE", x, y, x + w, y + h, "qr_texture"))
    return boxes


def _qr_texture_regions(arr: np.ndarray) -> list[tuple[int, int, int, int]]:
    import cv2

    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    binary = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 21, 8)
    transitions = np.zeros(gray.shape, np.float32)
    transitions[:, 1:] += binary[:, 1:] != binary[:, :-1]
    transitions[1:, :] += binary[1:, :] != binary[:-1, :]
    win = max(15, min(gray.shape) // 25)
    density = cv2.blur(transitions, (win, win))
    mask = (density > 0.3).astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (win, win)))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    regions = []
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        fill = cv2.contourArea(c) / float(w * h)
        if w * h > 0.01 * gray.size and 0.7 < w / h < 1.4 and fill > 0.85:
            m = int(0.06 * max(w, h))  # the density mask sits slightly inside the quiet zone
            regions.append((max(0, x - m), max(0, y - m), w + 2 * m, h + 2 * m))
    return regions
