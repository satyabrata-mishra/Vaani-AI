from app import config
from app.config import RESULTS_COLUMNS, RETRIEVAL_SETTINGS, load_settings


def test_results_columns_match_brief_exactly():
    assert ",".join(RESULTS_COLUMNS) == (
        "request_id,scenario,source,start_time,first_token_ms,total_ms,status,retries,"
        "tokens_in,tokens_out,retrieval_setting,answer_grade"
    )


def test_api_key_never_in_repr_or_public_dict():
    s = load_settings(groq_api_key="gsk_secret_for_test")
    assert "gsk_secret_for_test" not in repr(s)
    assert "groq_api_key" not in s.public_dict()


def test_at_least_three_retrieval_settings_that_differ():
    assert len(RETRIEVAL_SETTINGS) >= 3
    sizes = {s.piece_tokens for s in RETRIEVAL_SETTINGS.values()}
    assert len(sizes) >= 3
    assert config.DEFAULT_RETRIEVAL in RETRIEVAL_SETTINGS
    for s in RETRIEVAL_SETTINGS.values():
        assert s.fetch >= s.pieces
        assert s.overlap_tokens < s.piece_tokens
