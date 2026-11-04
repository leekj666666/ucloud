"""Authenticated same-origin console; read models never grant execution authority."""

import hashlib
import hmac
import html
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, or_, select

from .controls import ControlConflict, Controls, IncidentControl, columns
from .durable import digest
from .store import Incident, WorkflowRun, as_dict

SECRET_FIELDS = {
    "api_key",
    "password",
    "authorization",
    "cookie",
    "set-cookie",
    "access_token",
    "refresh_token",
    "database_url",
    "worker_password",
    "cloudsre_diag_token",
}


def public(value):
    if isinstance(value, dict):
        if str(value.get("name", "")).lower() in SECRET_FIELDS and "value" in value:
            return {**value, "value": "[REDACTED]"}
        return {
            k: "[REDACTED]" if k.lower() in SECRET_FIELDS else public(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [public(v) for v in value]
    return value


class Login(BaseModel):
    token: str = Field(min_length=1, max_length=256)


class Task(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=300)
    service: Literal["order", "inventory", "gateway"]
    namespace: Literal["sre-demo", "sre-demo-b"] = "sre-demo"
    execution_mode: Literal["manual", "auto"] = "manual"
    request_id: str = Field(pattern=r"^[a-f0-9-]{32,36}$")
    auto_acknowledged: bool = False

    @model_validator(mode="after")
    def acknowledge_auto(self):
        if self.execution_mode == "auto" and not self.auto_acknowledged:
            raise ValueError("Automatic execution requires explicit acknowledgement")
        return self


class Start(BaseModel):
    namespace: Literal["sre-demo", "sre-demo-b"] = "sre-demo"
    execution_mode: Literal["manual", "auto"] = "manual"
    auto_acknowledged: bool = False


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    action_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    intent_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    decision: Literal["approve", "reject"]
    reason: str = Field(min_length=1, max_length=1000)


class Stop(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)


def install(app, store, *, token=None, evidence_roots=()):
    token = token or os.environ.get("CLOUDSRE_CONSOLE_TOKEN")
    sessions = {}
    failures = {}
    controls = Controls(store)
    roots = [Path(p).resolve() for p in evidence_roots]
    router = APIRouter(prefix="/console")

    def same_origin(request: Request):
        origin = request.headers.get("origin")
        expected = str(request.base_url).rstrip("/")
        if origin and origin != expected:
            raise HTTPException(403, "Cross-origin control request refused")
        if request.headers.get("x-cloudsre-request") != "console":
            raise HTTPException(403, "Console request header required")

    def enabled():
        if not token:
            raise HTTPException(503, "Console access is not configured")

    def auth(request: Request):
        enabled()
        key = request.cookies.get("cloudsre_session", "")
        expiry = sessions.get(key, 0)
        if expiry < time.monotonic():
            sessions.pop(key, None)
            raise HTTPException(401, "Console session required")
        if request.method not in {"GET", "HEAD"}:
            same_origin(request)
        return "local-operator"

    auth_dependency = Depends(auth)

    @router.post("/session")
    def login(data: Login, request: Request, response: Response):
        enabled()
        same_origin(request)
        address = request.client.host if request.client else "unknown"
        attempts = [t for t in failures.get(address, []) if time.monotonic() - t < 60]
        if len(attempts) >= 5:
            raise HTTPException(429, "Too many login attempts; retry in one minute")
        if not hmac.compare_digest(data.token.encode(), token.encode()):
            failures[address] = [*attempts, time.monotonic()]
            raise HTTPException(403, "Invalid console access code")
        failures.pop(address, None)
        for k, expiry in list(sessions.items()):
            if expiry < time.monotonic():
                sessions.pop(k, None)
        key = secrets.token_urlsafe(32)
        sessions[key] = time.monotonic() + 43200
        response.set_cookie(
            "cloudsre_session",
            key,
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="strict",
            path="/console",
            max_age=43200,
        )
        response.headers["Cache-Control"] = "no-store"
        return {"actor": "local-operator", "expires_in_seconds": 43200}

    @router.get("/session")
    def session(actor=auth_dependency):
        return {"actor": actor, "default_execution_mode": "manual"}

    @router.delete("/session")
    def logout(request: Request, response: Response, actor=auth_dependency):
        sessions.pop(request.cookies.get("cloudsre_session", ""), None)
        response.delete_cookie("cloudsre_session", path="/console")
        return {"signed_out": True}

    def translate(fn):
        try:
            return fn()
        except KeyError:
            raise HTTPException(404, "Incident not found") from None
        except ControlConflict as exc:
            raise HTTPException(409, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @router.get("/incidents")
    def listing(
        q: str = "",
        status: str = "",
        service: str = "",
        scope: Literal["all", "console"] = "all",
        offset: int = 0,
        limit: int = 30,
        actor=auth_dependency,
    ):
        if len(q) > 200 or offset < 0 or not 1 <= limit <= 100:
            raise HTTPException(422, "Invalid pagination or query")
        conditions = []
        if scope == "console":
            conditions.append(Incident.id.in_(select(IncidentControl.incident_id)))
        if q:
            conditions.append(
                or_(Incident.title.ilike("%" + q + "%"), Incident.id == q)
            )
        if status:
            conditions.append(Incident.status == status)
        if service:
            conditions.append(Incident.service == service)
        with store.sessions() as s:
            total = s.scalar(
                select(func.count()).select_from(Incident).where(*conditions)
            )
            rows = list(
                s.scalars(
                    select(Incident)
                    .where(*conditions)
                    .order_by(Incident.created_at.desc(), Incident.id)
                    .offset(offset)
                    .limit(limit)
                )
            )
            ids = [row.id for row in rows]
            control_map = {
                row.incident_id: columns(row)
                for row in s.scalars(
                    select(IncidentControl).where(IncidentControl.incident_id.in_(ids))
                )
            }
            latest = {}
            for run in s.scalars(
                select(WorkflowRun)
                .where(WorkflowRun.incident_id.in_(ids))
                .order_by(WorkflowRun.updated_at)
            ):
                payload = json.loads(run.payload)
                latest[run.incident_id] = {
                    "run_id": run.id,
                    "updated_at": run.updated_at,
                    "outcome": payload.get("outcome"),
                    "namespace": payload.get("namespace"),
                    "usage": {
                        k: v
                        for k, v in payload.get("usage", {}).items()
                        if k not in {"calls", "ledger_path"}
                    },
                    "quality": payload.get("quality"),
                    "error": payload.get("error"),
                }
            return public(
                {
                    "items": [
                        {
                            **as_dict(row),
                            "diagnosis": None,
                            "control": control_map.get(row.id),
                            "latest": latest.get(row.id),
                        }
                        for row in rows
                    ],
                    "total": total,
                    "offset": offset,
                    "limit": limit,
                }
            )

    def verification_live(run):
        if not run:
            return None
        attempt = run["payload"].get("verification_attempts", 0)
        rid = run["id"]
        if (
            type(attempt) is not int
            or not 1 <= attempt <= 100
            or not re.fullmatch(r"[a-f0-9]{32}", rid)
        ):
            return None
        for root in roots:
            path = (
                root / rid / ("recovery-" + str(attempt)) / "samples.jsonl"
            ).resolve()
            if (
                not path.is_relative_to(root)
                or not path.is_file()
                or path.stat().st_size > 5_000_000
            ):
                continue
            rows = []
            partial = 0
            for line in path.read_text().splitlines():
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    partial += 1
            loads = [row for row in rows if row.get("kind") == "load"]
            full = [row for row in rows if row.get("kind") == "business"]
            return {
                "authoritative": False,
                "attempt": attempt,
                "partial_lines": partial,
                "load_requests": len(loads),
                "business_checks": len(full),
                "errors": sum(
                    row.get("result", {}).get("passed") is not True for row in loads
                ),
                "started_at": min((row["at"] for row in rows), default=None),
                "samples": [
                    {
                        "at": row["at"],
                        "passed": row.get("result", {}).get("passed", False),
                        "http_status": row.get("result", {}).get("http_status"),
                        "latency_seconds": row.get("result", {}).get("latency_seconds"),
                    }
                    for row in loads[-12:]
                ],
            }
        return None

    def detail(ident):
        row = store.get(ident)
        if not row:
            raise HTTPException(404, "Incident not found")
        runs = store.runs(ident)
        latest = runs[-1] if runs else None
        payload = latest["payload"] if latest else {}
        intent = payload.get("action_intent")
        receipts = store.receipts(ident)
        control = controls.get(ident)
        observed_namespaces = {
            a.get("payload", {}).get("labels", {}).get("namespace", "sre-demo")
            for a in receipts
        }
        namespace = control["namespace"] if control else payload.get("namespace")
        if not namespace and len(observed_namespaces) == 1:
            namespace = next(iter(observed_namespaces))
        human = controls.events(ident)
        timeline = [
            {**item, "kind": "workflow", "id": str(i)}
            for i, item in enumerate(payload.get("events", []))
        ]
        timeline += [
            {**item, "kind": "operator", "operation": item["kind"]} for item in human
        ]
        timeline.sort(key=lambda item: (item["at"], item["id"]))
        return public(
            {
                "incident": row,
                "control": control,
                "namespace": namespace,
                "runs": runs,
                "latest": latest,
                "timeline": timeline,
                "operator_events": human,
                "plan_hash": digest(intent) if intent else None,
                "decision": controls.decision(intent["id"]) if intent else None,
                "alerts": receipts,
                "verification_live": verification_live(latest),
                "verification_claim": "verified_recovery"
                if row["status"] == "RESOLVED"
                and payload.get("quality", {}).get("passed") is True
                else "not_verified_recovery",
            }
        )

    @router.get("/incidents/{ident}")
    def get(ident: str, actor=auth_dependency):
        return detail(ident)

    @router.post("/incidents", status_code=201)
    def create(data: Task, response: Response, actor=auth_dependency):
        row, created = translate(
            lambda: controls.create(
                **data.model_dump(exclude={"auto_acknowledged"}), actor=actor
            )
        )
        response.status_code = 201 if created else 200
        return detail(row["id"])

    @router.post("/incidents/{ident}/start")
    def start(ident: str, data: Start, actor=auth_dependency):
        if data.execution_mode == "auto" and not data.auto_acknowledged:
            raise HTTPException(422, "Automatic mode requires acknowledgement")
        translate(
            lambda: controls.start(ident, data.namespace, data.execution_mode, actor)
        )
        return detail(ident)

    @router.post("/incidents/{ident}/decisions")
    def decide(ident: str, data: Decision, actor=auth_dependency):
        return translate(
            lambda: controls.decide(ident, **data.model_dump(), actor=actor)
        )

    @router.post("/incidents/{ident}/stop", status_code=202)
    def stop(ident: str, data: Stop, actor=auth_dependency):
        return translate(lambda: controls.stop(ident, data.reason, actor))

    def evidence_files(ident):
        data = detail(ident)
        result = {}
        for run in data["runs"]:
            rid = run["id"]
            if not re.fullmatch(r"[a-f0-9]{32}", rid):
                continue
            for root in roots:
                for candidate in (
                    root / rid / "evidence",
                    root / ("d2-" + rid) / "evidence",
                ):
                    folder = candidate.resolve()
                    if not folder.is_relative_to(root) or not folder.is_dir():
                        continue
                    for p in sorted(folder.glob("e-*.json"))[:100]:
                        resolved = p.resolve()
                        if (
                            not resolved.is_relative_to(root)
                            or p.stat().st_size > 1_000_000
                        ):
                            continue
                        raw = json.loads(p.read_text())
                        if raw.get("id") != p.stem:
                            continue
                        result[p.stem] = {
                            "id": p.stem,
                            "kind": raw.get("kind"),
                            "collected_at": raw.get("collected_at"),
                            "source": raw.get("source"),
                            "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                            "content": public(raw),
                        }
        return result

    def report_view(data):
        latest = data["latest"]
        j = latest["payload"] if latest else {}
        fallback = {
            "report_status": "incomplete",
            "impact": {
                "service": data["incident"]["service"],
                "affected_business_requests": None,
                "description": "Observed symptoms only; customer impact is not inferred.",
            },
            "unresolved": [
                j.get("error") or j.get("stop_reason") or "尚未完成独立业务验收"
            ],
            "long_term_recommendations": [],
            "limitations": ["任务未完成，当前导出不是恢复确认。"],
        }
        if not latest or not j.get("report", {}).get("sha256"):
            return fallback
        rid = latest["id"]
        if not re.fullmatch(r"[a-f0-9]{32}", rid):
            return fallback
        for root in roots:
            for candidate in (
                root / rid / "incident-report.json",
                root / ("d2-" + rid) / "incident-report.json",
            ):
                path = candidate.resolve()
                if (
                    not path.is_relative_to(root)
                    or not path.is_file()
                    or path.stat().st_size > 2_000_000
                ):
                    continue
                raw = path.read_bytes()
                if hashlib.sha256(raw).hexdigest() != j["report"]["sha256"]:
                    continue
                report = json.loads(raw)
                if (report.get("incident_id"), report.get("run_id")) != (
                    data["incident"]["id"],
                    rid,
                ):
                    continue
                return public(
                    {
                        **report,
                        "report_status": "recorded",
                        "authoritative_incident_status": data["incident"]["status"],
                        "verification_claim": data["verification_claim"],
                        "operator_events": data["operator_events"],
                    }
                )
        return {
            **fallback,
            "report_status": "file_unavailable",
            "limitations": ["正式报告文件缺失或哈希不匹配，当前显示数据库记录。"],
        }

    @router.get("/incidents/{ident}/report")
    def recorded_report(ident: str, actor=auth_dependency):
        return report_view(detail(ident))

    @router.get("/incidents/{ident}/evidence")
    def evidence(ident: str, actor=auth_dependency):
        records = evidence_files(ident)
        return {
            "items": [
                {k: v for k, v in e.items() if k != "content"} for e in records.values()
            ],
            "limit_per_run": 100,
        }

    @router.get("/incidents/{ident}/evidence/{eid}")
    def evidence_detail(ident: str, eid: str, actor=auth_dependency):
        if not re.fullmatch(r"e-[a-f0-9]{32}", eid):
            raise HTTPException(404, "Evidence not found")
        record = evidence_files(ident).get(eid)
        if not record:
            raise HTTPException(404, "Evidence unavailable for this incident")
        return record

    @router.get("/incidents/{ident}/export")
    def export(
        ident: str, format: Literal["json", "markdown"] = "json", actor=auth_dependency
    ):
        data = detail(ident)
        data["evidence"] = [
            {k: v for k, v in item.items() if k != "content"}
            for item in evidence_files(ident).values()
        ]
        data["formal_report"] = report_view(data)
        data["export_notes"] = [
            "Database incident status is authoritative.",
            "Unknown usage and costs remain unknown.",
            "Cancelled or rejected tasks are not verified recovery.",
        ]
        headers = {
            "Content-Disposition": 'attachment; filename="incident-'
            + data["incident"]["id"]
            + ('.json"' if format == "json" else '.md"'),
            "Cache-Control": "no-store",
        }
        if format == "json":
            return JSONResponse(data, headers=headers)
        body = (
            "# Tm11GPT事件报告\n\n"
            + html.escape(data["incident"]["title"])
            + "\n\n状态："
            + data["incident"]["status"]
            + "。业务恢复声明："
            + data["verification_claim"]
            + "。\n\n下列记录包括证据索引、方案、人工决策、动作与业务验收；未完成任务不代表恢复，未知费用不记为零。\n\n```json\n"
            + json.dumps(data, ensure_ascii=False, indent=2).replace("```", "`\u200b``")
            + "\n```\n"
        )
        return PlainTextResponse(body, headers=headers, media_type="text/markdown")

    @app.middleware("http")
    async def console_headers(request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/console"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    app.include_router(router)
