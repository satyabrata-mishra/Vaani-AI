"""Pipeline steps and the full pipeline against the stand-in. No Groq quota is used."""
import asyncio
import csv

import pytest

from app.config import DEGRADED_PIECES, RETRIEVAL_SETTINGS, load_settings
from app.infra.admission import ModelGate
from app.infra.metrics import ResultsWriter
from app.ingest.chunker import Chunk
from app.pipeline.analyzer import analyze
from app.pipeline.orchestrator import Pipeline
from app.pipeline.prompts import build_messages
from app.pipeline.retrieval import Passage, RetrievalResult, RetrievalTool, retrieve_sync
from app.pipeline.verifier import render, split_sentences, verify
from app.providers.openai_compat import OpenAICompatProvider
from app.simulator.behavior import Faults
from app.simulator.runner import StandInThread


def test_analyzer_names_papers():
    assert analyze("How many heads does the Transformer use?").papers == ["attention"]
    assert set(analyze("Compare Moshi's Helium with the original Transformer").papers) == {"attention", "moshi"}
    assert analyze("What BLEU does H-Net get on WMT 2014?").papers == ["hnet"]
    assert analyze("What is the capital of France?").papers == []
    with pytest.raises(ValueError):
        analyze("  ")


def _p(pid, text, section="3.2", page=4):
    return Passage(Chunk(f"{pid}-1", pid, section, page, page, text, 50), bm25=1.0)


PASSAGES = [
    _p("attention", "We employ h = 8 parallel attention layers, or heads. For each of these we use dk = dv = 64."),
    _p("moshi", "Helium is a 7B parameter text language model trained on 2.1T tokens."),
]


def test_verifier_keeps_supported_and_drops_fabrications():
    v = verify("The Transformer uses 8 attention heads [P1]. Each head has dimension 64. [P1] "
               "It was trained on 3.5T tokens [P1]. Heads are parallel attention layers.", PASSAGES, ["attention"])
    reasons = [s.reason for s in v.dropped]
    assert len(v.verified) == 2
    assert any(r.startswith("number not in cited passage") for r in reasons)
    assert "no citation" in reasons


def test_verifier_wrong_paper_unknown_passage_and_refusal():
    v = verify("Helium was trained on 2.1T tokens [P2]. The model uses heads [P7].", PASSAGES, ["attention"])
    assert [s.reason for s in v.dropped] == ["cites a different paper than the question names",
                                             "cites a passage that was not provided"]
    assert verify("NOT_IN_PAPERS", PASSAGES).refused


def test_verifier_render_renumbers_by_first_use():
    v = verify("Helium has 7B parameters [P2]. It uses 8 heads [P1].", PASSAGES)
    text, sources = render(v, PASSAGES)
    assert "[1]" in text and "[2]" in text and "[P" not in text
    assert sources[0].startswith("[1] Moshi") and sources[1].startswith("[2] Attention")


def test_split_attaches_trailing_citations():
    assert split_sentences("It uses 8 heads. [P1] Each is 64 wide. [P1][P2]") == [
        "It uses 8 heads. [P1]", "Each is 64 wide. [P1][P2]"]


def test_prompt_matches_standin_passage_contract():
    m = build_messages("How many heads?", PASSAGES)
    assert "[P1] Attention (arXiv 1706.03762v7)" in m[1]["content"]
    assert m[1]["content"].index("Question:") < m[1]["content"].index("[P1]")


def test_multi_paper_retrieval_returns_each_paper():
    r = retrieve_sync("Compare Moshi's Helium with the original Transformer", "small_k6",
                      papers=("attention", "moshi"))
    assert {p.chunk.paper_id for p in r.passages} == {"attention", "moshi"}


# --- full pipeline against the stand-in --------------------------------------------------------

@pytest.fixture
def make_pipeline(tmp_path):
    made = []

    def make(profile="normal", setting="small_k6", tool=None, scale=0.05, **changes):
        st = StandInThread(profile, scale=scale, **changes).start()
        settings = load_settings(standin_base_url=st.api_base)
        writer = ResultsWriter(tmp_path / "results.csv", tmp_path / "traces.jsonl")
        pipe = Pipeline(settings, OpenAICompatProvider.standin(settings), writer=writer,
                        retrieval_setting=setting, tool=tool)
        made.append((st, pipe))
        return pipe

    yield make
    for st, _ in made:
        st.stop()


def _rows(tmp_path):
    with (tmp_path / "results.csv").open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


async def test_answer_is_verified_cited_and_recorded(make_pipeline, tmp_path):
    pipe = make_pipeline()
    t = await pipe.handle("How many attention heads does the Transformer base model use?", scenario="test")
    assert t.status == "ok" and t.outcome.startswith("answered")
    assert t.citations and all("Attention (arXiv 1706.03762v7)" in c for c in t.citations)
    assert t.first_token_ms is not None and t.tokens_in > 0 and t.retries == 0
    row = _rows(tmp_path)[0]
    assert row["status"] == "ok" and row["source"] == "stand-in" and row["retrieval_setting"] == "small_k6"
    await pipe.provider.aclose()


async def test_off_topic_is_refused_without_model_call(make_pipeline):
    pipe = make_pipeline(setting="medium_k4_rr")
    t = await pipe.handle("What is the capital of France?")
    assert (t.status, t.outcome) == ("ok", "refused_no_evidence")
    assert t.attempts == [] and t.tokens_in == 0
    await pipe.provider.aclose()


async def test_server_errors_are_retried_twice_then_reported(make_pipeline):
    pipe = make_pipeline(faults=Faults(error_rate=1.0))
    t = await pipe.handle("How many attention heads does the Transformer use?")
    assert (t.status, t.outcome) == ("error", "provider_server")
    assert t.retries == 2 and len(t.attempts) == 3 and t.first_token_ms is None
    await pipe.provider.aclose()


async def test_daily_budget_exhausted_gives_passages_and_stops_calling(make_pipeline):
    pipe = make_pipeline("daily_exhausted", day_used_requests=1000)
    t1 = await pipe.handle("How many attention heads does the Transformer use?")
    assert (t1.status, t1.outcome) == ("rate_limited", "daily_budget_exhausted_RPD")
    a = t1.answer
    assert "daily quota at our provider (Groq) is used up" in a          # what happened
    assert "cannot write an answer" in a                                # no full AI answer
    assert "Nothing below is generated" in a                            # nothing fabricated
    assert "UTC (in ~" in a and "as reported by the provider" in a      # retry time, from the provider's header
    assert "relevant passages, not an answer" in a and t1.citations
    assert t1.first_token_ms is None
    t2 = await pipe.handle("What dropout rate does the Transformer use?")
    assert t2.status == "rate_limited" and t2.attempts == []   # we knew, so we did not ask again
    await pipe.provider.aclose()


def test_daily_reply_when_reset_time_is_unknown():
    from app.infra.rate_limiter import DailyBudgetExhausted
    r = Pipeline._passages_only(PASSAGES, DailyBudgetExhausted("TPD", None))
    assert "(tokens per day)" in r.text and "has not said when" in r.text and "UTC" not in r.text
    assert len(r.sources) == 2


async def test_hanging_provider_times_out_within_deadline(make_pipeline):
    pipe = make_pipeline(faults=Faults(hang_rate=1.0, hang_s=30), scale=1.0)
    t = await pipe.handle("How many attention heads does the Transformer use?", deadline_s=3)
    assert t.status == "timeout" and t.total_ms < 4000
    await pipe.provider.aclose()


async def test_tool_failure_is_an_error_not_an_answer(make_pipeline):
    pipe = make_pipeline(tool=RetrievalTool(2.0, failure_rate=1.0))
    t = await pipe.handle("How many attention heads does the Transformer use?")
    assert (t.status, t.outcome) == ("error", "tool_failed") and t.attempts == []
    await pipe.provider.aclose()


async def test_person_giving_up_is_recorded(make_pipeline, tmp_path):
    pipe = make_pipeline(scale=1.0)
    task = asyncio.create_task(pipe.handle("How many attention heads does the Transformer use?"))
    await asyncio.sleep(0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    row = _rows(tmp_path)[0]
    assert row["status"] == "timeout" and row["first_token_ms"] == ""
    assert pipe.gate.in_flight == 0 and pipe.gate.waiting == 0
    await pipe.provider.aclose()


async def test_burst_under_halved_limit_is_bounded_and_learned(make_pipeline, tmp_path):
    # Provider limits are half of what the client was configured with, and nobody told the client.
    pipe = make_pipeline("rate_limited", scale=1.0)
    qs = ["How many attention heads does the Transformer use?"] * 12
    traces = await asyncio.gather(*(pipe.handle(q, deadline_s=8) for q in qs))
    assert len(_rows(tmp_path)) == 12
    assert {t.status for t in traces} <= {"ok", "rate_limited", "timeout"}
    assert pipe.gate.peak_in_flight <= pipe.s.max_in_flight
    assert pipe.limiter.tpm.capacity == 4000                        # learned from the response headers
    n429 = sum(a["status_code"] == 429 for t in traces for a in t.attempts)
    assert n429 <= pipe.s.max_in_flight                             # at most one round before learning
    assert any(t.outcome == "wait_exceeds_deadline" for t in traces)  # then turned away at arrival
    await pipe.provider.aclose()


# --- decide step: widening, refusing, degraded mode, queue capacity ----------------------------

class FixedTool:
    """Returns one Transformer passage with a chosen re-rank score, so the decide branches are deterministic."""

    def __init__(self, first: float, wider: float | None = None):
        self.first, self.wider, self.calls = first, wider, []

    async def __call__(self, query, setting_name, pieces=None, rerank=None, timeout_s=None, fetch=None,
                       papers=None):
        self.calls.append({"pieces": pieces, "rerank": rerank, "fetch": fetch})
        p = _p("attention", PASSAGES[0].chunk.text)
        p.rerank = self.wider if fetch and self.wider is not None else self.first
        return RetrievalResult(setting_name, query, [p], 1.0, 1.0, [], True, 1.0)


async def test_weak_evidence_widens_the_search_once(make_pipeline):
    tool = FixedTool(first=-3.0, wider=2.0)
    pipe = make_pipeline(tool=tool)
    t = await pipe.handle("How many attention heads does the Transformer use?")
    assert t.status == "ok"
    assert [c["fetch"] for c in tool.calls] == [None, RETRIEVAL_SETTINGS["small_k6"].fetch * 2]
    widened = [e for e in t.events if e["name"] == "widened"]
    assert [(e["before"], e["after"]) for e in widened] == [(-3.0, 2.0)]
    assert "retrieve_wider" in [s["name"] for s in t.spans]
    await pipe.provider.aclose()


async def test_very_weak_evidence_is_refused_before_the_model(make_pipeline):
    tool = FixedTool(first=-9.0)
    pipe = make_pipeline(tool=tool)
    t = await pipe.handle("How many attention heads does the Transformer use?")
    assert (t.status, t.outcome) == ("ok", "refused_no_evidence")
    assert len(tool.calls) == 1 and t.attempts == []
    await pipe.provider.aclose()


async def test_degraded_mode_cuts_pieces_but_keeps_reranking(make_pipeline):
    pipe = make_pipeline(setting="medium_k4_rr")
    pipe.limiter.tpm_headroom = lambda: 0.1          # below degrade_tpm_headroom
    t = await pipe.handle("How many attention heads does the Transformer use?")
    assert t.degraded and t.status == "ok"
    retrieve = [s for s in t.spans if s["name"] == "retrieve"][0]
    assert retrieve["reranked"] and retrieve["n"] == DEGRADED_PIECES
    assert "retrieve_wider" not in [s["name"] for s in t.spans]
    # Re-ranking still refuses off-topic questions while degraded (D13).
    off = await pipe.handle("What is the capital of France?")
    assert off.degraded and off.outcome == "refused_no_evidence" and off.attempts == []
    await pipe.provider.aclose()


async def test_degraded_mode_can_be_switched_off(make_pipeline):
    pipe = make_pipeline()
    pipe.allow_degraded = False                      # as in the retrieval-settings comparison
    pipe.limiter.tpm_headroom = lambda: 0.0
    t = await pipe.handle("How many attention heads does the Transformer use?")
    assert not t.degraded
    await pipe.provider.aclose()


async def test_full_queue_turns_requests_away_and_records_them(make_pipeline, tmp_path):
    pipe = make_pipeline(scale=1.0)
    pipe.gate = ModelGate(pipe.limiter, max_in_flight=1, queue_max=1, expected_service_s=pipe.s.expected_service_s)
    traces = await asyncio.gather(*(pipe.handle("How many attention heads does the Transformer use?",
                                                deadline_s=60) for _ in range(5)))
    full = [t for t in traces if t.outcome == "queue_full"]
    assert full and all(t.status == "rate_limited" and t.first_token_ms is None and t.attempts == []
                        for t in full)
    assert len(_rows(tmp_path)) == 5
    assert pipe.gate.peak_in_flight <= 1 and pipe.gate.peak_waiting <= 1
    assert pipe.gate.in_flight == 0 and pipe.gate.waiting == 0
    await pipe.provider.aclose()
