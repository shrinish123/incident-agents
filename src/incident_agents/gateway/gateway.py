"""Query gateway: the single path from agents to any external system.

Order of checks for every query:
  source known -> circuit closed -> investigation budget left -> lint against
  policy -> cache -> cost estimate -> per-query cap -> human approval above the
  soft cap -> reserve budget -> rate and concurrency limits -> execute with
  timeout and bounded retries -> cap rows, redact, scrub -> settle budget ->
  audit.

`execute` never raises to the caller. Every outcome, including a refusal, is a
structured `QueryResult` the agent can reason about.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import secrets as _secrets
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

from ..connectors.base import Connector, build_connector
from ..secrets import CredentialBroker, SourceAuthError
from .controls import AuditLog, AuditRecord, BudgetLedger, CircuitBreaker, Limiter, TTLCache, now
from .linters import LINTERS, LintContext
from .models import ConnectorError, ConnectorTimeout, CostEstimate, QueryRequest, QueryResult, RawResult, Status
from .policy import SourceConfig

Approver = Callable[[QueryRequest, CostEstimate], Awaitable[bool]]

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")


class QueryGateway:
    def __init__(
        self,
        sources: dict[str, SourceConfig],
        connectors: dict[str, Connector],
        broker: CredentialBroker,
        *,
        ledger: BudgetLedger | None = None,
        audit: AuditLog | None = None,
        approver: Approver | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        pii_salt: str | None = None,
    ) -> None:
        missing = set(sources) - set(connectors)
        if missing:
            raise ValueError(f"No connector for sources: {sorted(missing)}")
        self.sources = sources
        self.connectors = connectors
        self.broker = broker
        self.ledger = ledger or BudgetLedger()
        self.audit = audit or AuditLog()
        self.approver = approver
        self._clock = clock
        self._sleep = sleep
        self._salt = pii_salt or _secrets.token_hex(8)
        self._cache = TTLCache(clock)
        self._breakers = {
            n: CircuitBreaker(s.policy.breaker_threshold, s.policy.breaker_cooldown_s, s.policy.auth_cooldown_s, clock)
            for n, s in sources.items()
        }
        self._limiters = {
            n: Limiter(s.policy.max_concurrency, s.policy.rate_per_min, clock, sleep) for n, s in sources.items()
        }
        # (investigation, source) -> scopes that already had an aggregate query
        self._aggregated: dict[tuple[str, str], set[str]] = {}

    @classmethod
    def from_config(cls, sources: dict[str, SourceConfig], broker: CredentialBroker, **kwargs: Any) -> "QueryGateway":
        connectors = {n: build_connector(s.type, s.options) for n, s in sources.items()}
        return cls(sources, connectors, broker, **kwargs)

    def describe_sources(self) -> list[dict[str, str]]:
        """What agents are told about sources: names and purpose, nothing secret."""
        return [{"name": s.name, "kind": s.kind, "description": s.description} for s in self.sources.values()]

    # ------------------------------------------------------------------ execute

    async def execute(self, req: QueryRequest) -> QueryResult:
        started = self._clock()
        qid = uuid.uuid4().hex[:12]
        inv = req.investigation_id
        src = self.sources.get(req.source)

        def finish(status: Status, **kw: Any) -> QueryResult:
            result = QueryResult(status=status, source=req.source, query_id=qid, **kw)
            result.latency_ms = int((self._clock() - started) * 1000)
            if src is not None:
                result.cost_unit = src.policy.cost_unit
                result.budget_remaining = self.ledger.remaining(inv, src.name, src.policy.investigation_budget)
            if status is Status.REJECTED:
                self.ledger.note_rejection(inv)
            self._audit(req, src, result)
            return result

        if src is None:
            return finish(
                Status.REJECTED,
                violations=[f"Unknown source {req.source!r}"],
                hint=f"Available sources: {', '.join(sorted(self.sources))}.",
            )
        policy = src.policy
        connector = self.connectors[src.name]
        breaker = self._breakers[src.name]

        if reason := breaker.blocked():
            return finish(
                Status.UNAVAILABLE,
                error=f"Source unavailable: {reason}",
                hint="Do not retry this source now. Use another source or record this as an evidence gap.",
            )
        if reason := self.ledger.exhausted(inv):
            return finish(Status.BUDGET_EXHAUSTED, error=reason)

        # 1. Lint against policy.
        agg_key = (inv, src.name)
        ctx = LintContext(aggregated_scopes=self._aggregated.get(agg_key, set()))
        lint = LINTERS[src.kind](req.params, policy, ctx)
        if lint.violations:
            return finish(Status.REJECTED, violations=lint.violations, hint=lint.hint)
        params = lint.params

        # 2. Cache: a repeated query costs nothing.
        cache_key = TTLCache.key(src.name, params)
        if (hit := self._cache.get(cache_key)) is not None:
            return finish(
                hit.status, rows=hit.rows, truncated=hit.truncated, hint=hit.hint, cached=True,
                cost=0.0, est_cost=hit.est_cost, executed_params=hit.executed_params, meta=hit.meta,
            )

        # 3. Cost estimate, from policy if it can, else from the source (EXPLAIN and similar).
        est = lint.estimate
        if est is None:
            try:
                est = await self.broker.call_with_auth_retry(
                    src.auth, lambda creds: connector.estimate(params, creds), source=src.name, investigation_id=inv
                )
            except SourceAuthError:
                breaker.trip_auth()
                return finish(Status.UNAVAILABLE, error="Source unavailable: auth", executed_params=params)
            except ConnectorError as exc:
                if not exc.retryable:  # the source refused the query itself, e.g. unknown column
                    return finish(Status.ERROR, error=self._clean(exc), executed_params=params)
                est = None
            except Exception:
                est = None
        if est is None:
            if policy.require_estimate:
                return finish(
                    Status.REJECTED,
                    violations=["Cost could not be estimated and this source requires an estimate"],
                    executed_params=params,
                )
            est = CostEstimate(1.0 if policy.cost_unit == "queries" else 0.0, policy.cost_unit, "default")

        if policy.max_cost_per_query is not None and est.amount > policy.max_cost_per_query:
            return finish(
                Status.REJECTED,
                violations=[
                    f"Estimated cost {est.amount:g} {est.unit} exceeds the per-query limit "
                    f"{policy.max_cost_per_query:g} ({est.detail})"
                ],
                hint=(lint.hint + " Narrow the time window or add a more selective filter.").strip(),
                est_cost=est.amount,
                executed_params=params,
            )
        if policy.approval_above is not None and est.amount > policy.approval_above:
            approved = False
            if self.approver is not None:
                try:
                    approved = bool(await self.approver(req, est))
                except Exception:
                    approved = False
            if not approved:
                return finish(
                    Status.REJECTED,
                    violations=[
                        f"Estimated cost {est.amount:g} {est.unit} is above the approval threshold "
                        f"{policy.approval_above:g} and was not approved"
                    ],
                    hint="Narrow the query so it falls under the threshold.",
                    est_cost=est.amount,
                    executed_params=params,
                )

        # 4. Reserve budget before running, so parallel threads cannot overspend together.
        if not self.ledger.reserve(inv, src.name, est.amount, policy.investigation_budget):
            left = self.ledger.remaining(inv, src.name, policy.investigation_budget)
            return finish(
                Status.BUDGET_EXHAUSTED,
                error=f"Estimated cost {est.amount:g} {est.unit} exceeds the {left:g} left for this investigation",
                hint="Run a narrower query, or conclude with the evidence already gathered.",
                est_cost=est.amount,
                executed_params=params,
            )

        # 5. Execute under rate and concurrency limits, with timeout and bounded retries.
        attempts = 0
        raw: RawResult | None = None
        failure: tuple[Status, str] | None = None
        max_attempts = 1 + max(0, policy.max_retries)
        async with self._limiters[src.name].slot():
            while attempts < max_attempts:
                attempts += 1
                try:
                    raw = await asyncio.wait_for(
                        self.broker.call_with_auth_retry(
                            src.auth,
                            lambda creds: connector.execute(
                                params, creds, timeout_s=policy.timeout_s, max_rows=policy.max_rows
                            ),
                            source=src.name,
                            investigation_id=inv,
                        ),
                        timeout=policy.timeout_s,
                    )
                    failure = None
                    break
                except SourceAuthError:
                    breaker.trip_auth()
                    failure = (Status.UNAVAILABLE, "Source unavailable: auth")
                    break
                except (asyncio.TimeoutError, TimeoutError):
                    breaker.failure("timeouts")
                    failure = (Status.TIMEOUT, f"Timed out after {policy.timeout_s:g}s")
                except ConnectorError as exc:
                    status = Status.TIMEOUT if isinstance(exc, ConnectorTimeout) else Status.ERROR
                    failure = (status, self._clean(exc))
                    if not exc.retryable:
                        break
                    breaker.failure("source errors")
                except Exception as exc:  # connector bug or unexpected driver error
                    failure = (Status.ERROR, f"{type(exc).__name__}: {self._clean(exc)}")
                    breaker.failure("source errors")
                    break
                if breaker.blocked() or attempts >= max_attempts:
                    break
                await self._sleep(policy.backoff_s * (2 ** (attempts - 1)))

        if failure is not None or raw is None:
            status, message = failure or (Status.ERROR, "no result")
            # A timed-out query may still have been billed by the source.
            charged = est.amount if status is Status.TIMEOUT else 0.0
            self.ledger.settle(inv, src.name, est.amount, charged)
            hint = ""
            if status is Status.TIMEOUT:
                hint = "The source did not answer in time. Try a narrower query or another source."
            elif status is Status.UNAVAILABLE:
                hint = "Do not retry this source now. Use another source or record this as an evidence gap."
            return finish(
                status, error=message, hint=hint, attempts=attempts, est_cost=est.amount,
                cost=charged, executed_params=params,
            )

        # 6. Post-process: cap rows, redact personal data, scrub secrets.
        breaker.success()
        rows = raw.rows[: policy.max_rows]
        truncated = raw.truncated or len(raw.rows) > policy.max_rows
        rows = self.broker.scrubber.scrub_obj(self._redact(rows, set(policy.redact_fields)))
        cost = raw.cost if raw.cost is not None else est.amount
        self.ledger.settle(inv, src.name, est.amount, cost)
        if src.kind == "log_search" and params.get("mode") == "aggregate":
            self._aggregated.setdefault(agg_key, set()).add(lint.notes.get("scope_key", ""))

        result = finish(
            Status.PARTIAL if truncated else Status.OK,
            rows=rows,
            truncated=truncated,
            incomplete=raw.incomplete,
            hint=("The source returned an incomplete result. Retry the same query once; if it is still partial, treat counts as lower bounds."
                  if raw.incomplete else "Result was capped. Narrow the query to see the rest.") if truncated else "",
            attempts=attempts,
            est_cost=est.amount,
            cost=cost,
            executed_params=params,
            meta=self.broker.scrubber.scrub_obj(raw.meta),
        )
        if not raw.incomplete:  # an incomplete answer must not be replayed from cache: the retry should reach the source
            self._cache.put(cache_key, replace(result), policy.cache_ttl_s)
        return result

    # ------------------------------------------------------------------ helpers

    def _clean(self, exc: BaseException) -> str:
        return self.broker.scrubber.scrub(str(exc))[:300]

    def _token(self, prefix: str, value: Any) -> str:
        digest = hashlib.sha256(f"{self._salt}:{value}".encode()).hexdigest()[:10]
        return f"{prefix}_{digest}"

    def _redact(self, obj: Any, fields: set[str]) -> Any:
        """Replace personal data with stable tokens: the same value always maps to
        the same token, so agents can still correlate rows without seeing it."""
        if isinstance(obj, dict):
            return {
                k: (self._token("pii", v) if k in fields and v is not None else self._redact(v, fields))
                for k, v in obj.items()
            }
        if isinstance(obj, list):
            return [self._redact(v, fields) for v in obj]
        if isinstance(obj, str):
            return _EMAIL_RE.sub(lambda m: self._token("email", m.group(0).lower()), obj)
        return obj

    def _audit(self, req: QueryRequest, src: SourceConfig | None, result: QueryResult) -> None:
        scrub = self.broker.scrubber
        self.audit.write(
            AuditRecord(
                ts=now(),
                query_id=result.query_id,
                investigation_id=req.investigation_id,
                step_id=req.step_id,
                source=req.source,
                kind=src.kind if src else "unknown",
                intent=scrub.scrub(req.intent),
                params=scrub.scrub_obj(result.executed_params or req.params),
                status=result.status.value,
                violations=list(result.violations),
                est_cost=result.est_cost,
                cost=result.cost,
                cost_unit=result.cost_unit,
                rows=len(result.rows),
                attempts=result.attempts,
                latency_ms=result.latency_ms,
                cached=result.cached,
                error=result.error,
            )
        )
