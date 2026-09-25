"""Command line interface: ``python -m pii_redactor INPUT.docx -o OUTPUT.docx``."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .config import RedactionConfig


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pii_redactor",
        description="Redact PII in a .docx in place (text via Presidio + spaCy, images via Tesseract + OpenCV).",
    )
    p.add_argument("input", type=Path, help="input .docx file")
    p.add_argument("-o", "--output", type=Path, help="output .docx (default: <input>_redacted.docx)")
    p.add_argument("--report", type=Path, help="write a JSON run report (contains no original PII)")
    p.add_argument("--mapping", type=Path,
                   help="also write the original->fake mapping (SENSITIVE: contains the original PII)")
    p.add_argument("--seed", type=int, default=42, help="Faker seed for reproducible replacements")
    p.add_argument("--no-images", action="store_true", help="skip OCR/image redaction")
    p.add_argument("--no-faces", action="store_true", help="do not black out detected faces")
    p.add_argument("--no-qr", action="store_true", help="do not black out QR codes")
    p.add_argument("--ocr-lang", default="auto", help='Tesseract languages, e.g. "eng" or "eng+hin" (default: auto)')
    p.add_argument("--tesseract-cmd", help=r'path to tesseract, e.g. "C:\Program Files\Tesseract-OCR\tesseract.exe"')
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")
    for noisy in ("presidio-analyzer", "presidio_analyzer"):
        logging.getLogger(noisy).setLevel(logging.ERROR)

    output = args.output or args.input.with_name(f"{args.input.stem}_redacted.docx")
    if output.resolve() == args.input.resolve():
        print("error: output path must differ from the input path", file=sys.stderr)
        return 2
    config = RedactionConfig(
        seed=args.seed,
        redact_images=not args.no_images,
        redact_faces=not args.no_faces,
        redact_qr_codes=not args.no_qr,
        ocr_lang=args.ocr_lang,
        tesseract_cmd=args.tesseract_cmd,
        mapping_path=args.mapping,
    )
    from .pipeline import DocxRedactor  # heavy import (spaCy) only after argument validation

    try:
        report = DocxRedactor(config).redact(args.input, output, args.report)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    summary = {k: report[k] for k in ("output", "seconds", "text_units", "text_units_with_pii",
                                       "text_redactions_by_type", "unique_values_replaced")}
    summary["images_modified"] = sum(1 for r in report["images"] if r.get("modified"))
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
