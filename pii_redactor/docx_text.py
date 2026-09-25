"""In-place text access and editing for .docx paragraphs and table cells.

A Word paragraph's visible text is spread over many ``<w:t>`` runs (and runs
nested in hyperlinks, smart tags, insertions...). ``python-docx``'s
``Paragraph.runs`` skips runs inside ``<w:hyperlink>``, so this module walks
the paragraph XML itself and builds a list of *segments* (text, tab and
break nodes) with character offsets. A PII span that crosses several runs is
replaced by writing the fake value into the first run and removing the
remaining characters from the following runs. Run formatting is untouched and
nothing is appended to the document.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Iterator

from docx.document import Document as DocumentObject
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph

W_P, W_T, W_TAB, W_BR, W_CR = qn("w:p"), qn("w:t"), qn("w:tab"), qn("w:br"), qn("w:cr")
W_INSTR, W_TC, W_TR, W_TBL = qn("w:instrText"), qn("w:tc"), qn("w:tr"), qn("w:tbl")
W_FLDSIMPLE = qn("w:fldSimple")
_SKIP_SUBTREES = {qn("w:pPr"), qn("w:rPr"), qn("w:delText"), qn("w:sectPr"), qn("w:tblPr")}
_XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"


@dataclass
class Segment:
    node: object  # lxml element
    kind: str  # "text", "tab", "break", "instr"
    start: int
    end: int


@dataclass
class TextUnit:
    """One paragraph (in the body, a table cell, a header/footer or a text box)."""

    index: int
    paragraph: Paragraph
    part_name: str
    container: object  # parent element used to group consecutive lines (cell, body...)
    context: tuple[str, ...]
    segments: list[Segment] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(_segment_text(s) for s in self.segments)


def _segment_text(seg: Segment) -> str:
    if seg.kind in ("text", "instr"):
        return seg.node.text or ""
    return "\t" if seg.kind == "tab" else "\n"


def _collect_segments(p_el, kinds: set[str]) -> list[Segment]:
    """Depth-first walk of a paragraph, excluding nested paragraphs (text boxes)."""
    segments: list[Segment] = []
    pos = 0

    def walk(el):
        nonlocal pos
        for child in el:
            tag = child.tag
            if not isinstance(tag, str) or tag in _SKIP_SUBTREES or tag == W_P:
                continue
            kind = None
            if tag == W_T:
                kind = "text"
            elif tag == W_TAB:
                kind = "tab"
            elif tag in (W_BR, W_CR):
                kind = "break"
            elif tag == W_INSTR:
                kind = "instr"
            if kind is not None:
                if kind in kinds:
                    seg = Segment(child, kind, pos, pos)
                    seg.end = pos + len(_segment_text(seg))
                    pos = seg.end
                    segments.append(seg)
                continue
            walk(child)

    walk(p_el)
    return segments


def _cell_text(tc) -> str:
    return " ".join("".join(t.text or "" for t in p.iter(W_T)) for p in tc.iter(W_P)).strip()


def _table_context(p_el) -> tuple[str, ...]:
    """Column header of the table cell containing the paragraph (e.g. ('DIN',))."""
    tc = next((a for a in p_el.iterancestors(W_TC)), None)
    if tc is None:
        return ()
    tr = tc.getparent()
    tbl = tr.getparent() if tr is not None else None
    if tbl is None or tbl.tag != W_TBL:
        return ()
    col = [c for c in tr if c.tag == W_TC].index(tc)
    first_row = next((r for r in tbl if r.tag == W_TR), None)
    if first_row is None or first_row is tr:
        return ()
    header_cells = [c for c in first_row if c.tag == W_TC]
    if col >= len(header_cells):
        return ()
    words = re.findall(r"[A-Za-z]{2,}", _cell_text(header_cells[col]))
    return tuple(words[:6])


def iter_parts(document: DocumentObject):
    """Document body part plus every distinct header/footer part."""
    seen = set()
    yield document.part
    seen.add(document.part.partname)
    for section in document.sections:
        for hf in (section.header, section.footer, section.first_page_header, section.first_page_footer,
                   section.even_page_header, section.even_page_footer):
            try:
                if hf.is_linked_to_previous:
                    continue
                part = hf.part
            except (AttributeError, KeyError, ValueError):
                continue
            if part.partname not in seen:
                seen.add(part.partname)
                yield part


def iter_text_units(document: DocumentObject) -> Iterator[TextUnit]:
    """Yield every paragraph in document order: body, tables (incl. nested), text boxes, headers, footers."""
    index = 0
    for part in iter_parts(document):
        for p_el in part.element.iter(W_P):
            segments = _collect_segments(p_el, {"text", "tab", "break"})
            if not segments:
                continue
            tc = next((a for a in p_el.iterancestors(W_TC)), None)
            unit = TextUnit(
                index=index,
                paragraph=Paragraph(p_el, None),
                part_name=str(part.partname),
                container=tc if tc is not None else p_el.getparent(),
                context=_table_context(p_el),
                segments=segments,
            )
            index += 1
            yield unit


def apply_replacements(segments: list[Segment], replacements: list[tuple[int, int, str]]) -> None:
    """Replace ``[start, end)`` ranges of the concatenated segment text, in place.

    Ranges are applied right-to-left so earlier offsets stay valid. The fake
    value goes into the first overlapping text node; characters of the span in
    later nodes are deleted and tab/break nodes fully inside the span removed.
    """
    for start, end, new in sorted(replacements, key=lambda r: r[0], reverse=True):
        placed = False
        for seg in segments:
            if seg.end <= start or seg.start >= end:
                continue
            if seg.kind in ("tab", "break"):
                if start <= seg.start and seg.end <= end and seg.node.getparent() is not None:
                    seg.node.getparent().remove(seg.node)
                continue
            text = seg.node.text or ""
            ls, le = max(start, seg.start) - seg.start, min(end, seg.end) - seg.start
            insert = new if not placed else ""
            seg.node.text = text[:ls] + insert + text[le:]
            seg.node.set(_XML_SPACE, "preserve")
            placed = True
        if not placed:  # span started on a tab/break: attach to the next text node
            nxt = next((s for s in segments if s.kind in ("text", "instr") and s.start >= start), None)
            if nxt is not None:
                nxt.node.text = new + (nxt.node.text or "")


_MAILTO_OR_URL = re.compile(r"(?i)(mailto:)?([\w.+-]+@[\w-]+(?:\.[\w-]+)+)|((?:https?://|www\.)[^\s\"<>]+)")


def rewrite_links(text: str, replace_email: Callable[[str], str], replace_url: Callable[[str], str]) -> str:
    """Rewrite e-mail addresses and URLs inside field codes / relationship targets."""

    def sub(m: re.Match) -> str:
        if m.group(2):
            return (m.group(1) or "") + replace_email(m.group(2))
        return replace_url(m.group(3))

    return _MAILTO_OR_URL.sub(sub, text)


def redact_field_codes(document: DocumentObject, replace_email, replace_url) -> int:
    """HYPERLINK field codes (``<w:instrText>``) and ``<w:fldSimple w:instr>`` hide link targets."""
    changed = 0
    for part in iter_parts(document):
        for p_el in part.element.iter(W_P):
            segments = _collect_segments(p_el, {"instr"})
            if segments:
                text = "".join(_segment_text(s) for s in segments)
                new = rewrite_links(text, replace_email, replace_url)
                if new != text:
                    # Field code text is not displayed, so it is safe to consolidate it.
                    segments[0].node.text = new
                    for seg in segments[1:]:
                        seg.node.text = ""
                    changed += 1
        for fld in part.element.iter(W_FLDSIMPLE):
            instr = fld.get(qn("w:instr")) or ""
            new = rewrite_links(instr, replace_email, replace_url)
            if new != instr:
                fld.set(qn("w:instr"), new)
                changed += 1
    return changed


def redact_hyperlink_targets(document: DocumentObject, replace_email, replace_url) -> int:
    """External hyperlink relationships (``mailto:``/``http:`` targets in the .rels files)."""
    changed = 0
    for part in iter_parts(document):
        for rel in part.rels.values():
            if not rel.is_external or rel.reltype != RT.HYPERLINK:
                continue
            new = rewrite_links(rel.target_ref, replace_email, replace_url)
            if new != rel.target_ref:
                rel._target = new
                changed += 1
    return changed


def scrub_core_properties(document: DocumentObject) -> None:
    props = document.core_properties
    for attr in ("author", "last_modified_by", "comments", "keywords", "subject", "category"):
        try:
            setattr(props, attr, "")
        except (AttributeError, ValueError):
            pass
