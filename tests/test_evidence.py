from incident_agents.evidence import EvidenceStore, extract_pivots, fingerprint


def test_ids_are_readable_and_counted_per_kind_and_subject():
    store = EvidenceStore()
    a = store.add("inv1", "log", "fivo-api", "first", {"message": "a"})
    b = store.add("inv1", "log", "fivo-api", "second", {"message": "b"})
    c = store.add("inv1", "log", "fivo-worker", "third", {"message": "c"})
    d = store.add("inv1", "deploy", "fivo-api", "deploy", {"sha": "abc"})
    other = store.add("inv2", "log", "fivo-api", "other investigation", {"message": "a"})
    assert [a.id, b.id, c.id, d.id] == ["log:fivo-api:0001", "log:fivo-api:0002", "log:fivo-worker:0001", "deploy:fivo-api:0001"]
    assert other.id == "log:fivo-api:0001" and store.count("inv1") == 4 and store.count("inv2") == 1


def test_same_observation_keeps_its_first_id():
    store = EvidenceStore()
    first = store.add("inv", "log", "api", "line", {"message": "boom", "ts": "t1"}, query_id="q1")
    again = store.add("inv", "log", "api", "line", {"ts": "t1", "message": "boom"}, query_id="q2")
    assert again.id == first.id and again.query_id == "q1" and store.count("inv") == 1


def test_citations_are_checked_against_what_exists():
    store = EvidenceStore()
    real = store.add("inv", "metric", "api", "m", {"v": 1})
    known, invented = store.check_citations("inv", [real.id, "log:api:0099", real.id, "made-up"])
    assert known == [real.id] and invented == ["log:api:0099", "made-up"]
    assert store.check_citations("other", [real.id]) == ([], [real.id])


def test_pivots_link_evidence_across_sources():
    store = EvidenceStore()
    log = store.add("inv", "log", "api", "error line", {"m": 1}, pivots={"trace_id": "t-91", "user_id": "104"})
    trace = store.add("inv", "trace", "api", "span", {"m": 2}, pivots={"trace_id": "t-91"})
    store.add("inv", "log", "api", "unrelated", {"m": 3}, pivots={"trace_id": "t-07"})
    assert [e.id for e in store.find("inv", pivot=("trace_id", "t-91"))] == [log.id, trace.id]
    assert store.pivots("inv")["trace_id"] == {"t-07": ["log:api:0002"], "t-91": [log.id, trace.id]}
    assert store.get("inv", log.id).pivots == {"trace_id": "t-91", "user_id": "104"}
    assert [e.id for e in store.find("inv", kind="trace")] == [trace.id]
    assert [e.id for e in store.find("inv", text="unrelated")] == ["log:api:0002"]


def test_partial_results_are_marked_and_survive_a_restart(tmp_path):
    path = str(tmp_path / "evidence.db")
    store = EvidenceStore(path)
    ev = store.add("inv", "logagg", "api", "counts", {"rows": [1]}, complete=False, ts="2026-11-10T08:00:00Z")
    store.close()
    again = EvidenceStore(path).get("inv", ev.id)
    assert again == ev and again.digest() == {"id": ev.id, "summary": "counts", "ts": "2026-11-10T08:00:00Z", "partial": True}


def test_pivot_extraction_and_error_fingerprints():
    row = {"trace_id": "t1", "user_id": 104, "commit": "abc123", "level": "error", "status": 500, "ok": True,
           "message": "duplicate key value violates unique constraint \"ix_txn\" for user 104"}
    pivots = extract_pivots(row)
    assert pivots["trace_id"] == "t1" and pivots["user_id"] == "104" and pivots["sha"] == "abc123"
    assert "status" not in pivots and pivots["error_fingerprint"].startswith("fp_")
    same = fingerprint('Duplicate key value violates unique constraint "ix_other" for user 7')
    assert pivots["error_fingerprint"] == same != fingerprint("connection reset by peer")
    assert "error_fingerprint" not in extract_pivots({"level": "info", "message": "ok"})
