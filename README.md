# PII Redaction Tool (.docx, in place)

Reads a Word document (built and tested on the Red Herring Prospectus from the
assignment). It replaces personal and company-identifying information in the
text with **consistent fake values** and blacks out PII inside **embedded
images** such as scanned ID cards. The layout is not changed and no text is
appended: every edit happens inside the existing runs, table cells, field codes
and image parts of the `.docx`.

| Deliverable | Where |
|---|---|
| Source code | `pii_redactor/` (CLI: `python -m pii_redactor`) |
| Redacted document | `output/Red_Herring_Prospectus_redacted.docx` (+ `output/run_report.json`) |
| Approach, tradeoffs, FP/FN | this README |
| Evaluation method + report | [Evaluation](#evaluation) below, `evaluation/REPORT.md`, `evaluation/results.json` |
| Tests | `tests/` (38 tests, `pytest`) |

The original prospectus is **not** in the repository. It contains real PII, and
`data/` is git-ignored. Copy it to `data/Red_Herring_Prospectus.docx` to
reproduce the results.

---

## Quick start (Windows)

Requires Python 3.10+ (tested on 3.12) and Tesseract OCR.

1. **Tesseract**: install the UB-Mannheim build from
   <https://github.com/UB-Mannheim/tesseract/wiki>. During setup, tick
   *Additional language data → Hindi* (Aadhaar cards are bilingual). The
   default path is `C:\Program Files\Tesseract-OCR\tesseract.exe`. Either add
   that folder to `PATH` or pass `--tesseract-cmd`.
2. **Python environment** (PowerShell, in the project folder):

   ```powershell
   py -3.12 -m venv .venv
   .\.venv\Scripts\Activate.ps1          # cmd.exe: .venv\Scripts\activate.bat
   pip install -r requirements.txt
   python -m spacy download en_core_web_lg
   ```

   If PowerShell blocks the activation script, run
   `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once.
3. **Redact**:

   ```powershell
   python -m pii_redactor data\Red_Herring_Prospectus.docx -o output\Red_Herring_Prospectus_redacted.docx --report output\run_report.json
   # if tesseract is not on PATH:
   python -m pii_redactor data\Red_Herring_Prospectus.docx --tesseract-cmd "C:\Program Files\Tesseract-OCR\tesseract.exe"
   ```

   The prospectus takes about 20 s on a laptop CPU (4,215 text units, 8 images).

Linux/macOS: the same steps with `python3 -m venv .venv && source .venv/bin/activate`,
plus `apt install tesseract-ocr tesseract-ocr-hin` or `brew install tesseract tesseract-lang`.

### CLI options

| Option | Meaning |
|---|---|
| `-o OUTPUT` | output file (default `<input>_redacted.docx`) |
| `--report FILE` | JSON run report: counts per type/recognizer, per-image boxes. Contains **no** original PII |
| `--mapping FILE` | also write the original → fake mapping. **Sensitive**: it contains the real PII and makes the redaction reversible. Git-ignored (`*mapping*.json`) |
| `--seed N` | Faker seed (default 42), so reruns produce identical fakes |
| `--no-images` / `--no-faces` / `--no-qr` | skip image redaction entirely, or skip face/QR blackout |
| `--ocr-lang` | Tesseract languages, default `auto` (`eng+hin` if Hindi data is installed) |
| `--tesseract-cmd` | path to `tesseract.exe` |

---

## Approach

```
.docx ──python-docx──► text units (paragraphs, table cells incl. nested, headers/footers,
   │                    text boxes, footnotes, field-code instructions such as HYPERLINK "mailto:")
   │                        │
   │                        ▼
   │                Presidio AnalyzerEngine (spaCy en_core_web_lg + built-in + custom recognizers)
   │                        │
   │                        ▼
   │                post-analysis verification  (postprocess.py)
   │                        │
   │                        ▼
   │                document-level passes       (pipeline.py)
   │                        │
   │                        ▼
   │                ReplacementMap (Faker en_IN, cached original→fake) ──► rewrite runs in place
   │
   └──image parts──► PIL preprocess ─► pytesseract.image_to_data (pandas) ─► same detector on OCR lines
                     + ID-card label rules + OpenCV faces/QR ─► opaque black rectangles ─► part._blob
```

### 1. Text: parse and replace in place (`docx_text.py`, `pipeline.py`)

* Each paragraph and each table cell is one **text unit**. Its text is the
  concatenation of its runs. The unit remembers which character range belongs
  to which run.
* A replacement is written into the run where the entity starts, and the rest
  of the entity is removed from the following runs. Run formatting (bold,
  font, hyperlinks) is therefore preserved and nothing is appended.
* Field codes (`HYPERLINK "mailto:…"`) and hyperlink relationship targets get
  the same fake as the visible text, so a clickable e-mail does not leak the
  original.

### 2. Detection: Presidio + spaCy `en_core_web_lg` (`analyzer.py`, `recognizers.py`)

`AnalyzerEngine(nlp_engine=…, registry=…)` is created with only valid
arguments (no `deny_list` / `supported_languages`). The registry contains
Presidio's predefined recognizers plus these custom ones:

| Recognizer | Entity | Why |
|---|---|---|
| `IndianPhoneRecognizer` | PHONE_NUMBER | `+91 22 4009 4400`, `022-…`, toll-free numbers; Presidio's phone recognizer misses many Indian formats |
| `OrganizationSuffixRecognizer` | ORGANIZATION | "… Private Limited / Ltd / LLP / Bank / GmbH / Inc" chains; far more precise than spaCy ORG |
| `ContactPersonRecognizer` | PERSON | "Contact Person: …", "Mr./Ms. …" |
| `AddressRecognizer` (+ address-block pass) | ADDRESS | Indian PIN codes (`400 051`), US `City, ST 12345`, street cues |
| `DateOfBirthRecognizer` | DATE_OF_BIRTH | a date is only PII when a birth cue precedes it in the same sentence |
| `DinRecognizer`, `AadhaarRecognizer` (Verhoeff), Presidio `InPanRecognizer` | IN_DIN, IN_AADHAAR, IN_PAN | Indian personal identifiers, context-gated |
| `BrokenUrlRecognizer` | URL | `www.company. com` (a conversion artefact in the document) |
| Presidio built-ins | EMAIL_ADDRESS, US_SSN, CREDIT_CARD (Luhn), IP_ADDRESS, URL | |

Rule-based recognizers emit internal types (`ORG_SUFFIX`, `PERSON_LABELLED`).
This stops Presidio's de-duplication from letting a longer, sloppier spaCy span
of the same type swallow a precise rule span. The internal types are mapped
back afterwards. Tabs are turned into spaces before analysis (same length, so
offsets stay valid) because spaCy splits names at tabs.

### 3. Post-analysis verification (`postprocess.py`)

The analyzer is deliberately permissive, and false positives are removed
afterwards by a manual pass, as the brief requires:

* **Roles and headers**: "Company Secretary", "Director", "Book Running Lead
  Manager", "Registrar to the Offer", … (`ROLE_AND_HEADER_TERMS` in
  `config.py`) are dropped or trimmed off the span edges. "Mr. Rajesh Kumar,
  Company Secretary" keeps only the name.
* **Public bodies**: SEBI, RBI, stock exchanges, ministries, courts and the
  Registrar of Companies are kept, together with their addresses and URLs.
  They are not personal data, and a reader of a prospectus needs them.
* **Defined terms**: "Offer Document", "Green Shoe Option", "Anchor Investor
  Allocation Price" and similar terms are ORG/PERSON-looking capitalised
  phrases. A spaCy PERSON/ORG span is dropped when **every** token is an
  ordinary word. A token counts as ordinary if the document also uses it in
  lowercase, if it is a generic/legal word, or if it is an English dictionary
  word (WordNet, from spaCy's lemmatizer tables) that is not a common Indian or
  US first/last name.
* Trimming of determiners, labels ("DIN:", "Address:") and trailing generic
  tokens; unbalanced parentheses; spans followed by "Act", "Regulations",
  "Prospectus".

### 4. Document-level passes (`pipeline.py`)

* **Propagation**: once "Waterloo Motors Private Limited" is found, every
  other capitalised occurrence of that name is replaced too, even where the
  model missed it (in headings, all caps and tables).
* **Short forms**: the brand token of a legal-suffix company ("Nuvama" from
  "Nuvama Wealth Management Limited") and the suffix-stripped name are
  replaced with the matching part of the full name's fake, so the text still
  reads consistently.
* **Split names**: a name broken across two paragraphs is joined and replaced once.

### 5. Consistent fake values (`faker_map.py`)

`ReplacementMap` caches `(entity type, normalised original) → fake`, so the
same person or company gets the same fake everywhere, including case variants
(an all-caps original gets an all-caps fake). Fakes use Faker `en_IN`, seeded:

* **Format-preserving**: a phone keeps its `+91 22 XXXX XXXX` grouping, an
  e-mail keeps its shape, and an ORG keeps its original legal suffix
  ("Private Limited" stays "Private Limited").
* **Consistent across types**: a person's e-mail uses the fake person's name
  where the local part matches; URLs map domains consistently.
* **Deliberately invalid identifiers**: credit cards fail Luhn, Aadhaar fails
  Verhoeff, SSNs start with 9xx (never issued), IPs are from the RFC 5737
  documentation ranges. A fake can therefore never be a real live number.

### 6. Images and scanned IDs (`image_redactor.py`)

Image text is **not** extracted into the document. Each image part is processed
like this:

1. Load the blob with PIL. Preprocess it for OCR only: grayscale,
   auto-contrast, contrast ×1.8, sharpen, upscale small images. The original
   pixels are what gets painted.
2. `pytesseract.image_to_data(..., output_type=DATAFRAME)`, filtered by
   confidence in pandas, then grouped into lines. Rows are assigned by vertical
   overlap and split on large horizontal gaps. Oversized noise "words" are
   ignored.
3. PII boxes:
   * the same text detector runs on each OCR line, and character offsets are
     mapped back to word boxes;
   * if the image looks like an **ID document** (Aadhaar / PAN / "Govt. of
     India" / "DOB" cues), label rules also apply. The value after "Name",
     "DOB", "Address", "Aadhaar No" and similar labels is blacked out, as is
     the holder's name line and its Devanagari twin.
   * **faces** (OpenCV Haar cascade) and **QR codes** (OpenCV detector, plus a
     texture fallback on ID cards) are blacked out too.
4. Solid opaque black rectangles are drawn on the original image, which is
   re-encoded in its original format and size. `part._blob` is overwritten,
   so relationships, size and position in the document are unchanged.

In the prospectus, 2 of the 8 images are ID-card specimens and both are fully
redacted. The other 6 (small logos and graphics) contain no OCR-readable PII
and are left byte-identical.

---

## Policy choices (explicit)

| Item | Choice | Reason |
|---|---|---|
| People's names, private-company names, their addresses, e-mails, phones, websites, DIN/PAN/Aadhaar | **redacted** | identify a person or the issuer group |
| Order / ticket / invoice / application numbers | **kept** | not personal data on their own. The brief says either choice is fine if explicit. The synthetic set checks that they are kept |
| CIN, SEBI registration, firm registration numbers | **kept** | public regulatory identifiers of companies, not people |
| Public bodies (SEBI, RBI, NSE/BSE, ministries, RoC, courts), their addresses and URLs | **kept** | public institutions |
| Newspapers, standalone city/state names, generic dates | **kept** | not identifying on their own |
| Dates | **redacted only as DOB** | needs a birth cue ("DOB", "born on", "जन्म", a "Date of Birth" column) |
| Defined terms that look like names ("Green Shoe Option") | **kept** | vocabulary filter (see step 3) |

Changing a policy is a config edit. Move a type in or out of `TEXT_ENTITIES`
or edit the term lists in `config.py` / `postprocess.py`.

---

## Evaluation

The source document has no labels, so I built three kinds of gold data. All of
them are scored by `evaluation/evaluate.py`.

1. **RHP development sample** (`evaluation/gold/rhp_gold.json`): 390 text units
   (the dense cover/contact pages plus 80 random units), hand-labelled with
   169 entities. **The rules were tuned while looking at these units**, so the
   numbers are optimistic.
2. **RHP held-out sample** (`evaluation/gold/rhp_heldout_gold.json`): 254
   *different* units (the rest of the "General Information" section plus 50
   new random units), 185 entities. **Labelled after the rules were frozen,
   scored once, no tuning afterwards.** This is the honest estimate for the
   prospectus.
3. **Synthetic ticket log** (`evaluation/synthetic/`, made by
   `evaluation/make_synthetic.py`). The prospectus contains no SSNs, credit
   cards, IPs or dates of birth, so this **clearly labelled SYNTHETIC** document
   covers them: 25 paragraphs, a table and a fake ID-card image, 49 entities.
   It also contains *negatives* that must be kept: order, ticket and invoice
   numbers, non-birth dates, a public helpline.

The gold files store only unit index, SHA-1 of the unit text and character
offsets, never the PII text. If the evaluator is run on a different document,
the SHA-1 check fails loudly.

**Metrics.**

* **Token level, type-agnostic** (headline): a token is a `\w+` run. It is PII
  if a gold span covers it and counts as predicted if any detected span covers
  it. This yields TP/FP/FN/TN, from which precision, recall, F1 and accuracy
  follow. It is type-agnostic because, for privacy, redacting a company name
  typed as PERSON still hides it.
* **Per type**: the same metrics, but the type must also match.
* **Entity level**: each gold entity is *fully*, *partially* or *not*
  redacted. A partial redaction still leaks something.
* **Image**: black-pixel coverage of each gold box on the redacted synthetic
  ID card. ≥ 90 % counts as redacted; ≥ 50 % on a non-PII box counts as a false
  positive.
* **Leak check**: every unique gold PII value is searched for in the *entire*
  redacted prospectus, not just the sampled units.

Accuracy is always high, because most tokens are not PII and are correctly
left alone. Precision and recall are the numbers that matter.

### Results

| Dataset | Precision | Recall | F1 | Accuracy | Entities fully redacted |
|---|---|---|---|---|---|
| RHP **held-out** (254 units, scored once) | **0.984** | **0.986** | **0.985** | 0.995 | 181/185 (0.978) |
| RHP development (390 units, tuned on) | 1.000 | 0.997 | 0.999 | 1.000 | 168/169 (0.994) |
| Synthetic (58 units, SYNTHETIC) | 0.988 | 1.000 | 0.994 | 0.996 | 49/49 (1.000) |

* Synthetic ID-card image: 4/4 PII boxes blacked out, 0/6 non-PII boxes touched.
* Leak check (whole redacted prospectus): 1 of 91 development values and 3 of
  127 held-out values still appear, the same misses listed below.
* First run before any tuning (development set): P 0.976 / R 0.986;
  synthetic P 0.900 / R 0.982; image 3/4 PII boxes redacted, with 1 non-PII
  box wrongly blacked out.

Full tables per type, with the FP and miss lists (RHP values masked), are in
[`evaluation/REPORT.md`](evaluation/REPORT.md).

```powershell
python -m evaluation.make_synthetic            # regenerate the synthetic set (optional, deterministic)
python -m evaluation.evaluate --rhp data\Red_Herring_Prospectus.docx --redacted output\Red_Herring_Prospectus_redacted.docx
python -m evaluation.evaluate                  # synthetic + image only (no prospectus needed)
python -m pytest
```

### False positives and false negatives noticed

**False negatives (PII left in place):**

* *Held-out*: a law firm with a one-word name and no legal suffix ("T…l"). It
  looks like an ordinary capitalised word, and spaCy did not tag it.
* *Held-out*: two building-style addresses without a PIN code or street cue
  ("5th Floor, G… House", "The C…l"), plus one address only partly covered
  where a line break split it.
* *Development*: "Underwriters Laboratories", a company name made only of
  dictionary words. The vocabulary filter removes it.
* Brands that are ordinary words (e.g. the rating agency "CARE") are kept for
  the same reason.
* Company names that appear **only** in images other than the ID cards
  (logos): OCR does not read stylised logos, and logos are not blacked out.

**False positives (non-PII changed):**

* The newspaper "Loksatta" is tagged PERSON by spaCy (held-out, ×2).
* SEBI's own Bandra Kurla Complex address in one held-out unit. The label
  above it did not match the public-body rule because of the line layout.
* Indian place names tagged PERSON: Ahmednagar, Ahilyanagar, Taloja, and
  "Baner" in the synthetic set.
* "Chargeback" (synthetic) is tagged PERSON.
* "I-Sec" (ICICI Securities' short name) is redacted, but typed PERSON
  instead of ORG. That is harmless for privacy but counts against per-type
  precision.
* "Unpai", a PDF-to-Word conversion artefact.
* OCR on the NSDL specimen: the non-PII line "Pune – 411 016" and part of the
  UIDAI helpdesk e-mail are blacked out, because they look like an address and
  an e-mail.

**Other tradeoffs:**

* Faker names can coincide with real people or companies. They are random, not
  checked against a registry.
* The fake of a company keeps its legal suffix, so the entity *kind* stays
  visible. That is intentional, for readability.
* The replacement text has a different length, so line breaks can move
  slightly. Formatting and structure are unchanged.
* The rules are tuned to Indian corporate filings: PIN codes, "Private
  Limited", DIN/PAN. Other document types would need new cue lists. The
  held-out and development gap (≈ 1.5 points F1) is a fair estimate of the
  overfitting.
* Rule-first detection is predictable and explainable, but it needs curated
  lists. A fine-tuned NER model would generalise better and would need
  labelled training data, which this assignment does not provide.

---

## Adding a new PII type

Example: an Indian vehicle registration number (`MH 12 AB 1234`).

1. **Recognizer** (`pii_redactor/recognizers.py`):

   ```python
   class VehicleRegRecognizer(PatternRecognizer):
       def __init__(self):
           super().__init__(
               supported_entity="IN_VEHICLE_REG", name="VehicleRegRecognizer",
               patterns=[Pattern("reg", r"\b[A-Z]{2}\s?\d{2}\s?[A-Z]{1,2}\s?\d{4}\b", 0.3)],
               context=["vehicle", "registration", "reg no"], supported_language="en",
           )
   ```

   Add `VehicleRegRecognizer()` to `custom_recognizers()`.
2. **Enable it** (`pii_redactor/config.py`): add `"IN_VEHICLE_REG"` to
   `TEXT_ENTITIES`. Add it to `ENTITY_THRESHOLDS` if it needs context to pass
   (0.3 + context boost ≥ 0.4).
3. **Fake generator** (`pii_redactor/faker_map.py`): add
   `"IN_VEHICLE_REG": self._vehicle_reg` to the generator table. Write a method
   that returns a format-preserving fake, for example by replacing each
   letter and digit with a random one of the same class.
4. **Test and evaluate**: add a case to `tests/test_recognizers.py` and a
   `[[IN_VEHICLE_REG:…]]` line in `evaluation/make_synthetic.py`, then rerun
   the evaluation.

Images need no extra step: OCR lines go through the same detector. For a new
ID-card label, add it to the `*_LABELS` sets at the top of `image_redactor.py`.

---

## Project layout

```
pii_redactor/
  cli.py            command line (python -m pii_redactor)
  config.py         entity list, thresholds, role/public-body/keyword lists
  analyzer.py       Presidio AnalyzerEngine setup + detect()
  recognizers.py    custom recognizers
  postprocess.py    post-analysis verification (FP filtering, span trimming)
  pipeline.py       DocxRedactor: document-level passes, in-place run rewriting, images
  docx_text.py      text-unit extraction (paragraphs, cells, text boxes, fields) with run offsets
  faker_map.py      ReplacementMap: consistent, format-preserving fakes
  image_redactor.py OCR + ID-card rules + faces/QR + black boxes
  models.py         Span dataclass
evaluation/          gold data, synthetic generator, evaluator, REPORT.md
tests/               pytest suite
output/              redacted prospectus + run report
```

## Assumptions

* "PII" includes company names and addresses, as the brief asks, but not
  public institutions (see the policy table).
* The fake values only need to be realistic and consistent within one
  document. They are not linked to any real person or company.
* The gold labels were made by one annotator (me), so boundary decisions
  (e.g. whether "Limited" is part of the name) follow the policy above. The
  metrics are token-based partly to soften boundary disagreements.
