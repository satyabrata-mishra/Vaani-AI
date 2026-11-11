import json

from fastapi.testclient import TestClient

from app.simulator.runner import StandInThread


def test_ask_streams_verified_answer_then_done(monkeypatch, tmp_path):
    import app.infra.metrics as metrics
    monkeypatch.setattr(metrics, "RESULTS_CSV", tmp_path / "results.csv")
    monkeypatch.setattr(metrics, "TRACES_FILE", tmp_path / "traces.jsonl")
    with StandInThread("normal", scale=0.05) as st:
        monkeypatch.setenv("BACKEND", "standin")
        monkeypatch.setenv("STANDIN_BASE_URL", st.api_base)
        from app.main import app
        with TestClient(app) as client:
            r = client.post("/ask", json={"question": "How many attention heads does the Transformer use?",
                                          "retrieval_setting": "small_k6"})
            events = [json.loads(l[5:]) for l in r.text.splitlines() if l.startswith("data:")]
            assert r.status_code == 200 and events[-1]["type"] == "done"
            assert events[-1]["status"] == "ok" and events[-1]["sources"]
            assert any(e["type"] == "token" for e in events[:-1])
            assert client.post("/ask", json={"question": "x?", "retrieval_setting": "nope"}).status_code == 400
    assert (tmp_path / "results.csv").exists()


def test_request_validation_and_health(monkeypatch, tmp_path):
    import app.infra.metrics as metrics
    monkeypatch.setattr(metrics, "RESULTS_CSV", tmp_path / "results.csv")
    monkeypatch.setattr(metrics, "TRACES_FILE", tmp_path / "traces.jsonl")
    monkeypatch.setenv("BACKEND", "standin")
    from app.main import app
    with TestClient(app) as client:   # nothing here reaches the model, so no stand-in is needed
        for bad in ({}, {"question": ""}, {"question": "x" * 2001},
                    {"question": "Heads?", "deadline_s": 0}, {"question": "Heads?", "deadline_s": 121}):
            assert client.post("/ask", json=bad).status_code == 422, bad
        r = client.post("/ask", json={"question": "   "})
        done = [json.loads(l[5:]) for l in r.text.splitlines() if l.startswith("data:")][-1]
        assert (done["status"], done["outcome"]) == ("error", "invalid_input")
        assert done["first_token_ms"] is None
        h = client.get("/health").json()
        assert h["source"] == "stand-in" and h["in_flight"] == 0 and h["queue_waiting"] == 0
        assert h["daily_blocked"] is None   # otherwise the exhausted limit, "RPD" or "TPD"
