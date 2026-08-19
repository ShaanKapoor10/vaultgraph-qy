

def test_citations_are_fetched_in_one_call(monkeypatch):
    """
    One call regardless of citation count. A well-cited answer previously cost
    a round trip per citation.
    """
    from brahmastra import rag

    calls = []

    def bulk(ids):
        calls.append(list(ids))
        return {"n1": {"title": "First"}, "n2": {"title": "Second"}}

    monkeypatch.setattr(rag.db, "get_notes_by_ids", bulk)

    out = rag._citations({"n1", "n2", "n3"})

    assert len(calls) == 1, f"expected one bulk call, got {len(calls)}"
    by_id = {c["note_id"]: c["title"] for c in out}
    assert by_id["n1"] == "First"
    # A missing note still yields a citation, titled with its id: the fact was
    # genuinely extracted from it, so dropping it would hide the gap.
    assert by_id["n3"] == "n3"
