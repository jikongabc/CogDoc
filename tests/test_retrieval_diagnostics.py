from cogdoc.service.retrieval_diagnostics import run_retrieval_diagnostics
from cogdoc.service.retrieval_pipeline import RetrievalQuery
from cogdoc.tools.retriever.scope import RetrievalScope


def _doc(chunk_id, *, retrieval=None, text=None):
    return {
        "text": text or f"text {chunk_id}",
        "meta": {
            "chunk_id": chunk_id,
            "source": "paper.pdf",
            "source_sha256": "a" * 64,
            "source_type": "document",
            "chunk_type": "paragraph",
        },
        "retrieval": retrieval or {},
    }


class _Engine:
    def search_many_channels(self, queries, top_k=3, scope=None):
        return [
            {
                "vector": [_doc("shared", retrieval={"distance": 0.1})],
                "bm25": [
                    _doc("shared", retrieval={"bm25_score": 20.0}),
                    _doc("lex"),
                ],
            }
            for _ in queries
        ]


class _Knowledge:
    def search_many_channels(self, kb_id, queries, top_k=3, scope=None):
        return [{"embedding": [], "lexical": []} for _ in queries]


def test_diagnostics_exposes_routes_rrf_contributions_and_gate_decision():
    result = run_retrieval_diagnostics(
        engine=_Engine(),
        derived_knowledge_retriever=_Knowledge(),
        retrieval_feedback_store=None,
        kb_id="kb",
        query="question",
        queries=[RetrievalQuery("question", is_original=True)],
        top_k=3,
        scope=RetrievalScope(include_derived_knowledge=True),
        rerank=False,
    )

    assert {row["channel"] for row in result["routes"]} == {
        "rag_vector",
        "rag_bm25",
    }
    assert result["fused"][0]["chunk_id"] == "shared"
    assert set(result["fused"][0]["retrieval"]["channel_contributions"]) == {
        "rag_vector",
        "rag_bm25",
    }
    assert result["decision"]["supported"] is True
    assert result["latency_ms"]["total"] >= 0


def test_diagnostics_uses_query_for_exact_term_support():
    class ExactTermEngine:
        def search_many_channels(self, queries, top_k=3, scope=None):
            return [
                {
                    "vector": [
                        _doc(
                            "definition",
                            retrieval={"distance": 0.95},
                            text="ACM，全称 ACM-ICPC 国际大学生程序设计竞赛。",
                        )
                    ],
                    "bm25": [],
                }
                for _ in queries
            ]

    result = run_retrieval_diagnostics(
        engine=ExactTermEngine(),
        derived_knowledge_retriever=_Knowledge(),
        retrieval_feedback_store=None,
        kb_id="kb",
        query="ICPC全称是什么",
        queries=[RetrievalQuery("ICPC全称是什么", is_original=True)],
        top_k=3,
        scope=RetrievalScope(include_derived_knowledge=True),
        rerank=False,
    )

    assert result["decision"]["supported"] is False
    assert result["decision"]["score"] < 1.0
    assert (
        result["decision"]["reason"]
        == "lexical_coverage_requires_verification"
    )
