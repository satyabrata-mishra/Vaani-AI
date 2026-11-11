"""PDF → list of text lines tagged with page and section, using PyMuPDF's span fonts.

Heading rule (checked on all three papers): a *bold* line that is a section number
("3.2.1", "A.1") followed by a bold title line, or a bold line "3 Model Architecture".
Bold = font flag 16 or a bold font name. Unnumbered headings we track: Abstract, References,
Acknowledgements, Appendix."""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import asdict, dataclass

import pymupdf

from app.config import DATA_DIR, PAPERS_DIR

PAPERS = {
    "attention": {"file": "1706.03762v7.pdf", "arxiv": "1706.03762v7", "title": "Attention Is All You Need",
                  "short": "Attention"},
    "moshi": {"file": "2410.00037v1.pdf", "arxiv": "2410.00037v1",
              "title": "Moshi: a speech-text foundation model for real-time dialogue", "short": "Moshi"},
    "hnet": {"file": "2507.07955v2.pdf", "arxiv": "2507.07955v2",
             "title": "Dynamic Chunking for End-to-End Hierarchical Sequence Modeling", "short": "H-Net"},
}

SEC_NUM = r"(?:[1-9]|[A-H])(?:\.\d{1,2})*\.?"  # top-level 1-9 keeps bold table cells ("42.8") out
NUM_ONLY = re.compile(rf"^{SEC_NUM}$")
NUM_TITLE = re.compile(rf"^({SEC_NUM})\s+([A-Z][a-zA-Z-][^\n]{{1,90}})$")
TITLE_START = re.compile(r"^[A-Z][a-zA-Z-]")
RUNNING_HEADER_PAGES = 5  # a line repeated verbatim on this many pages is a running header
UNNUMBERED = {"abstract", "references", "acknowledgements", "acknowledgments", "appendix"}
MIN_FONT = 7.5  # drops figure tick labels and stray sub/superscripts (checked on H-Net p.20)
MARGIN = 0.06   # running headers / page numbers live in the top and bottom 6% of the page
SPACE_GAP = 0.12  # gap between spans, as a fraction of font size, that implies a word break.
# Moshi's PDF positions words without space characters, so spaces must come from the gaps.


@dataclass
class Line:
    page: int          # 1-based, as printed in a PDF viewer
    section: str       # "3.2.1 Scaled Dot-Product Attention", or "Front matter"
    text: str
    in_references: bool


def _is_heading_font(span) -> bool:
    f = span["font"]
    return bool(span["flags"] & 16) or any(k in f for k in ("Bold", "Medi", "TB", "-B", "CMBX", "CSC"))


def _join_spans(spans) -> str:
    parts = [spans[0]["text"]]
    for prev, cur in zip(spans, spans[1:]):
        gap = cur["bbox"][0] - prev["bbox"][2]
        if gap > SPACE_GAP * cur["size"]:
            parts.append(" ")
        parts.append(cur["text"])
    return " ".join("".join(parts).split())


def _page_lines(page) -> list[tuple[str, bool, float]]:
    out = []
    height = page.rect.height
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            spans = [s for s in line["spans"] if s["text"].strip()]
            if not spans:
                continue
            y0, y1 = line["bbox"][1], line["bbox"][3]
            if y1 < MARGIN * height or y0 > (1 - MARGIN) * height:
                continue
            size = max(s["size"] for s in spans)
            if size < MIN_FONT:
                continue
            out.append((_join_spans(spans), all(_is_heading_font(s) for s in spans), size))
    return out


def parse_paper(paper_id: str) -> list[Line]:
    meta = PAPERS[paper_id]
    doc = pymupdf.open(PAPERS_DIR / meta["file"])
    section = "Front matter"
    in_refs = False
    lines: list[Line] = []
    pages = [_page_lines(page) for page in doc]
    seen_on = Counter(t for raw in pages for t in {t for t, _, _ in raw if len(t) > 15})
    running = {t for t, n in seen_on.items() if n >= RUNNING_HEADER_PAGES}
    for pno, raw in enumerate(pages, start=1):
        raw = [r for r in raw if r[0] not in running]
        i = 0
        while i < len(raw):
            text, bold, size = raw[i]
            heading = None
            if (bold and NUM_ONLY.match(text) and i + 1 < len(raw) and raw[i + 1][1]
                    and TITLE_START.match(raw[i + 1][0])):
                heading = f"{text.rstrip('.')} {raw[i + 1][0]}"
                i += 1
            elif bold and (m := NUM_TITLE.match(text)) and len(text) < 100:
                heading = f"{m.group(1).rstrip('.')} {m.group(2)}"
            elif bold and text.strip().lower() in UNNUMBERED:
                heading = text.strip().title()
            if heading:
                section = heading
                if heading.lower() == "references":
                    in_refs = True
                elif in_refs and re.match(r"^[A-H](\.\d+)*\s", heading):
                    in_refs = False  # appendix after the bibliography
            elif re.fullmatch(r"\d{1,3}", text) and not bold:
                pass  # page number
            else:
                lines.append(Line(pno, section, text, in_refs))
            i += 1
    return lines


def parse_all() -> dict[str, list[Line]]:
    out_dir = DATA_DIR / "parsed"
    out_dir.mkdir(parents=True, exist_ok=True)
    result = {}
    for pid in PAPERS:
        lines = parse_paper(pid)
        (out_dir / f"{pid}.jsonl").write_text(
            "\n".join(json.dumps(asdict(l)) for l in lines), encoding="utf-8")
        result[pid] = lines
    return result


if __name__ == "__main__":
    for pid, lines in parse_all().items():
        secs = []
        for l in lines:
            if not secs or secs[-1][0] != l.section:
                secs.append((l.section, l.page, l.in_references))
        words = sum(len(l.text.split()) for l in lines if not l.in_references)
        ref_words = sum(len(l.text.split()) for l in lines if l.in_references)
        print(f"== {pid}: {len(lines)} lines, {words} words indexed, {ref_words} words in references")
        for s, p, r in secs:
            print(f"   p.{p:<3} {'[refs] ' if r else ''}{s}")
