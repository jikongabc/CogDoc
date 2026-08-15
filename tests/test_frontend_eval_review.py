from cogdoc.frontend.app import _eval_candidate_identity


def test_eval_candidate_identity_keeps_exact_half_open_span():
    candidate = {
        "chunk_id": "c1",
        "parent_chunk_id": "p1",
        "source": "policy.pdf",
        "source_sha256": "sha-1",
        "_selected_start": 4,
        "_selected_end": 11,
    }

    assert _eval_candidate_identity(candidate, include_span=True) == {
        "chunk_id": "c1",
        "parent_chunk_id": "p1",
        "source": "policy.pdf",
        "source_sha256": "sha-1",
        "start": 4,
        "end": 11,
    }


def test_eval_candidate_identity_omits_empty_parent_and_optional_span():
    candidate = {
        "chunk_id": "c1",
        "parent_chunk_id": "",
        "source": "policy.pdf",
        "source_sha256": "sha-1",
    }

    assert _eval_candidate_identity(candidate, include_span=False) == {
        "chunk_id": "c1",
        "source": "policy.pdf",
        "source_sha256": "sha-1",
    }
