"""Saved-report library and the deletion flow (SPEC §5, AC-5)."""

import datetime as dt

import pytest

from retail_agent.reports.deletion import MAX_BULK, execute_deletion, plan_deletion
from retail_agent.reports.store import ReportStore, Selector


class Clock:
    def __init__(self) -> None:
        self.now = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)

    def __call__(self) -> dt.datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(clock: Clock) -> ReportStore:
    return ReportStore(":memory:", clock=clock)


def save(store: ReportStore, owner="bob", conv="c1", title="Q1 review", body="", entities=()):
    return store.save(owner, conv, title=title, body=body, entities=list(entities))


def test_save_and_list_own_reports_only(store: ReportStore) -> None:
    mine = save(store, title="Jeans Q1")
    save(store, owner="alice", title="Dresses Q1")
    assert [r.id for r in store.list("bob")] == [mine.id]
    assert store.get("alice", mine.id) is None


def test_mentions_match_title_body_and_entities_case_insensitively(store: ReportStore) -> None:
    a = save(store, title="Levi's deep dive")
    b = save(store, body="Revenue of LEVI'S grew 4%")
    c = save(store, entities=["Levi's"])
    save(store, title="Calvin Klein")
    save(store, owner="alice", title="Levi's for Alice")
    found = store.resolve("bob", Selector(mentions="levi's"), conversation_id="c1")
    assert {r.id for r in found} == {a.id, b.id, c.id}


def test_this_conversation_and_older_than(store: ReportStore, clock: Clock) -> None:
    old = save(store, conv="c0")
    clock.now += dt.timedelta(days=40)
    new = save(store, conv="c1")
    assert [r.id for r in store.resolve("bob", Selector(this_conversation=True), "c1")] == [new.id]
    cutoff = dt.date(2026, 1, 15)
    assert [r.id for r in store.resolve("bob", Selector(older_than=cutoff), "c1")] == [old.id]


def test_ids_of_other_users_are_never_resolved(store: ReportStore) -> None:
    mine = save(store)
    theirs = save(store, owner="alice")
    found = store.resolve("bob", Selector(ids=[mine.id, theirs.id]), "c1")
    assert [r.id for r in found] == [mine.id]


def test_empty_selector_is_invalid() -> None:
    with pytest.raises(ValueError):
        Selector()


def test_delete_is_permanent_and_only_own(store: ReportStore) -> None:
    mine = save(store)
    theirs = save(store, owner="alice")
    assert store.delete("bob", [mine.id, theirs.id]) == [mine.id]
    assert store.list("bob") == []
    assert store.get("alice", theirs.id) is not None
    rows = store._db.execute("SELECT id FROM reports").fetchall()
    assert [r["id"] for r in rows] == [theirs.id]  # the row is gone, not flagged


def test_plan_with_no_match_needs_no_confirmation(store: ReportStore) -> None:
    plan = plan_deletion(store, "bob", Selector(mentions="nothing"), "c1")
    assert plan.status == "nothing_matched"
    assert plan.ids == []


def test_plan_over_bulk_limit_asks_to_narrow(store: ReportStore) -> None:
    for i in range(MAX_BULK + 1):
        save(store, title=f"Client X report {i}")
    plan = plan_deletion(store, "bob", Selector(mentions="client x"), "c1")
    assert plan.status == "too_many"
    assert plan.ids == []


def test_plan_lists_reports_and_phrase(store: ReportStore) -> None:
    a = save(store, title="Client X Q1")
    b = save(store, title="Client X Q2")
    plan = plan_deletion(store, "bob", Selector(mentions="client x"), "c1")
    assert plan.status == "needs_confirmation"
    assert plan.ids == [a.id, b.id]
    assert plan.phrase == "delete 2"
    assert [item["title"] for item in plan.items] == ["Client X Q1", "Client X Q2"]


@pytest.mark.parametrize("typed", ["", "yes", "delete", "delete 3", "delete all", "y"])
def test_wrong_phrase_cancels_and_deletes_nothing(store: ReportStore, typed: str) -> None:
    save(store, title="Client X Q1")
    save(store, title="Client X Q2")
    plan = plan_deletion(store, "bob", Selector(mentions="client x"), "c1")
    outcome = execute_deletion(store, "bob", plan.ids, typed=typed)
    assert outcome.status == "cancelled"
    assert len(store.list("bob")) == 2


@pytest.mark.parametrize("typed", ["delete 2", "  DELETE   2 "])
def test_exact_phrase_deletes_exactly_the_previewed_reports(store: ReportStore, typed: str) -> None:
    a = save(store, title="Client X Q1")
    b = save(store, title="Client X Q2")
    plan = plan_deletion(store, "bob", Selector(mentions="client x"), "c1")
    late = save(store, title="Client X Q3")  # appears after the preview
    outcome = execute_deletion(store, "bob", plan.ids, typed=typed)
    assert outcome.status == "deleted"
    assert outcome.ids == [a.id, b.id]
    assert [r.id for r in store.list("bob")] == [late.id]


def test_reports_already_gone_are_reported(store: ReportStore) -> None:
    a = save(store, title="Client X Q1")
    b = save(store, title="Client X Q2")
    plan = plan_deletion(store, "bob", Selector(mentions="client x"), "c1")
    store.delete("bob", [a.id])
    outcome = execute_deletion(store, "bob", plan.ids, typed="delete 2")
    assert outcome.status == "deleted"
    assert outcome.ids == [b.id]


def test_store_persists_to_file(tmp_path, clock: Clock) -> None:
    path = tmp_path / "reports.sqlite"
    first = ReportStore(path, clock=clock)
    r = save(first, title="Persisted", entities=["Jeans"])
    second = ReportStore(path, clock=clock)
    got = second.get("bob", r.id)
    assert got is not None
    assert got.entities == ["Jeans"]


def test_audit_records_the_flow_without_report_content(store: ReportStore) -> None:
    a = save(store, title="Client X secret plan", body="confidential numbers")
    plan = plan_deletion(store, "bob", Selector(mentions="client x"), "c1", trace_id="t1")
    execute_deletion(store, "bob", plan.ids, typed="delete 1", trace_id="t2")
    log = store.audit_log("bob")
    assert [(e.action, e.report_ids, e.trace_id) for e in log] == [
        ("delete_preview", [a.id], "t1"),
        ("delete_confirmed", [a.id], "t2"),
    ]
    dumped = repr(log)
    assert "secret" not in dumped
    assert "confidential" not in dumped


def test_audit_records_cancel_and_empty_match(store: ReportStore) -> None:
    a = save(store, title="Client X")
    plan_deletion(store, "bob", Selector(mentions="nothing"), "c1", trace_id="t0")
    plan = plan_deletion(store, "bob", Selector(mentions="client x"), "c1", trace_id="t1")
    execute_deletion(store, "bob", plan.ids, typed="yes", trace_id="t2")
    assert [(e.action, e.report_ids) for e in store.audit_log("bob")] == [
        ("delete_nothing_matched", []),
        ("delete_preview", [a.id]),
        ("delete_cancelled", [a.id]),
    ]
    assert store.audit_log("alice") == []
