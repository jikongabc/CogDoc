from cogdoc.tools.retriever.retrieval_text import retrieval_text


def test_retrieval_text_indexes_source_section_context_and_child_body():
    indexed = retrieval_text(
        {
            "text": "正文证据",
            "meta": {
                "source": "paper.pdf",
                "section_path": "Methods > Training",
                "context": "前文：实验设置",
            },
        }
    )

    assert indexed == (
        "来源：paper.pdf\n章节：Methods > Training\n\n前文：实验设置\n\n正文证据"
    )


def test_retrieval_text_keeps_sparse_legacy_docs_compatible():
    assert retrieval_text({"text": "正文", "meta": {}}) == "正文"


def test_retrieval_text_contextualizes_page_span_and_structured_content_type():
    indexed = retrieval_text(
        {
            "text": "| key | value |",
            "meta": {
                "source": "policy.pdf",
                "page_start": 2,
                "page_end": 3,
                "chunk_type": "table",
            },
        }
    )

    assert indexed.startswith("来源：policy.pdf\n页码：2-3\n内容类型：table\n\n")


def test_retrieval_text_does_not_add_internal_source_id_to_derived_knowledge():
    assert (
        retrieval_text(
            {
                "text": "审核通过的补充规则",
                "meta": {
                    "source": "knowledge:K1",
                    "source_type": "derived_knowledge",
                    "context": "来源说明",
                },
            }
        )
        == "来源说明\n\n审核通过的补充规则"
    )
