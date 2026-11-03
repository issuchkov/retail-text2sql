"""Runs prepared queries on BigQuery with cost bounds and classified errors (SPEC §4.4, §6).

Every query is dry-run first; an estimate above the byte cap is rejected before any bytes
are billed, and the real run carries `maximum_bytes_billed` as a second line of defence.
Transient failures are retried with backoff; everything else becomes a `QueryFailed` whose
`kind` says whether the model can fix it by rewriting the SQL.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, TypeVar

from google.api_core import exceptions as gexc
from google.cloud import bigquery

from retail_agent.config import Settings
from retail_agent.query.guard import PreparedQuery
from retail_agent.query.scope import QueryParam

T = TypeVar("T")

_TRANSIENT = (
    gexc.TooManyRequests,
    gexc.InternalServerError,
    gexc.BadGateway,
    gexc.ServiceUnavailable,
    gexc.GatewayTimeout,
    gexc.DeadlineExceeded,
    ConnectionError,
    TimeoutError,
)
_TRANSIENT_REASONS = {"rateLimitExceeded", "backendError", "internalError", "jobRateLimitExceeded"}
_TOO_EXPENSIVE_REASONS = {"bytesBilledLimitExceeded"}

# kind -> can the model fix it by changing the SQL?
_MODEL_CAN_FIX = {
    "invalid_query": True,
    "too_expensive": True,
    "permission": False,
    "unavailable": False,
}


class QueryFailed(Exception):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.message = message

    @property
    def model_can_fix(self) -> bool:
        return _MODEL_CAN_FIX[self.kind]


@dataclass(frozen=True)
class QueryResult:
    sql: str
    columns: list[str]
    rows: list[dict[str, Any]]
    row_limit_reached: bool  # hit the row cap: the result may or may not be complete
    bytes_processed: int
    bytes_billed: int
    duration_ms: int

    @property
    def row_count(self) -> int:
        return len(self.rows)


class BigQueryExecutor:
    def __init__(
        self,
        client: bigquery.Client,
        *,
        max_bytes_billed: int,
        timeout_s: float,
        location: str,
        retries: int = 2,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client
        self._max_bytes = max_bytes_billed
        self._timeout = timeout_s
        self._location = location
        self._retries = retries
        self._sleep = sleep

    def run(self, query: PreparedQuery, *, max_rows: int) -> QueryResult:
        started = time.monotonic()
        estimate = self.dry_run(query)
        if estimate > self._max_bytes:
            raise QueryFailed(
                "too_expensive",
                f"The query would scan {_human(estimate)}, above the {_human(self._max_bytes)} "
                "limit. Select fewer columns or filter by a narrower date range.",
            )

        config = self._config(query, dry_run=False)

        def execute() -> tuple[Any, Any]:
            job = self._client.query(
                query.sql, job_config=config, location=self._location, timeout=self._timeout
            )
            try:
                return job, job.result(timeout=self._timeout, max_results=max_rows)
            except Exception:
                _cancel(job)  # a retry starts a new job; do not leave this one running
                raise

        job, rows_iter = self._with_retries(execute)
        columns = [f.name for f in rows_iter.schema]
        rows = [{k: _jsonable(v) for k, v in r.items()} for r in rows_iter]
        return QueryResult(
            sql=query.sql,
            columns=columns,
            rows=rows,
            row_limit_reached=len(rows) >= max_rows,
            bytes_processed=job.total_bytes_processed or 0,
            bytes_billed=job.total_bytes_billed or 0,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    def dry_run(self, query: PreparedQuery) -> int:
        config = self._config(query, dry_run=True)
        job = self._with_retries(
            lambda: self._client.query(
                query.sql, job_config=config, location=self._location, timeout=self._timeout
            )
        )
        return job.total_bytes_processed or 0

    def _config(self, query: PreparedQuery, *, dry_run: bool) -> bigquery.QueryJobConfig:
        config = bigquery.QueryJobConfig(
            query_parameters=[_to_bq(p) for p in query.params],
            labels={"app": "retail-agent"},
            use_legacy_sql=False,
        )
        if dry_run:
            config.dry_run = True
            config.use_query_cache = False
        else:
            config.maximum_bytes_billed = self._max_bytes
        return config

    def _with_retries(self, fn: Callable[[], T]) -> T:
        attempt = 0
        while True:
            try:
                return fn()
            except Exception as e:  # classified below; nothing escapes unclassified
                failure = _classify(e)
                if failure is not None:
                    raise failure from e
                if attempt >= self._retries:
                    raise QueryFailed(
                        "unavailable",
                        "BigQuery is temporarily unavailable. Try again in a minute.",
                    ) from e
                attempt += 1
                self._sleep(min(8.0, 0.5 * 2**attempt) + random.uniform(0, 0.25))


def make_executor(settings: Settings) -> BigQueryExecutor:
    client = bigquery.Client(project=settings.google_cloud_project)
    return BigQueryExecutor(
        client,
        max_bytes_billed=settings.max_bytes_billed,
        timeout_s=settings.query_timeout_s,
        location=settings.bq_location,
    )


def _cancel(job: Any) -> None:
    # best effort: the job may have finished or never started
    with contextlib.suppress(Exception):
        job.cancel()


def _classify(error: Exception) -> QueryFailed | None:
    """Return a non-retryable failure, or None if the error is transient."""
    reasons = {e.get("reason") for e in getattr(error, "errors", None) or [] if isinstance(e, dict)}
    if reasons & _TOO_EXPENSIVE_REASONS:
        return QueryFailed(
            "too_expensive",
            "The query exceeded the bytes limit. Select fewer columns or filter by date.",
        )
    if isinstance(error, _TRANSIENT) or reasons & _TRANSIENT_REASONS:
        return None
    if isinstance(error, gexc.Forbidden) and "quotaExceeded" in reasons:
        return QueryFailed("unavailable", "BigQuery quota is exhausted for now.")
    if isinstance(error, (gexc.BadRequest, gexc.NotFound)):
        return QueryFailed("invalid_query", _message(error))
    if isinstance(error, (gexc.Forbidden, gexc.Unauthorized)):
        return QueryFailed(
            "permission", "The service has no permission to run this query (check credentials)."
        )
    return QueryFailed("unavailable", f"Unexpected BigQuery error ({type(error).__name__}).")


def _message(error: Exception) -> str:
    text = getattr(error, "message", None) or str(error)
    return text.splitlines()[0][:500]


def _to_bq(param: QueryParam) -> bigquery.ArrayQueryParameter | bigquery.ScalarQueryParameter:
    if param.array:
        return bigquery.ArrayQueryParameter(param.name, param.type, param.value)
    return bigquery.ScalarQueryParameter(param.name, param.type, param.value)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    return value


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1000:
            return f"{n:.0f} {unit}"
        n /= 1000
    return f"{n:.1f} TB"
