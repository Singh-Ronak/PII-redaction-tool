"""Build the SYNTHETIC evaluation document and its gold labels.

The Red Herring Prospectus contains no SSNs, credit-card numbers, IP
addresses or dates of birth, so those PII types are evaluated on this
hand-written, clearly synthetic customer-support log. Every person, company,
number and address below is invented for testing; card numbers are the
standard public test numbers (4111 1111 1111 1111, ...).

Inline markup ``[[TYPE:value]]`` marks gold PII; everything else is gold
non-PII, including deliberate negatives (order/ticket/invoice numbers, dates
that are not birth dates, version strings), which the tool is expected to
KEEP.

Run:  python -m evaluation.make_synthetic
Writes evaluation/synthetic/synthetic_ticket_log.docx and synthetic_gold.json.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
from pathlib import Path

import docx
from docx.shared import Inches, Pt
from PIL import Image, ImageDraw, ImageFont

OUT_DIR = Path(__file__).parent / "synthetic"
DOCX_PATH = OUT_DIR / "synthetic_ticket_log.docx"
GOLD_PATH = OUT_DIR / "synthetic_gold.json"

TITLE = "SYNTHETIC TEST DATA - Customer Support Ticket Log (all people, numbers and addresses are fictitious)"

PARAGRAPHS = [
    "Ticket TCK-104233 was opened by [[PERSON:Rashi Patil]] ([[EMAIL_ADDRESS:rashi.patil@gmail.com]]) on 12 March 2024 "
    "about a failed refund for order ORD-99812-A.",
    "Callback requested on [[PHONE_NUMBER:+91 98765 43210]]; alternate landline [[PHONE_NUMBER:020 2567 8841]].",
    "Customer DOB: [[DATE_OF_BIRTH:14/07/1986]]. Account opened 03/02/2019 at the Baner branch.",
    "Card on file [[CREDIT_CARD:4111 1111 1111 1111]] was declined for invoice INV-2024-00871 (amount Rs. 4,599.00).",
    "The login attempt came from IP [[IP_ADDRESS:203.0.113.45]] and was blocked by rule FW-2231.",
    "Escalated to [[PERSON:Arjun Mehta]] at [[ORGANIZATION:Brightline Logistics Private Limited]] for review.",
    "US customer [[PERSON:Emily Carter]] provided SSN [[US_SSN:536-22-8741]] for identity verification.",
    "Mailing address: [[ADDRESS:742 Evergreen Terrace, Springfield, IL 62704]]. Parcel weight 2.4 kg.",
    "Please ship the replacement router (firmware version 10.4.2) to "
    "[[ADDRESS:Flat 12, Shanti Kunj Society, Karve Road, Pune – 411 038, Maharashtra, India]].",
    "[[PERSON:Meera Iyer]] was born on [[DATE_OF_BIRTH:3 January 1979]] according to the KYC form.",
    "Refund reference RF-5512-3345 was issued on 18/11/2024 and credited within 5 working days.",
    "The mastercard ending [[CREDIT_CARD:5500 0000 0000 0004]] belongs to [[PERSON:Daniel Brooks]], "
    "e-mail [[EMAIL_ADDRESS:dbrooks@example.org]].",
    "Server logs show repeated requests from [[IP_ADDRESS:10.24.8.113]] and [[IP_ADDRESS:172.16.254.7]] between 02:00 and 02:15.",
    "Vendor contact: [[PERSON:Kavita Rao]], [[ORGANIZATION:Suncrest Analytics LLP]], telephone [[PHONE_NUMBER:+91 22 4009 7712]].",
    "Order 4500123456 contained 3 items; tracking number 1Z999AA10123456784 was shared with the courier.",
    "Date of Birth - [[DATE_OF_BIRTH:1990-11-23]]; nationality Indian; preferred language Marathi.",
    "The Amex card [[CREDIT_CARD:3400 000000 00009]] expired in 08/2023 and was replaced.",
    "IPv6 address [[IP_ADDRESS:2001:db8:85a3::8a2e:370:7334]] was whitelisted by the network team.",
    "Our head office is at [[ADDRESS:1600 Amphitheatre Parkway, Mountain View, CA 94043]]; support hours are 9am to 6pm.",
    "SSN [[US_SSN:412-55-0193]] was entered twice by mistake, reported by [[PERSON:Robert Fields]].",
    "Customer [[PERSON:Sanjay Kulkarni]] asked us to update his phone to [[PHONE_NUMBER:9823012345]].",
    "Warranty claim WC-00912 approved for model XR-500; the next service is due on 01/06/2025.",
    "Priority P2 incident INC0012345 affected 1,245 users in the Pune region for 42 minutes.",
    "Chargeback filed with [[ORGANIZATION:Northwind Payments Limited]] for card [[CREDIT_CARD:6011 0000 0000 0004]].",
    "Knowledge-base article KB-2207 explains how to reset the password; see section 4.2.1.",
]

TABLE_HEADER = ["Ticket ID", "Customer", "Email", "Phone", "Date of Birth", "Status"]
TABLE_ROWS = [
    ["TCK-200101", "[[PERSON:Nikhil Deshpande]]", "[[EMAIL_ADDRESS:nikhil.d@example.com]]",
     "[[PHONE_NUMBER:+91 99220 11345]]", "[[DATE_OF_BIRTH:21/09/1992]]", "Resolved"],
    ["TCK-200102", "[[PERSON:Priya Sharma]]", "[[EMAIL_ADDRESS:priya.sharma@example.net]]",
     "[[PHONE_NUMBER:+91 90110 22456]]", "[[DATE_OF_BIRTH:05/12/1988]]", "Open"],
    ["TCK-200103", "[[PERSON:John Mathews]]", "[[EMAIL_ADDRESS:jmathews@example.com]]",
     "[[PHONE_NUMBER:+1 415 555 0132]]", "[[DATE_OF_BIRTH:30/04/1975]]", "Pending"],
    ["TCK-200104", "[[PERSON:Ayesha Khan]]", "[[EMAIL_ADDRESS:ayesha.khan@example.org]]",
     "[[PHONE_NUMBER:+91 88050 33567]]", "[[DATE_OF_BIRTH:17/02/2001]]", "Closed"],
]

# Synthetic identity card image: (text, is_pii). The number is Verhoeff-invalid.
ID_CARD_LINES = [
    ("GOVERNMENT OF INDIA", False),
    ("Name: Rohan Verma", True),
    ("DOB: 11/08/1994", True),
    ("Gender: Male", False),
    ("4821 7730 5129", True),
    ("Address: 14 Lake View Road, Nashik - 422005", True),
    ("Unique Identification Authority of India", False),
]

_MARK = re.compile(r"\[\[([A-Z_]+):(.+?)\]\]")


def parse(marked: str) -> tuple[str, list[dict]]:
    """'[[PERSON:Rashi Patil]] called' -> ('Rashi Patil called', [{start, end, type, text}])."""
    out, spans, pos = [], [], 0
    for m in _MARK.finditer(marked):
        out.append(marked[pos:m.start()])
        start = sum(len(p) for p in out)
        out.append(m.group(2))
        spans.append({"start": start, "end": start + len(m.group(2)), "type": m.group(1), "text": m.group(2)})
        pos = m.end()
    out.append(marked[pos:])
    return "".join(out), spans


def _font(size: int):
    for name in ("DejaVuSans.ttf", "arial.ttf", "Arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def make_id_card() -> tuple[bytes, list[dict]]:
    img = Image.new("RGB", (900, 520), (236, 242, 248))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, 899, 70], fill=(255, 153, 51))
    boxes = []
    y = 18
    for i, (line, is_pii) in enumerate(ID_CARD_LINES):
        font = _font(34 if i == 0 else 30)
        x = 240 if i == 0 else 40
        if i == 1:
            y = 110
        draw.text((x, y), line, fill=(0, 0, 0), font=font)
        if ":" in line and is_pii:
            label, value = line.split(":", 1)
            lx = x + draw.textlength(label + ": ", font=font)
            l, t, r, b = draw.textbbox((lx, y), value.strip(), font=font)
            boxes.append({"text": value.strip(), "pii": True, "box": [l, t, r, b]})
            l, t, r, b = draw.textbbox((x, y), label + ":", font=font)
            boxes.append({"text": label + ":", "pii": False, "box": [l, t, r, b]})
        else:
            l, t, r, b = draw.textbbox((x, y), line, font=font)
            boxes.append({"text": line, "pii": is_pii, "box": [l, t, r, b]})
        y += 70
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue(), boxes


def _add_marked_runs(paragraph, marked: str) -> None:
    """Write text as several runs (PII values in bold) so run-splitting is exercised."""
    pos = 0
    for m in _MARK.finditer(marked):
        if m.start() > pos:
            paragraph.add_run(marked[pos:m.start()])
        paragraph.add_run(m.group(2)).bold = True
        pos = m.end()
    if pos < len(marked):
        paragraph.add_run(marked[pos:])


def build() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    document = docx.Document()
    document.styles["Normal"].font.size = Pt(10)
    document.add_heading(TITLE, level=1)
    gold_units = [{"text": TITLE, "entities": []}]

    for marked in PARAGRAPHS:
        text, spans = parse(marked)
        _add_marked_runs(document.add_paragraph(), marked)
        gold_units.append({"text": text, "entities": spans})

    document.add_paragraph("Ticket summary table")
    gold_units.append({"text": "Ticket summary table", "entities": []})
    table = document.add_table(rows=1, cols=len(TABLE_HEADER))
    table.style = "Table Grid"
    for cell, head in zip(table.rows[0].cells, TABLE_HEADER):
        cell.text = head
        gold_units.append({"text": head, "entities": []})
    for row in TABLE_ROWS:
        cells = table.add_row().cells
        for cell, marked in zip(cells, row):
            text, spans = parse(marked)
            p = cell.paragraphs[0]
            _add_marked_runs(p, marked)
            gold_units.append({"text": text, "entities": spans})

    document.add_paragraph("Scanned identity document attached to ticket TCK-200103:")
    gold_units.append({"text": "Scanned identity document attached to ticket TCK-200103:", "entities": []})
    card, boxes = make_id_card()
    document.add_picture(io.BytesIO(card), width=Inches(4.5))
    document.save(DOCX_PATH)

    for u in gold_units:
        u["sha1"] = hashlib.sha1(u["text"].encode("utf-8")).hexdigest()
    gold = {
        "description": "SYNTHETIC gold labels for synthetic_ticket_log.docx (generated by make_synthetic.py)",
        "units": gold_units,
        "image": {"index": 0, "size": [900, 520], "boxes": boxes},
    }
    GOLD_PATH.write_text(json.dumps(gold, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {DOCX_PATH} and {GOLD_PATH}")


if __name__ == "__main__":
    build()
