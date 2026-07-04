"""基金估值 Web Demo：同一进程提供响应式页面和 API。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response

from web_services import FundNotFoundError, service


ROOT = Path(__file__).resolve().parent
INDEX_HTML = ROOT / "static" / "index.html"

app = FastAPI(
    title="净值雷达 Demo",
    version="0.1.0",
    docs_url="/api/docs",
    redoc_url=None,
)


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(INDEX_HTML)


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    return Response(status_code=204)


@app.get("/api/health")
def health() -> Dict[str, Any]:
    return {"status": "ok", "service": "fund-valuation-demo"}


@app.get("/api/funds/{code}/validate")
def validate_fund(code: str) -> Dict[str, Any]:
    try:
        return service.validate(code)
    except FundNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"基金数据源暂不可用：{exc}") from exc


@app.get("/api/funds/{code}/overview")
def fund_overview(code: str) -> Dict[str, Any]:
    try:
        return service.get_overview(code)
    except FundNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (requests.RequestException, ValueError) as exc:
        raise HTTPException(status_code=502, detail=f"获取基金信息失败：{exc}") from exc


@app.get("/api/funds/{code}/history")
def fund_history(code: str) -> Dict[str, Any]:
    try:
        return service.history_payload(code)
    except FundNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (requests.RequestException, ValueError) as exc:
        raise HTTPException(status_code=502, detail=f"获取历史净值失败：{exc}") from exc


@app.get("/api/funds/{code}/estimate")
def fund_estimate(
    code: str,
    equity_exposure: float = Query(default=0.90, gt=0, le=1),
) -> Dict[str, Any]:
    try:
        return service.estimate(code, equity_exposure)
    except FundNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (requests.RequestException, ValueError) as exc:
        return {
            "status": "unavailable",
            "code": code,
            "reason": str(exc),
            "disclaimer": "仍可查看基金基本信息和历史净值。",
        }
