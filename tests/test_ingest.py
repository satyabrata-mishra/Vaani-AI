import pytest

from app.ingest.bm25 import BM25, tokenize
from app.ingest.chunker import approx_tokens, chunk_paper
from app.ingest.parse_pdf import Line, parse_paper


@pytest.fixture(scope="module")
def parsed():
    return {pid: parse_paper(pid) for pid in ("attention", "moshi", "hnet")}


def sections(lines):
    return {l.section for l in lines}


def test_sections_and_pages_detected(parsed):
    att = parsed["attention"]
    assert "3.2.1 Scaled Dot-Product Attention" in sections(att)
    first = next(l for l in att if l.section == "3.2.1 Scaled Dot-Product Attention")
    assert first.page == 4
    assert "2.2.1 Chunking Layer" in sections(parsed["hnet"])
    assert "3.3 Audio Tokenization" in sections(parsed["moshi"])


def test_moshi_words_are_separated(parsed):
    text = " ".join(l.text for l in parsed["moshi"])
    assert "Moshi is built on top of Helium" in text
    assert "MoshiisbuiltontopofHelium" not in text


def test_running_headers_and_table_cells_are_not_sections(parsed):
    for lines in parsed.values():
        for s in sections(lines):
            parts = s.split(maxsplit=1)
            assert len(parts) == 1 or parts[1][0].isalpha(), s  # "28.4 41.8" was a table cell
    assert not any("speech-text foundation model for real-time dialogue" in l.text for l in parsed["moshi"])


def test_references_flagged(parsed):
    refs = [l for l in parsed["attention"] if l.in_references]
    assert refs and all(l.page >= 10 for l in refs)
    assert not any(l.in_references for l in parsed["hnet"] if l.section.startswith("A.1"))


def test_chunks_are_section_pure_bounded_and_skip_references(parsed):
    chunks = chunk_paper("attention", parsed["attention"], piece_tokens=200, overlap_tokens=40)
    assert chunks
    for c in chunks:
        assert c.n_tokens <= 200
        assert c.page_start <= c.page_end
        assert c.section != "References"
    by_section = {}
    for c in chunks:
        by_section.setdefault(c.section, []).append(c)
    assert "3.2.1 Scaled Dot-Product Attention" in by_section


def test_hyphen_rejoined_only_when_word_exists():
    lines = [Line(1, "S", "the architec-", False), Line(1, "S", "ture is an architecture.", False),
             Line(1, "S", "a high-", False), Line(1, "S", "quality model.", False)]
    text = chunk_paper("x", lines, piece_tokens=100, overlap_tokens=10)[0].text
    assert "the architecture is" in text
    assert "high-quality" in text


def test_approx_tokens_conservative():
    assert approx_tokens("a" * 450) == 100


def test_bm25_prefers_exact_terms():
    docs = ["positional encoding uses sine and cosine", "the decoder stack has six layers", "audio codec"]
    bm = BM25(docs)
    s = bm.scores("sine cosine positional")
    assert s.index(max(s)) == 0
    assert bm.max_possible("sine cosine") >= max(bm.scores("sine cosine"))
    assert bm.unknown_terms("quantum gravity") == [tokenize("quantum")[0], tokenize("gravity")[0]]
