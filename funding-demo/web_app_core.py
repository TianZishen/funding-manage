"""基金估值 Web Demo：同一进程提供响应式页面和 API。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from portfolio_store import PortfolioNameExistsError, PortfolioStore
from web_services import FundNotFoundError, service


ROOT = Path(__file__).resolve().parent
INDEX_HTML = ROOT / "static" / "index.html"
DATA_DIR = Path(os.getenv("FUNDING_DATA_DIR", str(ROOT / "data")))
portfolio_store = PortfolioStore(DATA_DIR / "funding-rader.db")


class PortfolioCreate(BaseModel):
    name: str
    data: Dict[str, Any]


def validate_portfolio_data(data: Dict[str, Any]) -> None:
    if data.get("app") != "funding-rader" or data.get("version") != 2:
        raise HTTPException(status_code=400, detail="仅支持净值雷达 v2 数据格式")
    if not isinstance(data.get("codes"), list) or not isinstance(data.get("costs"), dict) or not isinstance(data.get("transactions"), dict):
        raise HTTPException(status_code=400, detail="投资组合数据结构不正确")

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


@app.get("/api/portfolios")
def list_portfolios() -> Dict[str, Any]:
    items = portfolio_store.list()
    return {"count": len(items), "items": items}


@app.post("/api/portfolios", status_code=201)
def create_portfolio(payload: PortfolioCreate) -> Dict[str, Any]:
    validate_portfolio_data(payload.data)
    try:
        return portfolio_store.create(payload.name, payload.data)
    except PortfolioNameExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/portfolios/{portfolio_id}")
def get_portfolio(portfolio_id: str) -> Dict[str, Any]:
    portfolio = portfolio_store.get(portfolio_id)
    if portfolio is None:
        raise HTTPException(status_code=404, detail="分类不存在")
    return portfolio


@app.put("/api/portfolios/{portfolio_id}")
def update_portfolio(portfolio_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
    validate_portfolio_data(data)
    portfolio = portfolio_store.update(portfolio_id, data)
    if portfolio is None:
        raise HTTPException(status_code=404, detail="分类不存在")
    return portfolio


@app.delete("/api/portfolios/{portfolio_id}", status_code=204)
def delete_portfolio(portfolio_id: str) -> Response:
    if len(portfolio_store.list()) <= 1:
        raise HTTPException(status_code=409, detail="至少需要保留一个分类")
    if not portfolio_store.delete(portfolio_id):
        raise HTTPException(status_code=404, detail="分类不存在")
    return Response(status_code=204)

@app.get("/api/funds/search")
def search_funds(q: str = "", limit: int = 8) -> Dict[str, Any]:
    try:
        items = service.search_funds(q, limit)
        return {"query": q, "count": len(items), "items": items}
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"基金目录暂不可用：{exc}") from exc

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
def fund_estimate(code: str) -> Dict[str, Any]:
    try:
        return service.estimate(code)
    except FundNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (requests.RequestException, ValueError) as exc:
        return {
            "status": "unavailable",
            "code": code,
            "reason": str(exc),
            "disclaimer": "仍可查看基金基本信息和历史净值。",
        }
