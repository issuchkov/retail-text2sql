"""BigQuery executor: dry run, byte cap, retries, error classification (SPEC §4.4, §6)."""

import datetime as dt
from decimal import Decimal

import pytest
from google.api_core import exceptions as gexc
from google.cloud import bigquery

from retail_agent.query.executor import BigQueryExecutor, QueryFailed
from retail_agent.query.guard import PreparedQuery
from retail_agent.query.scope import QueryParam

QUERY = PreparedQuery(
    sql="SELECT 1 AS x LIMIT 10",
    params=[
        QueryParam("scope_categories", "STRING", ["Jeans"], array=True),
        QueryParam("pseudonym_salt", "STRING", "s"),
    ],
    tables={"products"},
)


class FakeJob:
    def __init__(self, rows=(), bytes_processed=1_000, error=None):
        self.cancelled = False
        self._rows = list(rows)
        self.total_bytes_processed = bytes_processed
        self.total_bytes_billed = bytes_processed
        self._error = error

    def cancel(self):
        self.cancelled = True

    def result(self, timeout=None, max_results=None):
        if self._error:
            raise self._error
        return FakeRows(self._rows[:max_results])


class FakeRows(list):
    @property
    def schema(self):
        names = list(self[0].keys()) if self else []
        return [bigquery.SchemaField(n, "STRING") for n in names]


class FakeClient:
    """Returns scripted outcomes: a dry-run outcome then a run outcome per call."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def query(self, sql, job_config=None, location=None, timeout=None):
        self.calls.append((sql, job_config))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def row(**values):
    return bigquery.Row(tuple(values.values()), {k: i for i, k in enumerate(values)})


def make(client, **kw):
    sleeps = []
    ex = BigQueryExecutor(
        client,
        max_bytes_billed=10_000,
        timeout_s=5,
        location="US",
        retries=2,
        sleep=sleeps.append,
        **kw,
    )
    return ex, sleeps


def test_dry_run_then_run_with_parameters_and_cap() -> None:
    client = FakeClient([FakeJob(bytes_processed=500), FakeJob([row(x=1), row(x=2)], 500)])
    ex, _ = make(client)
    result = ex.run(QUERY, max_rows=10)

    assert result.rows == [{"x": 1}, {"x": 2}]
    assert result.columns == ["x"]
    assert result.bytes_processed == 500
    assert not result.row_limit_reached
    (_, dry_cfg), (_, run_cfg) = client.calls
    assert dry_cfg.dry_run and not dry_cfg.use_query_cache
    assert not run_cfg.dry_run
    assert run_cfg.maximum_bytes_billed == 10_000
    assert run_cfg.labels == {"app": "retail-agent"}
    params = {p.name: p for p in run_cfg.query_parameters}
    assert isinstance(params["scope_categories"], bigquery.ArrayQueryParameter)
    assert params["scope_categories"].values == ["Jeans"]
    assert isinstance(params["pseudonym_salt"], bigquery.ScalarQueryParameter)


def test_estimate_over_cap_is_rejected_before_running() -> None:
    client = FakeClient([FakeJob(bytes_processed=50_000)])
    ex, _ = make(client)
    with pytest.raises(QueryFailed) as err:
        ex.run(QUERY, max_rows=10)
    assert err.value.kind == "too_expensive"
    assert err.value.model_can_fix
    assert len(client.calls) == 1


def test_invalid_query_is_returned_for_self_correction() -> None:
    bad = gexc.BadRequest(
        "Unrecognized name: revenue at [1:8]", errors=[{"reason": "invalidQuery"}]
    )
    ex, sleeps = make(FakeClient([bad]))
    with pytest.raises(QueryFailed) as err:
        ex.run(QUERY, max_rows=10)
    assert err.value.kind == "invalid_query"
    assert err.value.model_can_fix
    assert "Unrecognized name: revenue" in err.value.message
    assert sleeps == []


def test_transient_errors_are_retried() -> None:
    client = FakeClient(
        [
            gexc.ServiceUnavailable("backend"),
            FakeJob(bytes_processed=10),
            gexc.InternalServerError("oops"),
            FakeJob([row(x=1)], 10),
        ]
    )
    ex, sleeps = make(client)
    assert ex.run(QUERY, max_rows=10).rows == [{"x": 1}]
    assert len(sleeps) == 2


def test_transient_errors_during_result_are_retried() -> None:
    client = FakeClient(
        [
            FakeJob(bytes_processed=10),
            FakeJob(error=gexc.TooManyRequests("slow down")),
            FakeJob([row(x=1)], 10),
        ]
    )
    ex, sleeps = make(client)
    assert ex.run(QUERY, max_rows=10).rows == [{"x": 1}]
    assert len(sleeps) == 1


def test_persistent_outage_becomes_unavailable() -> None:
    client = FakeClient([gexc.ServiceUnavailable("down")] * 3)
    ex, sleeps = make(client)
    with pytest.raises(QueryFailed) as err:
        ex.run(QUERY, max_rows=10)
    assert err.value.kind == "unavailable"
    assert not err.value.model_can_fix
    assert len(sleeps) == 2


def test_permission_error_is_not_retried() -> None:
    denied = gexc.Forbidden("Access Denied", errors=[{"reason": "accessDenied"}])
    ex, sleeps = make(FakeClient([denied]))
    with pytest.raises(QueryFailed) as err:
        ex.run(QUERY, max_rows=10)
    assert err.value.kind == "permission"
    assert sleeps == []


def test_bytes_billed_limit_from_bigquery_is_too_expensive() -> None:
    over = gexc.BadRequest("limit", errors=[{"reason": "bytesBilledLimitExceeded"}])
    ex, _ = make(FakeClient([FakeJob(bytes_processed=10), over]))
    with pytest.raises(QueryFailed) as err:
        ex.run(QUERY, max_rows=10)
    assert err.value.kind == "too_expensive"


def test_values_are_json_friendly_and_truncation_flagged() -> None:
    rows = [
        row(
            amount=Decimal("12.50"),
            at=dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.UTC),
            day=dt.date(2026, 1, 2),
            n=3,
        )
    ] * 3
    ex, _ = make(FakeClient([FakeJob(bytes_processed=10), FakeJob(rows, 10)]))
    result = ex.run(QUERY, max_rows=3)
    assert result.rows[0] == {
        "amount": 12.5,
        "at": "2026-01-02T03:04:05+00:00",
        "day": "2026-01-02",
        "n": 3,
    }
    assert result.row_limit_reached


def test_empty_result_is_not_an_error() -> None:
    ex, _ = make(FakeClient([FakeJob(bytes_processed=10), FakeJob([], 10)]))
    result = ex.run(QUERY, max_rows=10)
    assert result.rows == []
    assert result.row_count == 0


def test_a_job_that_fails_while_waiting_is_cancelled_before_the_retry() -> None:
    stuck = FakeJob(error=gexc.DeadlineExceeded("timed out"))
    client = FakeClient([FakeJob(bytes_processed=10), stuck, FakeJob([row(x=1)], 10)])
    ex, _ = make(client)
    assert ex.run(QUERY, max_rows=10).rows == [{"x": 1}]
    assert stuck.cancelled  # not left running (and billed) in BigQuery
