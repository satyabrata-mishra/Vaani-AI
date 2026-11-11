import pytest

from app.config import INDEX_DIR, RETRIEVAL_SETTINGS
from app.pipeline.retrieval import RetrievalTool, ToolError, ToolTimeout, retrieve_sync

pytestmark = pytest.mark.skipif(not (INDEX_DIR / "small_k6.jsonl").exists(),
                                reason="run python -m app.ingest.build_index first")


@pytest.mark.parametrize("setting", list(RETRIEVAL_SETTINGS))
def test_known_answer_is_retrieved(setting):
    r = retrieve_sync("Scaled Dot-Product Attention divide by square root of dk", setting)
    sections = [p.chunk.section for p in r.passages]
    assert "3.2.1 Scaled Dot-Product Attention" in sections
    assert len(r.passages) <= RETRIEVAL_SETTINGS[setting].pieces
    assert r.reranked == RETRIEVAL_SETTINGS[setting].rerank


def test_citation_format():
    p = retrieve_sync("Scaled Dot-Product Attention", "medium_k4_rr").passages[0]
    assert p.citation().startswith("Attention (arXiv 1706.03762v7), §3.2")


def test_reranker_separates_off_topic_question():
    on = retrieve_sync("How does H-Net decide chunk boundaries?", "medium_k4_rr")
    off = retrieve_sync("What is the capital of France?", "medium_k4_rr")
    assert on.top_rerank > 0 > off.top_rerank


async def test_tool_timeout():
    tool = RetrievalTool(timeout_s=0.05, delay_s=0.5)
    with pytest.raises(ToolTimeout):
        await tool("attention", "small_k6")


async def test_tool_injected_failure():
    tool = RetrievalTool(timeout_s=5, failure_rate=1.0)
    with pytest.raises(ToolError):
        await tool("attention", "small_k6")


async def test_tool_ok_and_overrides():
    r = await RetrievalTool(timeout_s=30)("Mimi codec", "medium_k4_rr", pieces=2, rerank=False)
    assert len(r.passages) == 2 and not r.reranked
