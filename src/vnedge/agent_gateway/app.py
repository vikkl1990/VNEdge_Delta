"""FastAPI router for the VNEDGE Agent Gateway."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from vnedge.agent_gateway.audit import AgentAuditEvent, AgentAuditLogger
from vnedge.agent_gateway.auth import AgentPrincipal, AgentTokenStore
from vnedge.agent_gateway.jobs import create_backtest_job, list_jobs, read_job
from vnedge.agent_gateway.task_registry import (
    QuantOSAgentGateway,
    env_quant_os_agent_gateway_dir,
)


@dataclass(frozen=True)
class AgentGatewayArtifacts:
    research_path: Path | None = None
    alpha_council_path: Path | None = None
    alpha_workbench_path: Path | None = None
    vibe_intelligence_path: Path | None = None
    lane_readiness_path: Path | None = None
    realtime_scanner_path: Path | None = None


class BacktestRequest(BaseModel):
    """Research-only backtest job request.

    The gateway records this request for a worker to pick up later. It does not
    execute arbitrary code inline, does not mutate strategy source, and does not
    create a paper/shadow/live lane.
    """

    model_config = ConfigDict(extra="forbid")

    strategy_id: str = Field(min_length=1, max_length=160)
    exchange: str = Field(min_length=1, max_length=80)
    symbol: str = Field(min_length=1, max_length=80)
    timeframe: str = Field(min_length=1, max_length=20)
    start: str | None = None
    end: str | None = None
    hypothesis_id: str | None = Field(default=None, max_length=240)
    notes: str | None = Field(default=None, max_length=2_000)
    initial_capital_usd: float = Field(default=500.0, gt=0.0, le=1_000_000.0)
    commission_bps: float | None = Field(default=None, ge=0.0, le=100.0)
    slippage_bps: float | None = Field(default=None, ge=0.0, le=100.0)
    strict_mode: bool = True
    live_orders_enabled: bool = False
    parameters: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _research_only(self) -> BacktestRequest:
        if not self.strict_mode:
            raise ValueError("strict_mode must stay true for agent-submitted backtests")
        if self.live_orders_enabled:
            raise ValueError("agent-submitted backtests cannot enable live orders")
        return self


class QuantOSTaskRequest(BaseModel):
    """Research-only durable task request for Agent Gateway v2."""

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_.:-]+$")
    objective: str = Field(min_length=1, max_length=2_000)
    priority: int = Field(default=50, ge=0, le=100)
    target: dict[str, Any] = Field(default_factory=dict)
    payload: dict[str, Any] = Field(default_factory=dict)
    live_orders_enabled: bool = False

    @model_validator(mode="after")
    def _research_only(self) -> QuantOSTaskRequest:
        if self.live_orders_enabled:
            raise ValueError("Quant OS tasks cannot enable live orders")
        return self


class QuantOSEventRequest(BaseModel):
    """Append one progress event to an existing Quant OS task."""

    model_config = ConfigDict(extra="forbid")

    event_type: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_.:-]+$")
    message: str = Field(min_length=1, max_length=2_000)
    level: str = Field(default="info", max_length=20)
    payload: dict[str, Any] = Field(default_factory=dict)


class QuantOSArtifactRequest(BaseModel):
    """Publish a hash-backed research artifact for one Quant OS task."""

    model_config = ConfigDict(extra="forbid")

    artifact_type: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_.:-]+$")
    summary: str = Field(min_length=1, max_length=2_000)
    content: str | dict[str, Any] | None = None
    path: str | None = Field(default=None, max_length=2_000)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _has_artifact_source(self) -> QuantOSArtifactRequest:
        if self.content is None and not self.path:
            raise ValueError("artifact request must include content or path")
        return self


def env_agent_audit_path(env: dict[str, str] | None = None) -> Path:
    source = os.environ if env is None else env
    return Path(source.get("AGENT_GATEWAY_AUDIT_PATH", "logs/agent_gateway/audit.jsonl"))


def env_agent_jobs_dir(env: dict[str, str] | None = None) -> Path:
    source = os.environ if env is None else env
    return Path(source.get("AGENT_GATEWAY_JOBS_DIR", "logs/agent_gateway/jobs"))


def mount_agent_gateway(
    app: FastAPI,
    *,
    provider: Any,
    token_store: AgentTokenStore,
    audit_logger: AgentAuditLogger,
    jobs_dir: Path,
    artifacts: AgentGatewayArtifacts,
    quant_os_gateway_dir: Path | None = None,
) -> None:
    router = APIRouter(prefix="/api/agent/v1", tags=["agent-gateway"])
    v2 = APIRouter(prefix="/api/agent/v2", tags=["agent-gateway-v2"])
    quant_os_gateway = QuantOSAgentGateway(quant_os_gateway_dir or env_quant_os_agent_gateway_dir())

    def _read_json_payload(path: Path | None, fallback: dict[str, Any]) -> dict[str, Any]:
        if path is None or not path.exists():
            return fallback
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return fallback
        return payload if isinstance(payload, dict) else fallback

    def _agent_from_request(request: Request) -> AgentPrincipal:
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            audit_logger.write(
                AgentAuditEvent(
                    agent=None,
                    token_prefix=None,
                    method=request.method,
                    path=request.url.path,
                    action="authenticate",
                    outcome="DENIED",
                    reason="missing bearer token",
                )
            )
            raise HTTPException(status_code=401, detail="missing bearer agent token")

        result = token_store.authenticate(token.strip())
        if not result.authorized or result.principal is None:
            audit_logger.write(
                AgentAuditEvent(
                    agent=result.name,
                    token_prefix=result.token_prefix,
                    method=request.method,
                    path=request.url.path,
                    action="authenticate",
                    outcome="DENIED",
                    reason=result.reason,
                )
            )
            raise HTTPException(status_code=401, detail=result.reason or "invalid agent token")
        return result.principal

    def _require_scope(
        principal: AgentPrincipal,
        scope: Literal["R", "B", "W_RESEARCH", "T_PAPER"],
        request: Request,
        *,
        action: str,
    ) -> None:
        if principal.has_scope(scope):
            return
        audit_logger.write(
            AgentAuditEvent(
                agent=principal.name,
                token_prefix=principal.token_prefix,
                method=request.method,
                path=request.url.path,
                action=action,
                scope=scope,
                outcome="DENIED",
                reason="missing required scope",
                paper_only=principal.paper_only,
            )
        )
        raise HTTPException(status_code=403, detail=f"missing required agent scope: {scope}")

    def _audit_ok(
        request: Request,
        principal: AgentPrincipal,
        *,
        action: str,
        scope: str | None,
        job_id: str | None = None,
    ) -> None:
        audit_logger.write(
            AgentAuditEvent(
                agent=principal.name,
                token_prefix=principal.token_prefix,
                method=request.method,
                path=request.url.path,
                action=action,
                scope=scope,
                outcome="OK",
                job_id=job_id,
                paper_only=principal.paper_only,
            )
        )

    def _snapshot_or_503() -> dict[str, Any]:
        snapshot = provider.latest()
        if snapshot is None:
            raise HTTPException(status_code=503, detail="no snapshot yet")
        return snapshot

    def _lane_rows(snapshot: dict[str, Any], principal: AgentPrincipal) -> list[dict[str, Any]]:
        raw_lanes = snapshot.get("lanes")
        if not isinstance(raw_lanes, list):
            raw_lanes = []
        if not raw_lanes and snapshot.get("lane_id"):
            raw_lanes = [snapshot]

        rows: list[dict[str, Any]] = []
        for raw in raw_lanes:
            if not isinstance(raw, dict):
                continue
            lane_id = str(raw.get("lane_id") or raw.get("id") or "")
            if lane_id and not principal.lane_allowed(lane_id):
                continue
            rows.append(
                {
                    "lane_id": lane_id,
                    "exchange": raw.get("exchange", ""),
                    "symbol": raw.get("symbol", snapshot.get("symbol", "")),
                    "timeframe": raw.get("timeframe", ""),
                    "mode": raw.get("mode", snapshot.get("mode", "")),
                    "strategy_id": raw.get("strategy_id", snapshot.get("strategy_id", "")),
                    "state": raw.get("state", raw.get("status", "")),
                    "risk_status": raw.get("risk_status", snapshot.get("risk_status", "")),
                    "feed_health": raw.get("feed_health", {}),
                    "fills": raw.get("fills", 0),
                    "virtual_pnl": raw.get("virtual_pnl", raw.get("realized_pnl", 0.0)),
                    "last_reason": raw.get("last_reason", raw.get("skip_reason", "")),
                    "can_trade": False,
                    "can_promote": False,
                }
            )
        return rows

    @router.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "gateway": "vnedge_agent_gateway_v1",
            "version": 1,
            "auth": "bearer_agent_token",
            "paper_only_default": True,
            "live_orders_available": False,
            "routes": [
                "whoami",
                "state",
                "lanes",
                "research/latest",
                "alpha-council",
                "alpha-workbench",
                "vibe-intelligence",
                "lane-readiness",
                "realtime-scanner",
                "jobs",
                "backtests",
                "v2/tasks",
                "v2/events",
                "v2/artifacts",
            ],
        }

    @router.get("/whoami")
    async def whoami(request: Request) -> JSONResponse:
        principal = _agent_from_request(request)
        _audit_ok(request, principal, action="whoami", scope=None)
        return JSONResponse(
            {
                "name": principal.name,
                "token_prefix": principal.token_prefix,
                "scopes": sorted(principal.scopes),
                "paper_only": principal.paper_only,
                "rate_limit_per_min": principal.rate_limit_per_min,
                "expires_at": (
                    principal.expires_at.isoformat() if principal.expires_at is not None else None
                ),
                "markets": list(principal.markets),
                "lanes": list(principal.lanes),
                "can_live_trade": False,
            },
            headers={"X-Agent-Name": principal.name},
        )

    @router.get("/state")
    async def state(request: Request) -> JSONResponse:
        principal = _agent_from_request(request)
        _require_scope(principal, "R", request, action="read_state")
        snapshot = _snapshot_or_503()
        _audit_ok(request, principal, action="read_state", scope="R")
        return JSONResponse(snapshot, headers={"X-Agent-Name": principal.name})

    @router.get("/lanes")
    async def lanes(request: Request) -> JSONResponse:
        principal = _agent_from_request(request)
        _require_scope(principal, "R", request, action="read_lanes")
        snapshot = _snapshot_or_503()
        rows = _lane_rows(snapshot, principal)
        _audit_ok(request, principal, action="read_lanes", scope="R")
        return JSONResponse(
            {
                "generated_from": "dashboard_snapshot",
                "lanes": rows,
                "count": len(rows),
                "can_trade": False,
                "can_promote": False,
            },
            headers={"X-Agent-Name": principal.name},
        )

    def _artifact_route(
        request: Request,
        *,
        action: str,
        path: Path | None,
        fallback: dict[str, Any],
    ) -> JSONResponse:
        principal = _agent_from_request(request)
        _require_scope(principal, "R", request, action=action)
        payload = _read_json_payload(path, fallback)
        _audit_ok(request, principal, action=action, scope="R")
        return JSONResponse(payload, headers={"X-Agent-Name": principal.name})

    @router.get("/research/latest")
    async def research_latest(request: Request) -> JSONResponse:
        return _artifact_route(
            request,
            action="read_research_latest",
            path=artifacts.research_path,
            fallback={"results": [], "can_trade": False, "can_promote": False},
        )

    @router.get("/alpha-council")
    async def alpha_council(request: Request) -> JSONResponse:
        return _artifact_route(
            request,
            action="read_alpha_council",
            path=artifacts.alpha_council_path,
            fallback={"summary": {}, "debates": [], "can_trade": False, "can_promote": False},
        )

    @router.get("/alpha-workbench")
    async def alpha_workbench(request: Request) -> JSONResponse:
        return _artifact_route(
            request,
            action="read_alpha_workbench",
            path=artifacts.alpha_workbench_path,
            fallback={"summary": {}, "tasks": [], "can_trade": False, "can_promote": False},
        )

    @router.get("/vibe-intelligence")
    async def vibe_intelligence(request: Request) -> JSONResponse:
        return _artifact_route(
            request,
            action="read_vibe_intelligence",
            path=artifacts.vibe_intelligence_path,
            fallback={"summary": {}, "cards": [], "can_trade": False, "can_promote": False},
        )

    @router.get("/lane-readiness")
    async def lane_readiness(request: Request) -> JSONResponse:
        return _artifact_route(
            request,
            action="read_lane_readiness",
            path=artifacts.lane_readiness_path,
            fallback={
                "summary": {},
                "rows": [],
                "operator_answer": "lane readiness report unavailable",
                "can_trade": False,
                "can_promote": False,
            },
        )

    @router.get("/realtime-scanner")
    async def realtime_scanner(request: Request) -> JSONResponse:
        return _artifact_route(
            request,
            action="read_realtime_scanner",
            path=artifacts.realtime_scanner_path,
            fallback={
                "summary": {},
                "rows": [],
                "mode": "live_observation_not_replay",
                "can_trade": False,
                "can_promote": False,
            },
        )

    @router.get("/jobs")
    async def jobs(request: Request, limit: int = 100) -> JSONResponse:
        principal = _agent_from_request(request)
        _require_scope(principal, "R", request, action="list_jobs")
        rows = list_jobs(jobs_dir, limit=limit)
        _audit_ok(request, principal, action="list_jobs", scope="R")
        return JSONResponse(
            {"jobs": rows, "count": len(rows), "can_trade": False, "can_promote": False},
            headers={"X-Agent-Name": principal.name},
        )

    @router.get("/jobs/{job_id}")
    async def job_detail(request: Request, job_id: str) -> JSONResponse:
        principal = _agent_from_request(request)
        _require_scope(principal, "R", request, action="read_job")
        job = read_job(jobs_dir, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="agent job not found")
        _audit_ok(request, principal, action="read_job", scope="R", job_id=job_id)
        return JSONResponse(job, headers={"X-Agent-Name": principal.name})

    @router.post("/backtests", status_code=202)
    async def request_backtest(request: Request, payload: BacktestRequest) -> JSONResponse:
        principal = _agent_from_request(request)
        _require_scope(principal, "B", request, action="request_backtest")
        if not principal.market_allowed(payload.exchange, payload.symbol):
            audit_logger.write(
                AgentAuditEvent(
                    agent=principal.name,
                    token_prefix=principal.token_prefix,
                    method=request.method,
                    path=request.url.path,
                    action="request_backtest",
                    scope="B",
                    outcome="DENIED",
                    reason="market not allowed for agent token",
                    paper_only=principal.paper_only,
                )
            )
            raise HTTPException(status_code=403, detail="market not allowed for agent token")
        job = create_backtest_job(
            jobs_dir=jobs_dir,
            agent=principal.name,
            request=payload.model_dump(mode="json"),
        )
        _audit_ok(request, principal, action="request_backtest", scope="B", job_id=job["job_id"])
        return JSONResponse(job, status_code=202, headers={"X-Agent-Name": principal.name})

    app.include_router(router)

    def _enforce_optional_market_target(
        principal: AgentPrincipal,
        request: Request,
        target: dict[str, Any],
        *,
        action: str,
        scope: str,
    ) -> None:
        exchange = target.get("exchange")
        symbol = target.get("symbol")
        if exchange is None or symbol is None:
            return
        if principal.market_allowed(str(exchange), str(symbol)):
            return
        audit_logger.write(
            AgentAuditEvent(
                agent=principal.name,
                token_prefix=principal.token_prefix,
                method=request.method,
                path=request.url.path,
                action=action,
                scope=scope,
                outcome="DENIED",
                reason="market not allowed for agent token",
                paper_only=principal.paper_only,
            )
        )
        raise HTTPException(status_code=403, detail="market not allowed for agent token")

    @v2.get("/health")
    async def quant_os_health() -> dict[str, Any]:
        snapshot = quant_os_gateway.snapshot(limit=20)
        return {
            "status": "ok",
            "gateway": "quant_os_agent_gateway_v2",
            "version": 2,
            "task_root": str(quant_os_gateway.root),
            "summary": snapshot["summary"],
            "live_orders_available": False,
            "can_trade": False,
            "can_promote": False,
        }

    @v2.get("/tasks")
    async def quant_os_tasks(request: Request, limit: int = 100) -> JSONResponse:
        principal = _agent_from_request(request)
        _require_scope(principal, "R", request, action="read_quant_os_tasks")
        payload = quant_os_gateway.snapshot(limit=max(1, min(int(limit), 250)))
        _audit_ok(request, principal, action="read_quant_os_tasks", scope="R")
        return JSONResponse(payload, headers={"X-Agent-Name": principal.name})

    @v2.post("/tasks", status_code=202)
    async def create_quant_os_task(
        request: Request,
        payload: QuantOSTaskRequest,
    ) -> JSONResponse:
        principal = _agent_from_request(request)
        _require_scope(principal, "W_RESEARCH", request, action="create_quant_os_task")
        _enforce_optional_market_target(
            principal,
            request,
            payload.target,
            action="create_quant_os_task",
            scope="W_RESEARCH",
        )
        task = quant_os_gateway.create_task(
            kind=payload.kind,
            objective=payload.objective,
            priority=payload.priority,
            requested_by=principal.name,
            target=payload.target,
            payload=payload.payload,
        )
        _audit_ok(
            request,
            principal,
            action="create_quant_os_task",
            scope="W_RESEARCH",
            job_id=str(task["task_id"]),
        )
        return JSONResponse(task, status_code=202, headers={"X-Agent-Name": principal.name})

    @v2.get("/events")
    async def quant_os_events(request: Request, limit: int = 100) -> JSONResponse:
        principal = _agent_from_request(request)
        _require_scope(principal, "R", request, action="read_quant_os_events")
        payload = quant_os_gateway.snapshot(limit=max(1, min(int(limit), 250)))
        _audit_ok(request, principal, action="read_quant_os_events", scope="R")
        return JSONResponse(
            {
                "gateway_id": payload["gateway_id"],
                "events": payload["events"],
                "can_trade": False,
                "can_promote": False,
            },
            headers={"X-Agent-Name": principal.name},
        )

    @v2.post("/tasks/{task_id}/events", status_code=202)
    async def append_quant_os_event(
        request: Request,
        task_id: str,
        payload: QuantOSEventRequest,
    ) -> JSONResponse:
        principal = _agent_from_request(request)
        _require_scope(principal, "W_RESEARCH", request, action="append_quant_os_event")
        try:
            event = quant_os_gateway.emit_event(
                task_id,
                event_type=payload.event_type,
                message=payload.message,
                level=payload.level,
                payload=payload.payload,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        _audit_ok(
            request,
            principal,
            action="append_quant_os_event",
            scope="W_RESEARCH",
            job_id=task_id,
        )
        return JSONResponse(event, status_code=202, headers={"X-Agent-Name": principal.name})

    @v2.get("/artifacts")
    async def quant_os_artifacts(request: Request, limit: int = 100) -> JSONResponse:
        principal = _agent_from_request(request)
        _require_scope(principal, "R", request, action="read_quant_os_artifacts")
        payload = quant_os_gateway.snapshot(limit=max(1, min(int(limit), 250)))
        _audit_ok(request, principal, action="read_quant_os_artifacts", scope="R")
        return JSONResponse(
            {
                "gateway_id": payload["gateway_id"],
                "artifacts": payload["artifacts"],
                "can_trade": False,
                "can_promote": False,
            },
            headers={"X-Agent-Name": principal.name},
        )

    @v2.post("/tasks/{task_id}/artifacts", status_code=202)
    async def publish_quant_os_artifact(
        request: Request,
        task_id: str,
        payload: QuantOSArtifactRequest,
    ) -> JSONResponse:
        principal = _agent_from_request(request)
        _require_scope(principal, "W_RESEARCH", request, action="publish_quant_os_artifact")
        try:
            if payload.content is not None:
                artifact = quant_os_gateway.register_content_artifact(
                    task_id,
                    artifact_type=payload.artifact_type,
                    summary=payload.summary,
                    content=payload.content,
                    metadata=payload.metadata,
                )
            else:
                artifact = quant_os_gateway.register_artifact(
                    task_id,
                    artifact_path=Path(str(payload.path)),
                    artifact_type=payload.artifact_type,
                    summary=payload.summary,
                    metadata=payload.metadata,
                )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"artifact path not found: {exc}")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        _audit_ok(
            request,
            principal,
            action="publish_quant_os_artifact",
            scope="W_RESEARCH",
            job_id=task_id,
        )
        return JSONResponse(artifact, status_code=202, headers={"X-Agent-Name": principal.name})

    app.include_router(v2)
