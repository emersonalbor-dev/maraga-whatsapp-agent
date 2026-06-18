# agent/ventas_api.py — Clientes en tiempo real: Walmart, MercadoLibre, Amazon (SP-API)

import os
import json
import time
import uuid
import base64
import hashlib
import hmac as hmac_lib
import logging
import asyncio
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
        for line in order.get("orderLines", []):
            prod = line.get("item", {}).get("productName", "Sin nombre")
            qty = int(float(line.get("orderLineQuantity", {}).get("amount", 1)))
            # Usar unitPrice * qty (precio CON IVA que ve el comprador)
            # consistente con el total del pedido (orderTotal ya incluye IVA)
            unit_price = float(line.get("item", {}).get("unitPrice", {}).get("amount", 0))
            line_total = unit_price * qty
            if line_total == 0:
                # Fallback: usar chargeAmount + tax si unitPrice no está disponible
                charges = line.get("charges", [])
                if charges:
                    charge = float(charges[0]["chargeAmount"]["amount"])
                    taxes = sum(float(t["taxAmount"]["amount"]) for t in charges[0].get("tax", []))
                    line_total = charge + taxes
            prod_agg[prod]["ingresos"] += line_total
            prod_agg[prod]["unidades"] += qty
            unidades += qty
        # total = suma de orderTotal (precio comprador con IVA) — consistente con prod_agg
        total += float(order.get("orderTotal", {}).get("amount", 0))

    ordenes = len(all_orders)
    top5 = sorted(
        [{"titulo": k, "ingresos": round(v["ingresos"], 2), "unidades": v["unidades"]}
         for k, v in prod_agg.items()],
        key=lambda x: x["ingresos"], reverse=True
    )[:5]

    today = datetime.now()
    logger.info(f"Walmart resumen: {ordenes} órdenes, total=${total:.2f} MXN (con IVA)")
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


async def get_ml_fresh_token() -> str:
    """Devuelve el access token de ML vigente, renovándolo si es necesario."""
    if not _ml_access_token or time.time() > _ml_token_exp - 60:
        await _refresh_ml_token()
    return _ml_access_token


# ─── ML BILLING (proxy server-side para evitar rate limit en browser) ──────────
_ml_billing_cache: dict = {}  # { "YYYY-MM": { ml:{}, mp:{}, publicidad:N, pages:N, _ts:N } }


async def _ml_billing_fetch_group(period_key: str, group: str) -> tuple[dict, int]:
    """Fetch y agrega todos los registros de billing de un grupo (ML o MP)."""
    acc: dict = defaultdict(float)
    pages = 0
    from_id = 0

    def _proc(rec: dict) -> tuple[str, float] | None:
        ci  = rec.get("charge_info") or {}
        t   = ci.get("detail_sub_type") or ""
        amt = abs(ci.get("detail_amount") or 0)
        grp = ci.get("detail_type") or ""
        if group == "ML":
            if t == "CV":                                       return ("cv", amt)
            if t == "CFF":                                      return ("envios_full", amt)
            if t in ("CXD", "CDSD"):                           return ("envios_xd", amt)
            if t in ("PADS", "CBADS"):                         return ("publicidad", amt)
            if t in ("CFWA","CFCB","CFRS","CFPB","CFBA"):      return ("cfwa", amt)
            if t == "CESM":                                     return ("cesm", amt)
            if t == "BV":                                       return ("bv", amt)
            if t in ("BFF", "BXD"):                            return ("benvios", amt)
            if grp == "BONUS":                                  return ("bv", amt)
            if grp == "CHARGE":                                 return ("otros", amt)
        else:  # MP
            if t == "CRIA":                                     return ("adelanto", amt)
            if t == "CPOPC":                                    return ("servicios_mp", amt)
            if grp == "CHARGE":                                 return ("otros", amt)
        return None

    while True:
        token = await get_ml_fresh_token()
        url = (
            f"{ML_BASE}/billing/integration/periods/key/{period_key}"
            f"/group/{group}/details"
        )
        params = {
            "document_type": "BILL",
            "limit": 1000,
            "from_id": from_id,
            "sort_by": "ID",
            "order_by": "ASC",
        }
        retries = 0
        while True:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.get(url, params=params,
                                headers={"Authorization": f"Bearer {token}"})
            if r.status_code == 429:
                wait = 30 * (retries + 1)
                logger.warning("ML billing 429 (%s/%s), esperando %ss", group, period_key, wait)
                await asyncio.sleep(wait)
                retries += 1
                token = await get_ml_fresh_token()
                continue
            if r.status_code == 401:
                await _refresh_ml_token()
                token = _ml_access_token
                continue
            r.raise_for_status()
            break

        data = r.json()
        results = data.get("results") or []
        pages += 1
        for rec in results:
            kv = _proc(rec)
            if kv:
                acc[kv[0]] += kv[1]

        if data.get("last_id") and len(results) > 0:
            from_id = data["last_id"]
            await asyncio.sleep(10)  # pausa entre páginas
        else:
            break

    return dict(acc), pages


async def fetch_ml_billing_aggregated(mes: str) -> dict:
    """Fetches, agrega y cachea billing de ML+MP para un mes YYYY-MM."""
    cur_mes = datetime.now(timezone.utc).strftime("%Y-%m")

    cached = _ml_billing_cache.get(mes)
    if cached:
        age_h = (time.time() * 1000 - cached.get("_ts", 0)) / 3_600_000
        if mes < cur_mes or age_h < 1:
            return cached

    period_key = mes + "-01"

    ml_acc, ml_pages = await _ml_billing_fetch_group(period_key, "ML")
    await asyncio.sleep(5)  # pausa entre grupos ML y MP
    mp_acc, _ = await _ml_billing_fetch_group(period_key, "MP")

    result = {
        "mes":        mes,
        "ml":         ml_acc,
        "mp":         mp_acc,
        "publicidad": ml_acc.get("publicidad", 0),
        "pages":      ml_pages,
        "_ts":        int(time.time() * 1000),  # ms para compatibilidad con Date.now()
    }
    _ml_billing_cache[mes] = result
    logger.info("ML billing %s cargado: %d pág. ML, publicidad=$%.0f", mes, ml_pages, result["publicidad"])
    return result


async def fetch_ml_mes(date_from: str, date_to: str) -> dict:
    LIMIT = 50
    offset = 0
    all_orders: list = []

    while True:
        data = await _ml_get("/orders/search", {
            "seller": ML_SELLER_ID,
            "order.date_closed.from": date_from,   # fecha de pago confirmado (= lo que muestra la plataforma)
            "order.date_closed.to": date_to,
            "sort": "date_desc",
            "limit": LIMIT,
            "offset": offset,
        })
        results = data.get("results", [])
        paging_total = data.get("paging", {}).get("total", 0)
        logger.info(f"ML page offset={offset}: {len(results)} resultados, paging.total={paging_total}")
        if not results:
            break
        all_orders.extend(results)
        offset += LIMIT

    prod_agg: dict = defaultdict(lambda: {"ingresos": 0.0, "unidades": 0})
    total = 0.0
    ordenes = 0
    unidades = 0
    canceladas = 0

    REJECTED = {"rejected", "refunded", "charged_back", "null"}
    for order in all_orders:
        if order.get("status") == "cancelled":
            canceladas += 1
            continue
        # Excluir órdenes donde todos los pagos fueron rechazados/reembolsados
        pagos = order.get("payments", [])
        if pagos and all(str(p.get("status", "")).lower() in REJECTED for p in pagos):
            canceladas += 1
            continue
        ordenes += 1
        # Usar unit_price * qty (= GMV que muestra la plataforma ML en "Ventas")
        for item in order.get("order_items", []):
            titulo = item.get("item", {}).get("title", "Sin nombre")
            qty = item.get("quantity", 1)
            price = float(item.get("unit_price", 0)) * qty
            prod_agg[titulo]["ingresos"] += price
            prod_agg[titulo]["unidades"] += qty
            unidades += qty
            total += price

    logger.info(
        f"ML resumen: {len(all_orders)} órdenes totales, "
        f"{ordenes} activas (unit_price*qty)=${total:.2f}, {canceladas} canceladas"
    )

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


# ─── AMAZON SP-API (tiempo real) ─────────────────────────────────────────────
AMZ_CLIENT_ID     = os.getenv("AMAZON_CLIENT_ID", "")
AMZ_CLIENT_SECRET = os.getenv("AMAZON_CLIENT_SECRET", "")
AMZ_REFRESH_TOKEN = os.getenv("AMAZON_REFRESH_TOKEN", "")
AMZ_AWS_KEY       = os.getenv("AWS_ACCESS_KEY_ID", "")
AMZ_AWS_SECRET    = os.getenv("AWS_SECRET_ACCESS_KEY", "")
AMZ_MARKETPLACE   = os.getenv("AMAZON_MARKETPLACE_ID", "A1AM78C64UM0Y8")
AMZ_SP_BASE       = "https://sellingpartnerapi-na.amazon.com"
AMZ_REGION        = "us-east-1"

_amz_lwa_token: str = ""
_amz_lwa_exp:   float = 0.0


async def _get_amz_lwa_token() -> str:
    global _amz_lwa_token, _amz_lwa_exp
    if _amz_lwa_token and time.time() < _amz_lwa_exp - 60:
        return _amz_lwa_token
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(
            "https://api.amazon.com/auth/o2/token",
            data={
                "grant_type":    "refresh_token",
                "refresh_token": AMZ_REFRESH_TOKEN,
                "client_id":     AMZ_CLIENT_ID,
                "client_secret": AMZ_CLIENT_SECRET,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
        )
        r.raise_for_status()
        d = r.json()
    _amz_lwa_token = d["access_token"]
    _amz_lwa_exp   = time.time() + d.get("expires_in", 3600)
    logger.info("Token LWA Amazon renovado")
    return _amz_lwa_token


def _amz_sigv4_headers(method: str, url: str, lwa_token: str, body: str = "") -> dict:
    """AWS Signature Version 4 para Amazon SP-API."""
    from urllib.parse import urlparse, urlencode
    parsed  = urlparse(url)
    now     = datetime.utcnow()
    amz_date   = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")

    # Canonical query string (ordenado)
    qs_pairs = sorted(parsed.query.split("&")) if parsed.query else []
    canonical_qs = "&".join(qs_pairs)

    # Headers canónicos
    headers_to_sign = {
        "host":               parsed.hostname,
        "x-amz-access-token": lwa_token,
        "x-amz-date":         amz_date,
    }
    canonical_headers = "".join(f"{k}:{v}\n" for k, v in sorted(headers_to_sign.items()))
    signed_headers    = ";".join(sorted(headers_to_sign.keys()))

    payload_hash = hashlib.sha256(body.encode()).hexdigest()
    canonical_req = "\n".join([
        method, parsed.path, canonical_qs,
        canonical_headers, signed_headers, payload_hash,
    ])

    cred_scope  = f"{date_stamp}/{AMZ_REGION}/execute-api/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256", amz_date, cred_scope,
        hashlib.sha256(canonical_req.encode()).hexdigest(),
    ])

    def _hmac(key, msg):
        return hmac_lib.new(key, msg.encode(), hashlib.sha256).digest()

    k_date    = _hmac(f"AWS4{AMZ_AWS_SECRET}".encode(), date_stamp)
    k_region  = _hmac(k_date, AMZ_REGION)
    k_service = _hmac(k_region, "execute-api")
    k_sign    = _hmac(k_service, "aws4_request")
    signature = hmac_lib.new(k_sign, string_to_sign.encode(), hashlib.sha256).hexdigest()

    auth = (
        f"AWS4-HMAC-SHA256 Credential={AMZ_AWS_KEY}/{cred_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return {
        "Authorization":      auth,
        "x-amz-access-token": lwa_token,
        "x-amz-date":         amz_date,
        "Content-Type":       "application/json",
        "Accept":             "application/json",
    }


async def _amz_get(path: str, params: dict = {}) -> dict:
    lwa = await _get_amz_lwa_token()
    from urllib.parse import urlencode
    qs  = ("?" + urlencode(params)) if params else ""
    url = f"{AMZ_SP_BASE}{path}{qs}"
    headers = _amz_sigv4_headers("GET", url, lwa)
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(url, headers=headers)
        if r.status_code >= 400:
            raise RuntimeError(f"SP-API {r.status_code}: {r.text[:200]}")
        return r.json()


async def fetch_amazon_mes(date_from: str, date_to: str) -> dict:
    """Obtiene órdenes de Amazon SP-API en tiempo real."""
    if not AMZ_AWS_KEY or AMZ_AWS_KEY == "PENDIENTE":
        raise RuntimeError("AWS_ACCESS_KEY_ID no configurado en Railway")
    if not AMZ_REFRESH_TOKEN or AMZ_REFRESH_TOKEN == "PENDIENTE":
        raise RuntimeError("AMAZON_REFRESH_TOKEN no configurado en Railway")

    all_orders = []
    next_token = None
    while True:
        params: dict = {
            "CreatedAfter":  date_from,
            "CreatedBefore": date_to,
            "MarketplaceIds": AMZ_MARKETPLACE,
            "MaxResultsPerPage": 100,
        }
        if next_token:
            params["NextToken"] = next_token
        data = await _amz_get("/orders/v0/orders", params)
        orders = data.get("payload", {}).get("Orders", [])
        all_orders.extend(orders)
        next_token = data.get("payload", {}).get("NextToken")
        if not next_token or not orders:
            break

    # Agregar items por orden (en paralelo por lotes de 10)
    import asyncio
    prod_agg: dict = defaultdict(lambda: {"ingresos": 0.0, "unidades": 0})
    total = 0.0
    unidades = 0

    async def _fetch_items(order_id: str):
        try:
            d = await _amz_get(f"/orders/v0/orders/{order_id}/orderItems")
            return d.get("payload", {}).get("OrderItems", [])
        except Exception:
            return []

    # Sólo órdenes no canceladas
    active = [o for o in all_orders if o.get("OrderStatus") not in ("Canceled", "Unfulfillable")]
    total = sum(float(o.get("OrderTotal", {}).get("Amount", 0)) for o in active)

    item_results = await asyncio.gather(*[_fetch_items(o["AmazonOrderId"]) for o in active])
    for items in item_results:
        for item in items:
            title = item.get("Title", "Sin nombre")
            qty   = int(item.get("QuantityOrdered", 1))
            price = float(item.get("ItemPrice", {}).get("Amount", 0))
            prod_agg[title]["ingresos"] += price
            prod_agg[title]["unidades"] += qty
            unidades += qty

    ordenes = len(active)
    top5 = sorted(
        [{"titulo": k, "ingresos": round(v["ingresos"], 2), "unidades": v["unidades"]}
         for k, v in prod_agg.items()],
        key=lambda x: x["ingresos"], reverse=True
    )[:5]

    today = datetime.now()
    logger.info(f"Amazon SP-API: {ordenes} órdenes activas, total=${total:.2f} MXN")
    return {
        "total":         round(total, 2),
        "ordenes":       ordenes,
        "unidades":      unidades,
        "ticketPromedio": round(total / ordenes) if ordenes else 0,
        "currency":      "MXN",
        "skus":          len(prod_agg),
        "parcial":       True,
        "parcialLabel":  f"al {today.day} de {MESES_ES[today.month]}",
        "top":           top5,
    }


# ─── AMAZON (caché en memoria + fallback a knowledge JSON) ────────────────────
_amazon_mem_cache: dict = {}   # { "2026-06": {...datos amazon...} }
_tableau_plat_cache: dict = {}  # { "2026-06": {"amazon": {...}, "maraga_mx": {...}} }


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
    """Devuelve datos de Amazon: Tableau cache primero, luego memory cache, luego JSON."""
    # 1) Tableau cache (actualizado por cron)
    tableau_entry = _tableau_plat_cache.get(mes, {})
    if "amazon" in tableau_entry:
        return tableau_entry["amazon"]
    # 2) Caché en memoria legacy
    if mes in _amazon_mem_cache:
        return _normalize_amazon(_amazon_mem_cache[mes])
    # 3) Fallback: archivo JSON
    try:
        ruta = os.path.join("knowledge", "ventas-dashboard-2026.json")
        with open(ruta, "r", encoding="utf-8") as f:
            data = json.load(f)
        raw = data.get("datos_por_mes", {}).get(mes, {}).get("amazon")
        return _normalize_amazon(raw)
    except Exception as e:
        logger.warning(f"No se pudo cargar Amazon cache de archivo: {e}")
        return None


def set_tableau_plat_cache(mes: str, platform: str, data: dict) -> None:
    """Guarda datos de Tableau para una plataforma en memoria."""
    if mes not in _tableau_plat_cache:
        _tableau_plat_cache[mes] = {}
    # Preservar top products si el nuevo push llega con lista vacía
    existing = _tableau_plat_cache[mes].get(platform, {})
    if not data.get("top") and existing.get("top"):
        data = {**data, "top": existing["top"]}
    _tableau_plat_cache[mes][platform] = data
    logger.info(f"Tableau cache [{platform}] actualizado para {mes}: total={data.get('total')}")


def get_maraga_mx_cached(mes: str) -> Optional[dict]:
    """Devuelve datos de MaragaMX desde caché de Tableau."""
    return _tableau_plat_cache.get(mes, {}).get("maraga_mx")


# ─── TIKTOK SHOP ──────────────────────────────────────────────────────────────
TTK_APP_KEY      = os.getenv("TIKTOK_APP_KEY", "")
TTK_APP_SECRET   = os.getenv("TIKTOK_APP_SECRET", "")
TTK_ACCESS_TOKEN = os.getenv("TIKTOK_ACCESS_TOKEN", "")
TTK_SHOP_CIPHER  = os.getenv("TIKTOK_SHOP_CIPHER", "")
TTK_BASE         = "https://open-api.tiktokglobalshop.com"


def _tiktok_sign(path: str, params: dict, body: str = "") -> str:
    excluded = {"sign", "access_token"}
    sorted_str = "".join(
        f"{k}{v}" for k, v in sorted(params.items())
        if k not in excluded and v is not None and v != ""
    )
    base = f"{TTK_APP_SECRET}{path}{sorted_str}{body}"
    return hmac_lib.new(TTK_APP_SECRET.encode(), base.encode(), hashlib.sha256).hexdigest()


async def _tiktok_post(path: str, body: dict) -> dict:
    ts = int(time.time())
    params: dict = {
        "app_key":      TTK_APP_KEY,
        "timestamp":    ts,
        "access_token": TTK_ACCESS_TOKEN,
    }
    if TTK_SHOP_CIPHER:
        params["shop_cipher"] = TTK_SHOP_CIPHER
    body_str = json.dumps(body, separators=(",", ":"))
    params["sign"] = _tiktok_sign(path, params, body_str)
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(
            f"{TTK_BASE}{path}",
            params=params,
            content=body_str,
            headers={"Content-Type": "application/json"},
        )
        r.raise_for_status()
        data = r.json()
    if data.get("code") != 0:
        raise RuntimeError(f"TikTok API {data.get('code')}: {data.get('message')}")
    return data.get("data", {})


async def fetch_tiktok_mes(ts_from: int, ts_to: int) -> dict:
    """Obtiene órdenes de TikTok Shop MX en tiempo real."""
    if not TTK_APP_KEY or not TTK_APP_SECRET or not TTK_ACCESS_TOKEN:
        raise RuntimeError("Credenciales TikTok no configuradas en Railway")

    cancelled = {"CANCELLED", "PARTIALLY_CANCELLING"}
    all_orders: list = []
    page_token: Optional[str] = None

    while True:
        body: dict = {
            "create_time_ge": ts_from,
            "create_time_lt": ts_to,
            "page_size": 50,
            "sort_field": "CREATE_TIME",
            "sort_order": "ASC",
        }
        if page_token:
            body["page_token"] = page_token
        data = await _tiktok_post("/api/v2/order/search", body)
        orders = data.get("orders") or []
        all_orders.extend(orders)
        page_token = data.get("next_page_token") or ""
        if not page_token or not orders:
            break

    prod_agg: dict = defaultdict(lambda: {"ingresos": 0.0, "unidades": 0})
    total    = 0.0
    unidades = 0
    ordenes  = 0

    for order in all_orders:
        if order.get("order_status") in cancelled:
            continue
        ordenes += 1
        pay = order.get("payment_info") or {}
        total += float(pay.get("total_amount", 0))
        for line in order.get("line_items") or []:
            name = line.get("product_name", "Sin nombre")
            qty  = int(line.get("quantity", 1))
            prod_agg[name]["ingresos"] += float(line.get("sale_price", 0)) * qty
            prod_agg[name]["unidades"] += qty
            unidades += qty

    top5 = sorted(
        [{"titulo": k, "ingresos": round(v["ingresos"], 2), "unidades": v["unidades"]}
         for k, v in prod_agg.items()],
        key=lambda x: x["ingresos"], reverse=True
    )[:5]

    today = datetime.now()
    logger.info(f"TikTok Shop: {ordenes} órdenes, total=${total:.2f} MXN")
    return {
        "total":          round(total, 2),
        "ordenes":        ordenes,
        "unidades":       unidades,
        "ticketPromedio": round(total / ordenes) if ordenes else 0,
        "currency":       "MXN",
        "skus":           len(prod_agg),
        "parcial":        True,
        "parcialLabel":   f"al {today.day} de {MESES_ES[today.month]}",
        "top":            top5,
    }


# ─── VTEX (MARAGA MX — venta directa) ─────────────────────────────────────────
VTEX_ACCOUNT     = os.getenv("VTEX_ACCOUNT_NAME", "")
VTEX_ENVIRONMENT = os.getenv("VTEX_ENVIRONMENT", "vtexcommercestable")
VTEX_APP_KEY     = os.getenv("VTEX_APP_KEY", "")
VTEX_APP_TOKEN   = os.getenv("VTEX_APP_TOKEN", "")


async def _vtex_get(path: str, params: dict = {}) -> dict:
    base = f"https://{VTEX_ACCOUNT}.{VTEX_ENVIRONMENT}.com.br"
    headers = {
        "X-VTEX-API-AppKey":   VTEX_APP_KEY,
        "X-VTEX-API-AppToken": VTEX_APP_TOKEN,
        "Accept":              "application/json",
    }
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(f"{base}{path}", params=params, headers=headers)
        r.raise_for_status()
        return r.json()


async def fetch_vtex_mes(date_from: str, date_to: str) -> dict:
    """Obtiene órdenes de VTEX (MaragaMX venta directa) en tiempo real."""
    if not VTEX_ACCOUNT or not VTEX_APP_KEY or not VTEX_APP_TOKEN:
        raise RuntimeError("Credenciales VTEX no configuradas en Railway")

    cancelled = {"canceled", "canceling"}
    date_filter = f"creationDate:[{date_from} TO {date_to}]"
    all_orders: list = []
    page = 1

    while True:
        data = await _vtex_get("/api/oms/pvt/orders", {
            "orderBy":        "creationDate,desc",
            "f_creationDate": date_filter,
            "page":           page,
            "per_page":       100,
        })
        orders = data.get("list", [])
        all_orders.extend(orders)
        paging = data.get("paging", {})
        if page >= paging.get("pages", 1) or not orders:
            break
        page += 1

    prod_agg: dict = defaultdict(lambda: {"ingresos": 0.0, "unidades": 0})
    total    = 0.0
    unidades = 0
    ordenes  = 0

    for order in all_orders:
        if order.get("status") in cancelled:
            continue
        ordenes += 1
        # VTEX value está en centavos
        total += float(order.get("value", 0)) / 100.0
        for item in order.get("items") or []:
            name  = item.get("description") or item.get("name", "Sin nombre")
            qty   = int(item.get("quantity", 1))
            price = float(item.get("sellingPrice", 0)) / 100.0 * qty
            prod_agg[name]["ingresos"] += price
            prod_agg[name]["unidades"] += qty
            unidades += qty

    top5 = sorted(
        [{"titulo": k, "ingresos": round(v["ingresos"], 2), "unidades": v["unidades"]}
         for k, v in prod_agg.items()],
        key=lambda x: x["ingresos"], reverse=True
    )[:5]

    today = datetime.now()
    logger.info(f"VTEX MaragaMX: {ordenes} órdenes, total=${total:.2f} MXN")
    return {
        "total":          round(total, 2),
        "ordenes":        ordenes,
        "unidades":       unidades,
        "ticketPromedio": round(total / ordenes) if ordenes else 0,
        "currency":       "MXN",
        "skus":           len(prod_agg),
        "parcial":        True,
        "parcialLabel":   f"al {today.day} de {MESES_ES[today.month]}",
        "top":            top5,
    }


# ─── AGREGADOR PRINCIPAL ──────────────────────────────────────────────────────
async def get_ventas_mes_actual() -> dict:
    now = datetime.now()
    mes_str = now.strftime("%Y-%m")

    # Rango: 1 del mes actual → hoy 23:59:59 (hora MX = UTC-6)
    inicio = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    fin    = now.replace(hour=23, minute=59, second=59, microsecond=0)

    # Formatos de fecha para cada API
    iso_from = inicio.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    iso_to   = fin.strftime("%Y-%m-%dT%H:%M:%S.999Z")
    wm_from  = inicio.strftime("%Y-%m-%dT%H:%M:%SZ")
    wm_to    = fin.strftime("%Y-%m-%dT%H:%M:%SZ")
    ml_from  = inicio.strftime("%Y-%m-%dT%H:%M:%S.000-06:00")
    ml_to    = fin.strftime("%Y-%m-%dT%H:%M:%S.000-06:00")
    amz_from = inicio.strftime("%Y-%m-%dT%H:%M:%SZ")
    amz_to   = fin.strftime("%Y-%m-%dT%H:%M:%SZ")
    ts_from  = int(inicio.timestamp())
    ts_to    = int(fin.timestamp())

    import asyncio
    results = await asyncio.gather(
        fetch_walmart_mes(wm_from, wm_to),
        fetch_ml_mes(ml_from, ml_to),
        fetch_amazon_mes(amz_from, amz_to),
        fetch_tiktok_mes(ts_from, ts_to),
        fetch_vtex_mes(iso_from, iso_to),
        return_exceptions=True,
    )

    walmart_data = results[0] if not isinstance(results[0], Exception) else None
    ml_data      = results[1] if not isinstance(results[1], Exception) else None
    amazon_live  = results[2] if not isinstance(results[2], Exception) else None
    tiktok_data  = results[3] if not isinstance(results[3], Exception) else None
    vtex_data    = results[4] if not isinstance(results[4], Exception) else None

    # Amazon y MaragaMX: fallback a caché de Tableau si la API directa falla
    amazon_data    = amazon_live or get_amazon_cached(mes_str)
    maraga_mx_data = vtex_data   or get_maraga_mx_cached(mes_str)

    walmart_error   = None
    ml_error        = None
    amazon_error    = None
    tiktok_error    = None
    maraga_mx_error = None

    if isinstance(results[0], Exception):
        walmart_error = str(results[0])
        logger.error(f"Error Walmart: {results[0]}")
    if isinstance(results[1], Exception):
        ml_error = str(results[1])
        logger.error(f"Error ML: {results[1]}")
    if isinstance(results[2], Exception):
        amazon_error = str(results[2])
        logger.warning(f"Amazon SP-API falló (usando caché): {results[2]}")
    if isinstance(results[3], Exception):
        tiktok_error = str(results[3])
        logger.error(f"Error TikTok: {results[3]}")
    if isinstance(results[4], Exception):
        maraga_mx_error = str(results[4])
        logger.error(f"Error VTEX MaragaMX: {results[4]}")

    return {
        "mes":            mes_str,
        "actualizado_at": now.isoformat(),
        "walmart":        walmart_data,
        "walmart_error":  walmart_error,
        "mercadolibre":   ml_data,
        "ml_error":       ml_error,
        "amazon":         amazon_data,
        "amazon_error":   amazon_error if not amazon_data else None,
        "tiktok":         tiktok_data,
        "tiktok_error":   tiktok_error,
        "maraga_mx":      maraga_mx_data,
        "maraga_mx_error": maraga_mx_error if not maraga_mx_data else None,
    }
