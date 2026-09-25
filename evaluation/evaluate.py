"""Evaluate the PII redactor against hand-labelled and synthetic gold data.

Datasets
--------
1. ``rhp``        Hand-labelled development sample of the Red Herring
                  Prospectus (``gold/rhp_gold.json``; offsets only, no PII
                  text). Rules were tuned on it, so its numbers are optimistic.
   ``rhp_heldout`` Disjoint sample labelled after the rules were frozen
                  (``gold/rhp_heldout_gold.json``); scored once, no tuning.
2. ``synthetic``  Synthetic support-ticket log covering SSN, credit card, IP
                  and date of birth, plus non-PII look-alikes
                  (``synthetic/synthetic_gold.json``).
3. ``image``      Boxes on the synthetic ID-card image: is every PII box
                  blacked out, and is every non-PII box left readable?
4. ``leak check`` Every gold PII value of the RHP sample is searched for in
                  the *whole* redacted document text.

Metrics
-------
Token level (a token is a run of word characters, so an e-mail or a phone
number group is one token). A token is PII if any gold span covers it.
* "redaction" metrics ignore the entity type: did a PII token get redacted?
  This is what matters for privacy. Accuracy counts correct PII and
  correct non-PII tokens.
* per-type metrics require the predicted type to equal the gold type.
Entity level: a gold entity is "fully redacted" if all of its tokens are
covered by predictions (of any type), "partially" if only some are.

Usage
-----
    python -m evaluation.evaluate --rhp data/Red_Herring_Prospectus.docx
    python -m evaluation.evaluate --rhp data/Red_Herring_Prospectus.docx --redacted output/Red_Herring_Prospectus_redacted.docx

Writes ``evaluation/results.json`` and ``evaluation/REPORT.md``.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import docx
import numpy as np
from PIL import Image

from pii_redactor.config import INDIAN_STATES, ORG_KEYWORDS, RedactionConfig
from pii_redactor.docx_text import iter_text_units
from pii_redactor.pipeline import DocxRedactor
from pii_redactor.postprocess import GENERIC_WORDS

HERE = Path(__file__).parent
TOKEN_RE = re.compile(r"\w+", re.UNICODE)
TYPES = ["PERSON", "ORGANIZATION", "EMAIL_ADDRESS", "PHONE_NUMBER", "ADDRESS", "URL", "IN_DIN",
         "US_SSN", "CREDIT_CARD", "DATE_OF_BIRTH", "IP_ADDRESS", "IN_PAN", "IN_AADHAAR"]


@dataclass
class Counts:
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0

    def add(self, other: "Counts") -> None:
        self.tp += other.tp
        self.fp += other.fp
        self.fn += other.fn
        self.tn += other.tn

    def as_dict(self) -> dict:
        p = self.tp / (self.tp + self.fp) if self.tp + self.fp else None
        r = self.tp / (self.tp + self.fn) if self.tp + self.fn else None
        f1 = 2 * p * r / (p + r) if p and r else (0.0 if p is not None and r is not None else None)
        total = self.tp + self.fp + self.fn + self.tn
        acc = (self.tp + self.tn) / total if total else None
        return {"tp": self.tp, "fp": self.fp, "fn": self.fn, "tn": self.tn,
                "precision": _r(p), "recall": _r(r), "f1": _r(f1), "accuracy": _r(acc)}


def _r(x):
    return None if x is None else round(x, 4)


@dataclass
class Result:
    name: str
    redaction: Counts = field(default_factory=Counts)
    by_type: dict[str, Counts] = field(default_factory=lambda: defaultdict(Counts))
    entities: Counter = field(default_factory=Counter)  # (type, full|partial|missed)
    false_positives: list[tuple[str, str]] = field(default_factory=list)
    misses: list[tuple[str, str]] = field(default_factory=list)
    units: int = 0


def _token_labels(text: str, spans) -> list[str | None]:
    labels = []
    for m in TOKEN_RE.finditer(text):
        label = None
        for s, e, t in spans:
            if m.start() < e and s < m.end():
                label = t
                break
        labels.append(label)
    return labels


def score_unit(res: Result, text: str, gold: list, pred: list, mask_misses: bool) -> None:
    res.units += 1
    g_labels, p_labels = _token_labels(text, gold), _token_labels(text, pred)
    for g, p in zip(g_labels, p_labels):
        c = res.redaction
        if g and p:
            c.tp += 1
        elif p:
            c.fp += 1
        elif g:
            c.fn += 1
        else:
            c.tn += 1
        for t in {g, p} - {None}:
            bt = res.by_type[t]
            if g == t and p == t:
                bt.tp += 1
            elif p == t:
                bt.fp += 1
            elif g == t:
                bt.fn += 1
    tokens = list(TOKEN_RE.finditer(text))
    for s, e, t in gold:
        idx = [i for i, m in enumerate(tokens) if m.start() < e and s < m.end()]
        covered = sum(1 for i in idx if p_labels[i])
        status = "full" if idx and covered == len(idx) else "partial" if covered else "missed"
        res.entities[(t, status)] += 1
        if status != "full":
            value = text[s:e]
            res.misses.append((t, f"{status}: {_mask(value) if mask_misses else value}"))
    for s, e, t in pred:
        if not any(s < ge and gs < e for gs, ge, _ in gold):
            res.false_positives.append((t, re.sub(r"\s+", " ", text[s:e])))


def _mask(value: str) -> str:
    """Hide real PII in reports: 'Nuvama' -> 'N****a'."""
    return re.sub(r"\w+", lambda m: m.group(0)[0] + "*" * max(0, len(m.group(0)) - 2) + m.group(0)[-1]
                  if len(m.group(0)) > 2 else "*" * len(m.group(0)), value)


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def evaluate_rhp(detections, gold_file: str, name: str) -> tuple[Result, list[tuple[str, str]]]:
    gold = json.loads((HERE / "gold" / gold_file).read_text(encoding="utf-8"))
    res = Result(name)
    gold_values = []
    for g in gold["units"]:
        unit = detections.units[g["index"]]
        if _sha1(unit.text) != g["sha1"]:
            raise SystemExit(f"Unit {g['index']} does not match the gold file; is this the original RHP?")
        pred = [(s.start, s.end, s.entity_type) for s in detections.spans[g["index"]]]
        spans = [tuple(sp) for sp in g["spans"]]
        score_unit(res, unit.text, spans, pred, mask_misses=True)
        gold_values += [(t, unit.text[s:e]) for s, e, t in spans]
    return res, gold_values


def evaluate_synthetic(redactor: DocxRedactor, docx_path: Path) -> Result:
    gold = json.loads((HERE / "synthetic" / "synthetic_gold.json").read_text(encoding="utf-8"))
    detections = redactor.detect_document(docx.Document(str(docx_path)))
    by_hash = defaultdict(list)
    for unit, spans in zip(detections.units, detections.spans):
        by_hash[_sha1(unit.text)].append((unit, spans))
    res = Result("synthetic")
    for g in gold["units"]:
        matches = by_hash.get(g["sha1"])
        if not matches:
            raise SystemExit(f"Synthetic unit not found in document: {g['text'][:60]!r}")
        unit, spans = matches.pop(0)
        pred = [(s.start, s.end, s.entity_type) for s in spans]
        score_unit(res, unit.text, [(e["start"], e["end"], e["type"]) for e in g["entities"]], pred,
                   mask_misses=False)
    return res


def evaluate_image(redactor: DocxRedactor, docx_path: Path) -> dict:
    gold = json.loads((HERE / "synthetic" / "synthetic_gold.json").read_text(encoding="utf-8"))["image"]
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out.docx"
        redactor.redact(docx_path, out)
        doc = docx.Document(str(out))
        images = [p for p in doc.part.package.iter_parts() if p.content_type.startswith("image/")]
        img = np.asarray(Image.open(io.BytesIO(images[gold["index"]].blob)).convert("RGB"))
    if img.shape[1] != gold["size"][0] or img.shape[0] != gold["size"][1]:
        raise SystemExit("Redacted image size changed; box comparison would be invalid.")
    black = np.all(img < 40, axis=2)
    rows = []
    for b in gold["boxes"]:
        l, t, r, bt = (int(round(v)) for v in b["box"])
        coverage = float(black[t:bt, l:r].mean())
        rows.append({"text": b["text"], "pii": b["pii"], "black_coverage": round(coverage, 3)})
    pii = [r for r in rows if r["pii"]]
    non = [r for r in rows if not r["pii"]]
    hidden = [r for r in pii if r["black_coverage"] >= 0.9]
    wrongly = [r for r in non if r["black_coverage"] >= 0.5]
    return {
        "pii_boxes": len(pii), "pii_boxes_redacted": len(hidden),
        "box_recall": _r(len(hidden) / len(pii)) if pii else None,
        "non_pii_boxes": len(non), "non_pii_boxes_redacted": len(wrongly),
        "box_precision": _r(len(hidden) / (len(hidden) + len(wrongly))) if hidden or wrongly else None,
        "boxes": rows,
    }


def leak_check(redacted_path: Path, gold_values: list[tuple[str, str]]) -> dict:
    doc = docx.Document(str(redacted_path))
    full = " ".join(u.text for u in iter_text_units(doc))
    norm = re.sub(r"\s+", " ", full).casefold()
    generic = GENERIC_WORDS | ORG_KEYWORDS | {w.casefold() for s in INDIAN_STATES for w in s.split()} | {"family"}
    leaked = Counter()
    examples = []
    checked = set()
    for t, v in gold_values:
        key = re.sub(r"\s+", " ", v).strip().casefold()
        if (t, key) in checked or len(key) < 4:
            continue
        if all(tok in generic for tok in TOKEN_RE.findall(key)):
            continue  # "FAMILY TRUST", "Maharashtra, India": also legitimately present in fakes/kept text
        checked.add((t, key))
        if re.search(r"(?<!\w)" + re.escape(key) + r"(?!\w)", norm):
            leaked[t] += 1
            examples.append(f"{t}: {_mask(v)}")
    return {"unique_gold_values_checked": len(checked), "leaked_by_type": dict(leaked),
            "leaked_total": sum(leaked.values()), "leaked_values_masked": examples}


def summarise(res: Result) -> dict:
    types = sorted(set(res.by_type) | {t for t, _ in res.entities})
    entity = {}
    for t in types:
        n = {s: res.entities[(t, s)] for s in ("full", "partial", "missed")}
        total = sum(n.values())
        entity[t] = {**n, "total": total, "full_recall": _r(n["full"] / total) if total else None}
    tot = {s: sum(res.entities[(t, s)] for t in types) for s in ("full", "partial", "missed")}
    total = sum(tot.values())
    entity["ALL"] = {**tot, "total": total, "full_recall": _r(tot["full"] / total) if total else None}
    return {
        "units": res.units,
        "token_redaction": res.redaction.as_dict(),
        "token_by_type": {t: res.by_type[t].as_dict() for t in types if t in res.by_type},
        "entity": entity,
        "false_positives": sorted(Counter(res.false_positives).items(), key=lambda kv: -kv[1]),
        "misses": sorted(Counter(res.misses).items(), key=lambda kv: -kv[1]),
    }


def _fmt(x) -> str:
    return "–" if x is None else f"{x:.3f}"


def write_report(results: dict, path: Path) -> None:
    lines = ["# Evaluation report", "",
             "Generated by `python -m evaluation.evaluate`. Method: see `evaluation/evaluate.py` and the README.", ""]
    for name, title, note in (
            ("rhp", "Red Herring Prospectus – development sample",
             "Development set: the rules were tuned while looking at these units, so these numbers are optimistic."),
            ("rhp_heldout", "Red Herring Prospectus – held-out sample",
             "Held-out set: labelled after the rules were frozen, scored once, no tuning afterwards."),
            ("synthetic", "Synthetic support-ticket log (SSN / card / IP / DOB)",
             "SYNTHETIC data generated by `evaluation/make_synthetic.py` (covers PII types absent from the RHP).")):
        if name not in results:
            continue
        r = results[name]
        red = r["token_redaction"]
        lines += [f"## {title}", "", note, "", f"Units evaluated: {r['units']}", "",
                  "### Redaction (type-agnostic, token level)", "",
                  "| Precision | Recall | F1 | Accuracy | TP | FP | FN | TN |", "|---|---|---|---|---|---|---|---|",
                  f"| {_fmt(red['precision'])} | {_fmt(red['recall'])} | {_fmt(red['f1'])} | {_fmt(red['accuracy'])} "
                  f"| {red['tp']} | {red['fp']} | {red['fn']} | {red['tn']} |", "",
                  "### Per entity type (token level, type must match)", "",
                  "| Type | Precision | Recall | F1 | TP | FP | FN |", "|---|---|---|---|---|---|---|"]
        for t, c in r["token_by_type"].items():
            lines.append(f"| {t} | {_fmt(c['precision'])} | {_fmt(c['recall'])} | {_fmt(c['f1'])} "
                         f"| {c['tp']} | {c['fp']} | {c['fn']} |")
        lines += ["", "### Entity level (gold entity fully / partially / not redacted)", "",
                  "| Type | Gold | Fully | Partially | Missed | Full recall |", "|---|---|---|---|---|---|"]
        for t, c in r["entity"].items():
            lines.append(f"| {t} | {c['total']} | {c['full']} | {c['partial']} | {c['missed']} | {_fmt(c['full_recall'])} |")
        lines += ["", "False positives (predicted span with no gold overlap):", ""]
        lines += [f"- {t}: `{v}` ×{n}" for (t, v), n in r["false_positives"]] or ["- none"]
        lines += ["", "Missed / partial gold entities" + (" (values masked)" if name.startswith("rhp") else "") + ":", ""]
        lines += [f"- {t}: `{v}` ×{n}" for (t, v), n in r["misses"]] or ["- none"]
        lines.append("")
    if "image" in results:
        im = results["image"]
        lines += ["## Synthetic ID-card image", "",
                  f"PII boxes blacked out (≥90% black pixels): {im['pii_boxes_redacted']}/{im['pii_boxes']} "
                  f"(recall {_fmt(im['box_recall'])}); non-PII boxes wrongly blacked (≥50%): "
                  f"{im['non_pii_boxes_redacted']}/{im['non_pii_boxes']} (precision {_fmt(im['box_precision'])}).", "",
                  "| Text | PII | Black coverage |", "|---|---|---|"]
        lines += [f"| {b['text']} | {'yes' if b['pii'] else 'no'} | {b['black_coverage']:.2f} |" for b in im["boxes"]]
        lines.append("")
    for key, label in (("leak_check", "development"), ("leak_check_heldout", "held-out")):
        if key not in results:
            continue
        lc = results[key]
        lines += [f"## Leak check on the redacted prospectus ({label} gold values)", "",
                  f"{lc['unique_gold_values_checked']} unique gold PII values from the sample were searched for in the "
                  f"full text of the redacted document: {lc['leaked_total']} found "
                  f"({', '.join(f'{k}: {v}' for k, v in lc['leaked_by_type'].items()) or 'none'}).", ""]
        lines += [f"- {v}" for v in lc["leaked_values_masked"]]
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rhp", type=Path, help="original Red_Herring_Prospectus.docx (not in the repository)")
    ap.add_argument("--redacted", type=Path, help="redacted RHP for the leak check")
    ap.add_argument("--synthetic", type=Path, default=HERE / "synthetic" / "synthetic_ticket_log.docx")
    ap.add_argument("--no-image", action="store_true", help="skip the OCR image evaluation")
    ap.add_argument("--out", type=Path, default=HERE)
    args = ap.parse_args(argv)

    redactor = DocxRedactor(RedactionConfig())
    results: dict = {}
    if args.rhp:
        detections = redactor.detect_document(docx.Document(str(args.rhp)))
        res, gold_values = evaluate_rhp(detections, "rhp_gold.json", "rhp")
        results["rhp"] = summarise(res)
        held, held_values = evaluate_rhp(detections, "rhp_heldout_gold.json", "rhp_heldout")
        results["rhp_heldout"] = summarise(held)
        if args.redacted:
            results["leak_check"] = leak_check(args.redacted, gold_values)
            results["leak_check_heldout"] = leak_check(args.redacted, held_values)
    results["synthetic"] = summarise(evaluate_synthetic(redactor, args.synthetic))
    if not args.no_image:
        results["image"] = evaluate_image(redactor, args.synthetic)

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "results.json").write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    write_report(results, args.out / "REPORT.md")
    for name in ("rhp", "rhp_heldout", "synthetic"):
        if name in results:
            red = results[name]["token_redaction"]
            print(f"{name:11s} precision={_fmt(red['precision'])} recall={_fmt(red['recall'])} "
                  f"f1={_fmt(red['f1'])} accuracy={_fmt(red['accuracy'])} "
                  f"entity_full_recall={_fmt(results[name]['entity']['ALL']['full_recall'])}")
    if "image" in results:
        print(f"image      box_recall={_fmt(results['image']['box_recall'])} "
              f"box_precision={_fmt(results['image']['box_precision'])}")
    for key in ("leak_check", "leak_check_heldout"):
        if key in results:
            print(f"{key:18s} leaked={results[key]['leaked_total']}/{results[key]['unique_gold_values_checked']}")
    print(f"Report: {args.out / 'REPORT.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
