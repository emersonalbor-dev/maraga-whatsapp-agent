# agent/ventas_api.py — Clientes en tiempo real: Walmart, MercadoLibre, Amazon (cache)

import os
import json
import time
import uuid
import base64
import logging
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from typing import Optional
import httpx

logger = logging.getLogger("maraga-ventas")

MESES_ES = {
    1:"enero",2:"febrero",3:"marzo",4:"abril",5:"mayo",6:"junio",
    7:"julio",8:"agosto",9:"septiembre",10:"octubre",11:"noviembre",12:"diciembre"
}

# ─── WALMART ──────────────────────────────────────────────────────────────────
WM_CLIENT_ID     = os.getenv("WALMART_CLIENT_ID", "")
WM_CLIENT_SECRET = os.getenv("WALMART_CLIENT_SECRET", "")
WM_MARKET        = os.getenv("WALMART_MARKET", "mx")
WM_BASE          = "https://marketplace.walmartapis.com/v3"

_wm_token: Optional[str] = None
_wm_token_exp: float = 0.0


async def _get_walmart_token() -> str:
    global _wm_token, _wm_token_exp
    if _wm_token and time.time() < _wm_token_exp - 30:
        return _wm_token
    creds = base64.b64encode(f"{WM_CLIENT_ID}:{WM_CLIENT_SECRET}".encode()).decode()
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(
            f"{WM_BASE}/token",
            content="grant_type=client_credentials",
            headers={
                "Authorization": f"Basic {creds}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "WM_SVC.NAME": "Walmart Marketplace",
                "WM_QOS.CORRELATION_ID": str(uuid.uuid4()),
                "WM_MARKET": WM_MARKET,
            }
        )
        r.raise_for_status()
        data = r.json()
    _wm_token = data["access_token"]
    _wm_token_exp = time.time() + data.get("expires_in", 900)
    logger.info("Token Walmart renovado")
    return _wm_token


async def _walmart_get(path: str, params: dict = {}) -> dict:
    token = await _get_walmart_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "WM_SVC.NAME": "Walmart Marketplace",
        "WM_QOS.CORRELATION_ID": str(uuid.uuid4()),
        "WM_SEC.ACCESS_TOKEN": token,
        "WM_MARKET": WM_MARKET,
    }
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(f"{WM_BASE}{path}", params=params, headers=headers)
        r.raise_for_status()
        return r.json()


async def fetch_walmart_mes(date_from: str, date_to: str) -> dict:
    all_orders = []
    params: dict = {
        "createdStartDate": date_from,
        "createdEndDate": date_to,
        "limit": 100,
    }
    while True:
        data = await _walmart_get("/orders", params)
        batch = data.get("order", []) or []
        all_orders.extend(batch)
        cursor = data.get("meta", {}).get("nextCursorMark", "-1")
        if cursor == "-1" or not batch:
            break
        params["nextCursor"] = cursor

    prod_agg: dict = defaultdict(lambda: {"ingresos": 0.0, "unidades": 0})
    total = 0.0
    unidades = 0

    for order in all_orders:
        total += float(order.get("orderTotal", {}).get("amount", 0))
        for line in order.get("orderLines", []):
            prod = line.get("item", {}).get("productName", "Sin nombre")
            qty = int(float(line.get("orderLineQuantity", {}).get("amount", 1)))
            charges = line.get("charges", [])
            price = float(charges[0]["chargeAmount"]["amount"]) if charges else 0.0
            prod_agg[prod]["ingresos"] += price
            prod_agg[prod]["unidades"] += qty
            unidades += qty

    ordenes = len(all_orders)
    top5 = sorted(
        [{"titulo": k, "ingresos": round(v["ingresos"], 2), "unidades": v["unidades"]}
         for k, v in prod_agg.items()],
        key=lambda x: x["ingresos"], reverse=True
    )[:5]

    today = datetime.now()
    return {
        "total": round(total, 2),
        "ordenes": ordenes,
        "unidades": unidades,
        "ticketPromedio": round(total / ordenes) if ordenes else 0,
        "currency": "MXN",
        "skus": len(prod_agg),
        "parcial": True,
        "parcialLabel": f"al {today.day} de {MESES_ES[today.month]}",
        "top": top5,
    }


# ─── MERCADOLIBRE ─────────────────────────────────────────────────────────────
ML_CLIENT_ID     = os.getenv("ML_CLIENT_ID", "3229341112864987")
ML_CLIENT_SECRET = os.getenv("ML_CLIENT_SECRET", "")
ML_SELLER_ID     = os.getenv("ML_SELLER_ID", "244438069")
ML_BASE          = "https://api.mercadolibre.com"

_ml_access_token: str  = os.getenv("ML_ACCESS_TOKEN", "")
_ml_refresh_token: str = os.getenv("ML_REFRESH_TOKEN", "")
_ml_token_exp: float   = 0.0


async def _refresh_ml_token() -> None:
    global _ml_access_token, _ml_refresh_token, _ml_token_exp
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(
            f"{ML_BASE}/oauth/token",
            data={
                "grant_type": "refresh_token",
                "client_id": ML_CLIENT_ID,
                "client_secret": ML_CLIENT_SECRET,
                "refresh_token": _ml_refresh_token,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        r.raise_for_status()
        data = r.json()
    _ml_access_token  = data["access_token"]
    _ml_refresh_token = data["refresh_token"]
    _ml_token_exp     = time.time() + data.get("expires_in", 21600)
    logger.info("Token MercadoLibre renovado")


async def _ml_get(path: str, params: dict = {}) -> dict:
    global _ml_token_exp
    if not _ml_access_token or time.time() > _ml_token_exp - 60:
        await _refresh_ml_token()
    headers = {"Authorization": f"Bearer {_ml_access_token}"}
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(f"{ML_BASE}{path}", params=params, headers=headers)
        if r.status_code == 401:
            await _refresh_ml_token()
            r = await c.get(f"{ML_BASE}{path}",
                            params=params,
                            headers={"Authorization": f"Bearer {_ml_access_token}"})
        r.raise_for_status()
        return r.json()


async def fetch_ml_mes(date_from: str, date_to: str) -> dict:
    LIMIT = 50
    offset = 0
    all_orders: list = []

    while True:
        data = await _ml_get("/orders/search", {
            "seller": ML_SELLER_ID,
            "order.date_created.from": date_from,
            "order.date_created.to": date_to,
            "sort": "date_asc",
            "limit": LIMIT,
            "offset": offset,
        })
        results = data.get("results", [])
        if not results:
            break
        all_orders.extend(results)
        total_count = data.get("paging", {}).get("total", 0)
        offset += LIMIT
        if offset >= total_count:
            break

    prod_agg: dict = defaultdict(lambda: {"ingresos": 0.0, "unidades": 0})
    total = 0.0
    ordenes = 0
    unidades = 0

    for order in all_orders:
        if order.get("status") == "cancelled":
            continue
        ordenes += 1
        total += float(order.get("total_amount", 0))
        for item in order.get("order_items", []):
            titulo = item.get("item", {}).get("title", "Sin nombre")
            qty = item.get("quantity", 1)
            price = float(item.get("unit_price", 0)) * qty
            prod_agg[titulo]["ingresos"] += price
            prod_agg[titulo]["unidades"] += qty
            unidades += qty

    top5 = sorted(
        [{"titulo": k, "ingresos": round(v["ingresos"], 2), "unidades": v["unidades"]}
         for k, v in prod_agg.items()],
        key=lambda x: x["ingresos"], reverse=True
    )[:5]

    today = datetime.now()
    return {
        "total": round(total, 2),
        "ordenes": ordenes,
        "unidades": unidades,
        "ticketPromedio": round(total / ordenes) if ordenes else 0,
        "skus": len(prod_agg),
        "parcial": True,
        "parcialLabel": f"al {today.day} de {MESES_ES[today.month]}",
        "top": top5,
    }


# ─── AMAZON (caché en memoria + fallback a knowledge JSON) ────────────────────
_amazon_mem_cache: dict = {}   # { "2026-06": {...datos amazon...} }


def set_amazon_cache(mes: str, data: dict) -> None:
    """Guarda datos de Amazon en memoria (llamado desde el endpoint /amazon-push)."""
    _amazon_mem_cache[mes] = data
    logger.info(f"Amazon cache en memoria actualizado para {mes}: "
                f"total={data.get('total')}, ordenes={data.get('ordenes')}")


def _normalize_amazon(data: dict) -> dict:
    """Normaliza el objeto Amazon a las claves que espera el dashboard."""
    if not data:
        return data
    out = dict(data)
    # ticket_promedio → ticketPromedio
    if "ticket_promedio" in out and "ticketPromedio" not in out:
        out["ticketPromedio"] = out.pop("ticket_promedio")
    # top_productos → top (con campo titulo en lugar de producto)
    if "top_productos" in out and "top" not in out:
        out["top"] = [
            {"titulo": p.get("producto", p.get("titulo", "")),
             "ingresos": p.get("ingresos", 0),
             "unidades": p.get("unidades", 0)}
            for p in out.pop("top_productos")
        ]
    # nota → parcialLabel
    if "nota" in out and "parcialLabel" not in out:
        out["parcialLabel"] = out.pop("nota")
    if "parcialLabel" in out:
        out["parcial"] = True
    return out


def get_amazon_cached(mes: str) -> Optional[dict]:
    """Devuelve datos de Amazon: memoria primero, luego knowledge JSON."""
    # 1) Caché en memoria (actualizado por cron/push)
    if mes in _amazon_mem_cache:
        return _normalize_amazon(_amazon_mem_cache[mes])
    # 2) Fallback: archivo JSON (actualizado por cron vía git)
    try:
        ruta = os.path.join("knowledge", "ventas-dashboard-2026.json")
        with open(ruta, "r", encoding="utf-8") as f:
            data = json.load(f)
        raw = data.get("datos_por_mes", {}).get(mes, {}).get("amazon")
        return _normalize_amazon(raw)
    except Exception as e:
        logger.warning(f"No se pudo cargar Amazon cache de archivo: {e}")
        return None


# ─── AGREGADOR PRINCIPAL ──────────────────────────────────────────────────────
async def get_ventas_mes_actual() -> dict:
    now = datetime.now()
    mes_str = now.strftime("%Y-%m")

    # Rango: 1 del mes actual → hoy 23:59:59 (hora MX = UTC-6)
    inicio = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    fin    = now.replace(hour=23, minute=59, second=59, microsecond=0)

    # ISO 8601 para cada API
    wm_from = inicio.strftime("%Y-%m-%dT%H:%M:%SZ")
    wm_to   = fin.strftime("%Y-%m-%dT%H:%M:%SZ")
    ml_from = inicio.strftime("%Y-%m-%dT%H:%M:%S.000-06:00")
    ml_to   = fin.strftime("%Y-%m-%dT%H:%M:%S.000-06:00")

    import asyncio
    results = await asyncio.gather(
        fetch_walmart_mes(wm_from, wm_to),
        fetch_ml_mes(ml_from, ml_to),
        return_exceptions=True,
    )

    walmart_data = results[0] if not isinstance(results[0], Exception) else None
    ml_data      = results[1] if not isinstance(results[1], Exception) else None
    amazon_data  = get_amazon_cached(mes_str)

    if isinstance(results[0], Exception):
        logger.error(f"Error Walmart: {results[0]}")
    if isinstance(results[1], Exception):
        logger.error(f"Error ML: {results[1]}")

    return {
        "mes": mes_str,
        "actualizado_at": now.isoformat(),
        "walmart": walmart_data,
        "mercadolibre": ml_data,
        "amazon": amazon_data,
    }
