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