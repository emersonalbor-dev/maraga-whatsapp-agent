# agent/main.py — Servidor FastAPI + Webhook de WhatsApp para Mara (Maraga)

import os
import re
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

from agent.brain import generar_respuesta
from agent.memory import inicializar_db, guardar_mensaje, obtener_historial, obtener_conversaciones_recientes
from agent.providers import obtener_proveedor
from agent.ventas_api import get_ventas_mes_actual, set_amazon_cache, set_tableau_plat_cache, get_ml_fresh_token, fetch_ml_billing_aggregated

load_dotenv()

ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
logging.basicConfig(
    level=logging.DEBUG if ENVIRONMENT == "development" else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("maraga-agent")

proveedor = obtener_proveedor()
PORT = int(os.getenv("PORT", 8000))


@asynccontextmanager
async def lifespan(app: FastAPI):
    await inicializar_db()
    logger.info(f"🤖 Mara (Maraga Agent) iniciada — puerto {PORT}")
    logger.info(f"📱 Proveedor WhatsApp: {proveedor.__class__.__name__}")
    yield


app = FastAPI(
    title="Mara — Agente WhatsApp de Maraga",
    version="1.0.0",
    lifespan=lifespan
)

# CORS para el dashboard en GitHub Pages
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://emersonalbor-dev.github.io",
        "http://localhost:3000",
        "http://127.0.0.1:5500",
        "null",   # file:// en navegador local
    ],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


@app.get("/")
async def health_check():
    """Health check para Railway."""
    return {"status": "ok", "agente": "Mara", "marca": "Maraga"}


AMAZON_PUSH_SECRET   = os.getenv("AMAZON_PUSH_SECRET", "maraga-amazon-2026")
TABLEAU_PUSH_SECRET  = os.getenv("TABLEAU_PUSH_SECRET", "maraga-tableau-2026")
WHATSAPP_BRIDGE_URL  = os.getenv("WHATSAPP_BRIDGE_URL", "")  # URL del bridge QR


@app.post("/api/ventas/amazon-push")
async def amazon_push(request: Request):
    """
    Recibe datos de Amazon desde el cron de Claude Code (que consulta Porter MCP).
    Actualiza el caché en memoria para que /api/ventas/mes-actual lo sirva en tiempo real.
    Protegido con header X-Amazon-Secret.
    """
    auth = request.headers.get("X-Amazon-Secret", "")
    if auth != AMAZON_PUSH_SECRET:
        raise HTTPException(status_code=401, detail="No autorizado")
    try:
        body = await request.json()
        mes = body.get("mes")
        data = body.get("data")
        if not mes or not data:
            raise HTTPException(status_code=400, detail="Falta campo 'mes' o 'data'")
        set_amazon_cache(mes, data)
        logger.info(f"Amazon push recibido: {mes} total={data.get('total')}")
        return {"ok": True, "mes": mes, "total": data.get("total")}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error en /api/ventas/amazon-push: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/ventas/plataforma-push")
async def plataforma_push(request: Request):
    """
    Recibe datos de Amazon/MaragaMX desde el cron de Claude Code (Tableau MCP).
    Protegido con header X-Tableau-Secret.
    """
    auth = request.headers.get("X-Tableau-Secret", "")
    if auth != TABLEAU_PUSH_SECRET:
        raise HTTPException(status_code=401, detail="No autorizado")
    try:
        body = await request.json()
        mes = body.get("mes")
        if not mes:
            raise HTTPException(status_code=400, detail="Falta campo 'mes'")
        pushed = []
        for platform in ("amazon", "maraga_mx", "tiktok"):
            data = body.get(platform)
            if data:
                set_tableau_plat_cache(mes, platform, data)
                pushed.append(platform)
        logger.info(f"Tableau push recibido: {mes} plataformas={pushed}")
        return {"ok": True, "mes": mes, "plataformas": pushed}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error en /api/ventas/plataforma-push: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/ml/token")
async def ml_token():
    """
    Devuelve el access token de MercadoLibre vigente (auto-renovado por Railway).
    El frontend lo usa para hacer llamadas directas a la API de ML sin guardar client_secret.
    """
    from datetime import datetime, timezone
    token = await get_ml_fresh_token()
    return JSONResponse(content={
        "access_token": token,
        "actualizado_at": datetime.now(timezone.utc).isoformat(),
    })


@app.get("/api/ml/billing/{mes}")
async def ml_billing(mes: str):
    """
    Fetches y agrega billing de ML+MP para el mes YYYY-MM desde Railway.
    Evita que el browser haga llamadas directas al billing API de ML (rate limit muy estricto).
    """
    if not re.match(r'^\d{4}-\d{2}$', mes):
        raise HTTPException(status_code=400, detail="mes debe ser YYYY-MM")
    try:
        data = await fetch_ml_billing_aggregated(mes)
        return JSONResponse(content=data)
    except Exception as e:
        logger.error(f"Error en /api/ml/billing/{mes}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/ventas/plataforma-mes")
async def plataforma_mes(mes: str = ""):
    """
    Retorna Amazon + MaragaMX + TikTok para el mes solicitado (YYYY-MM).
    Consulta VTEX y TikTok Shop en tiempo real; usa caché como fallback.
    """
    if not mes or not re.match(r'^\d{4}-\d{2}$', mes):
        raise HTTPException(status_code=400, detail="Parámetro 'mes' requerido en formato YYYY-MM")
    import asyncio
    import calendar
    from datetime import datetime, timezone
    from agent.ventas_api import (
        get_amazon_cached, get_maraga_mx_cached, get_tiktok_cached,
        fetch_vtex_mes, fetch_tiktok_mes,
    )

    now  = datetime.now(timezone.utc)
    cur_mes = now.strftime("%Y-%m")
    year, month = int(mes[:4]), int(mes[5:7])
    inicio = datetime(year, month, 1, 0, 0, 0, tzinfo=timezone.utc)
    if mes == cur_mes:
        fin = now.replace(hour=23, minute=59, second=59, microsecond=0)
    else:
        last_day = calendar.monthrange(year, month)[1]
        fin = datetime(year, month, last_day, 23, 59, 59, tzinfo=timezone.utc)

    iso_from = inicio.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    iso_to   = fin.strftime("%Y-%m-%dT%H:%M:%S.999Z")
    ts_from  = int(inicio.timestamp())
    ts_to    = int(fin.timestamp())

    vtex_result, tiktok_result = await asyncio.gather(
        fetch_vtex_mes(iso_from, iso_to),
        fetch_tiktok_mes(ts_from, ts_to),
        return_exceptions=True,
    )

    amazon_data    = get_amazon_cached(mes)
    maraga_mx_data = vtex_result   if not isinstance(vtex_result,   Exception) else get_maraga_mx_cached(mes)
    tiktok_data    = tiktok_result if not isinstance(tiktok_result, Exception) else get_tiktok_cached(mes)

    maraga_mx_error = str(vtex_result)   if isinstance(vtex_result,   Exception) else None
    tiktok_error    = str(tiktok_result) if isinstance(tiktok_result, Exception) else None

    if maraga_mx_error:
        logger.warning(f"VTEX falló en plataforma-mes, usando caché: {maraga_mx_error}")
    if tiktok_error:
        logger.warning(f"TikTok falló en plataforma-mes: {tiktok_error}")

    return JSONResponse(content={
        "mes":              mes,
        "amazon":           amazon_data,
        "maraga_mx":        maraga_mx_data,
        "maraga_mx_error":  maraga_mx_error if not maraga_mx_data else None,
        "tiktok":           tiktok_data,
        "tiktok_error":     tiktok_error,
        "fuente":           "live",
        "actualizado_at":   now.isoformat(),
    })


@app.get("/api/auth/amazon")
async def amazon_auth_start():
    """Inicia el flujo OAuth de Amazon SP-API (LWA). Redirige a Amazon Seller Central."""
    import secrets
    from fastapi.responses import RedirectResponse, HTMLResponse
    client_id = os.getenv("AMAZON_CLIENT_ID", "")
    if not client_id:
        return HTMLResponse("<h2>Error: AMAZON_CLIENT_ID no configurado en Railway</h2>", status_code=500)
    state = secrets.token_hex(12)
    redirect_uri = "https://maraga-whatsapp-agent-production.up.railway.app/api/auth/amazon/callback"
    auth_url = (
        f"https://www.amazon.com/ap/oa"
        f"?client_id={client_id}"
        f"&scope=sellingpartnerapi::eforests"
        f"&response_type=code"
        f"&redirect_uri={redirect_uri}"
        f"&state={state}"
    )
    return RedirectResponse(url=auth_url)


@app.get("/api/auth/amazon/callback")
async def amazon_auth_callback(code: str = "", error: str = ""):
    """Recibe el código OAuth de Amazon y lo intercambia por refresh_token."""
    from fastapi.responses import HTMLResponse
    if error or not code:
        return HTMLResponse(f"<h2>Error de autorización: {error}</h2>", status_code=400)
    client_id     = os.getenv("AMAZON_CLIENT_ID", "")
    client_secret = os.getenv("AMAZON_CLIENT_SECRET", "")
    redirect_uri  = "https://maraga-whatsapp-agent-production.up.railway.app/api/auth/amazon/callback"
    import httpx as _httpx
    async with _httpx.AsyncClient(timeout=15) as c:
        r = await c.post(
            "https://api.amazon.com/auth/o2/token",
            data={
                "grant_type":    "authorization_code",
                "code":          code,
                "redirect_uri":  redirect_uri,
                "client_id":     client_id,
                "client_secret": client_secret,
            },
        )
    data = r.json()
    if "refresh_token" not in data:
        return HTMLResponse(f"<h2>Error al intercambiar código</h2><pre>{data}</pre>", status_code=400)
    refresh_token = data["refresh_token"]
    logger.info(f"Amazon refresh_token obtenido (primeros 20 chars): {refresh_token[:20]}...")
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Amazon Auth OK</title>
<style>body{{font-family:sans-serif;padding:40px;max-width:700px;margin:auto}}
pre{{background:#f4f4f4;padding:16px;border-radius:8px;word-break:break-all}}
.ok{{color:#1d9e75;font-size:18px;font-weight:700}}</style></head><body>
<div class="ok">✅ Amazon autorizado correctamente</div>
<h3>Guarda este valor en Railway como variable de entorno:</h3>
<p><strong>AMAZON_REFRESH_TOKEN</strong></p>
<pre>{refresh_token}</pre>
<p>Ve a <a href="https://railway.app" target="_blank">railway.app</a> → tu proyecto → Variables → agregar AMAZON_REFRESH_TOKEN con ese valor.</p>
</body></html>"""
    return HTMLResponse(html)


@app.get("/api/auth/tiktok")
async def tiktok_auth_start():
    """Inicia el flujo OAuth de TikTok Shop."""
    from fastapi.responses import RedirectResponse, HTMLResponse
    app_key = os.getenv("TIKTOK_APP_KEY", "")
    if not app_key:
        return HTMLResponse("<h2>Error: TIKTOK_APP_KEY no configurado en Railway</h2>", status_code=500)
    redirect_uri = "https://maraga-whatsapp-agent-production.up.railway.app/api/auth/tiktok/callback"
    import urllib.parse
    auth_url = (
        f"https://services.tiktokshop.com/open/authorize"
        f"?app_key={app_key}"
        f"&state=maraga"
        f"&redirect_uri={urllib.parse.quote(redirect_uri)}"
    )
    return RedirectResponse(url=auth_url)


@app.get("/api/auth/tiktok/callback")
async def tiktok_auth_callback(code: str = "", auth_code: str = "", error: str = ""):
    """Recibe el código OAuth de TikTok y lo intercambia por access_token."""
    from fastapi.responses import HTMLResponse
    auth_code = auth_code or code
    if error or not auth_code:
        return HTMLResponse(f"<h2>Error de autorización TikTok: {error or 'sin código'}</h2>", status_code=400)
    app_key    = os.getenv("TIKTOK_APP_KEY", "")
    app_secret = os.getenv("TIKTOK_APP_SECRET", "")
    import httpx as _httpx, json as _json, hashlib as _hashlib, hmac as _hmac, time as _time
    ts   = int(_time.time())
    path = "/api/v2/token/get"
    body = {"app_key": app_key, "app_secret": app_secret, "auth_code": auth_code, "grant_type": "authorized_code"}
    body_str = _json.dumps(body, separators=(",", ":"))
    excluded = {"sign", "access_token"}
    params   = {"app_key": app_key, "timestamp": ts}
    sorted_s = "".join(f"{k}{v}" for k, v in sorted(params.items()) if k not in excluded)
    base_str = f"{app_secret}{path}{sorted_s}{body_str}"
    sign     = _hmac.new(app_secret.encode(), base_str.encode(), _hashlib.sha256).hexdigest()
    params["sign"] = sign
    async with _httpx.AsyncClient(timeout=15) as c:
        r = await c.post(
            f"https://open-api.tiktokglobalshop.com{path}",
            params=params, content=body_str,
            headers={"Content-Type": "application/json"},
        )
    data = r.json()
    if data.get("code") != 0:
        return HTMLResponse(f"<h2>Error TikTok {data.get('code')}: {data.get('message')}</h2><pre>{data}</pre>", status_code=400)
    tok   = data.get("data", {})
    at    = tok.get("access_token", "")
    rt    = tok.get("refresh_token", "")
    # Get shop cipher
    shop_cipher = ""
    ts2   = int(_time.time())
    params2 = {"app_key": app_key, "timestamp": ts2, "access_token": at}
    sorted_s2 = "".join(f"{k}{v}" for k, v in sorted(params2.items()) if k not in {"sign","access_token"} and v)
    base_str2 = f"{app_secret}/api/v2/shop/get_authorized_shop{sorted_s2}"
    sign2 = _hmac.new(app_secret.encode(), base_str2.encode(), _hashlib.sha256).hexdigest()
    params2["sign"] = sign2
    try:
        async with _httpx.AsyncClient(timeout=15) as c:
            r2 = await c.get("https://open-api.tiktokglobalshop.com/api/v2/shop/get_authorized_shop", params=params2)
        shops_data = r2.json()
        shops = shops_data.get("data", {}).get("shops", [])
        if shops:
            shop_cipher = shops[0].get("shop_cipher", "")
    except Exception as e:
        shop_cipher = f"ERROR: {e}"
    logger.info(f"TikTok access_token obtenido: {at[:20]}...")
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>TikTok Auth OK</title>
<style>body{{font-family:sans-serif;padding:40px;max-width:700px;margin:auto}}
pre{{background:#f4f4f4;padding:16px;border-radius:8px;word-break:break-all}}
.ok{{color:#1d9e75;font-size:18px;font-weight:700}}</style></head><body>
<div class="ok">✅ TikTok Shop autorizado correctamente</div>
<h3>Guarda estos valores en Railway como variables de entorno:</h3>
<p><strong>TIKTOK_ACCESS_TOKEN</strong></p><pre>{at}</pre>
<p><strong>TIKTOK_REFRESH_TOKEN</strong> (para renovar)</p><pre>{rt}</pre>
<p><strong>TIKTOK_SHOP_CIPHER</strong></p><pre>{shop_cipher or '(no se pudo obtener — corre /api/auth/tiktok/shops después)'}</pre>
<p>Ve a <a href="https://railway.app" target="_blank">railway.app</a> → tu proyecto → Variables.</p>
</body></html>"""
    return HTMLResponse(html)


@app.get("/api/ventas/mes-actual")
async def ventas_mes_actual():
    """
    Retorna ventas en tiempo real del mes en curso para las 3 plataformas.
    Llamado desde el botón 'Actualizar' del dashboard directoros.
    """
    try:
        data = await get_ventas_mes_actual()
        return JSONResponse(content=data)
    except Exception as e:
        logger.error(f"Error en /api/ventas/mes-actual: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/chat")
async def chat_directo(request: Request):
    """
    Chat directo con Mara desde el dashboard de directores.
    No requiere WhatsApp — llama al brain directamente.
    """
    try:
        body = await request.json()
        mensaje = (body.get("mensaje") or "").strip()
        usuario = (body.get("usuario") or "director-dashboard").strip()
        if not mensaje:
            raise HTTPException(status_code=400, detail="Campo 'mensaje' requerido")

        # Historial de la sesión del director (ID separado del canal WhatsApp)
        session_id = f"dashboard-{usuario}"
        historial = await obtener_historial(session_id)

        # Generar respuesta con Mara
        respuesta = await generar_respuesta(mensaje, historial)

        # Guardar en memoria para contexto continuo
        await guardar_mensaje(session_id, "user", mensaje)
        await guardar_mensaje(session_id, "assistant", respuesta)

        logger.info(f"💬 Dashboard [{usuario}]: {mensaje[:60]}...")
        return JSONResponse(content={"respuesta": respuesta, "usuario": usuario})

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error en /api/chat: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/chats/whatsapp")
async def chats_whatsapp():
    """
    Retorna conversaciones recientes de WhatsApp desde la API de Twilio.
    Agrupa por contacto y muestra el último mensaje de cada hilo.
    """
    import base64
    import httpx as _httpx

    account_sid  = os.getenv("TWILIO_ACCOUNT_SID", "")
    auth_token   = os.getenv("TWILIO_AUTH_TOKEN", "")
    twilio_phone = os.getenv("TWILIO_PHONE_NUMBER", "+14155238886")
    wa_twilio    = f"whatsapp:{twilio_phone}"

    # ── Prioridad 1: Bridge QR (Baileys) ─────────────────────────────────────
    if WHATSAPP_BRIDGE_URL:
        try:
            import httpx as _httpx
            async with _httpx.AsyncClient(timeout=10) as c:
                r = await c.get(f"{WHATSAPP_BRIDGE_URL}/api/chats/whatsapp")
                return JSONResponse(content=r.json())
        except Exception as e:
            logger.warning(f"Bridge QR no disponible: {e}")

    # ── Prioridad 2: Twilio Messages API ──────────────────────────────────────
    if account_sid and auth_token:
        try:
            creds = base64.b64encode(f"{account_sid}:{auth_token}".encode()).decode()
            url   = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"

            async with _httpx.AsyncClient(timeout=15) as c:
                r = await c.get(
                    url,
                    params={"PageSize": 200},
                    headers={"Authorization": f"Basic {creds}"},
                )
                data = r.json()

            mensajes = data.get("messages", [])
            threads: dict = {}

            for msg in mensajes:
                frm       = msg.get("from", "")
                to        = msg.get("to", "")
                direction = msg.get("direction", "")

                # Solo WhatsApp
                if not (frm.startswith("whatsapp:") or to.startswith("whatsapp:")):
                    continue

                # Número del cliente (el que NO es Twilio)
                customer = frm if direction == "inbound" else to
                if customer == wa_twilio:
                    continue

                created = msg.get("date_created", "")
                if customer not in threads or created > threads[customer]["date_created"]:
                    threads[customer] = {
                        "telefono":        customer,
                        "ultimo_mensaje":  (msg.get("body") or "")[:120],
                        "role":            "user" if direction == "inbound" else "assistant",
                        "timestamp":       created,
                        "pendiente":       direction == "inbound",
                        "status":          msg.get("status", ""),
                        "date_created":    created,
                    }

            chats = sorted(threads.values(), key=lambda x: x["date_created"], reverse=True)[:20]
            # Limpiar campo interno antes de devolver
            for c in chats:
                c.pop("date_created", None)

            pendientes = sum(1 for c in chats if c["pendiente"])
            logger.info(f"Twilio chats: {len(chats)} hilos, {pendientes} pendientes")
            return JSONResponse(content={"chats": chats, "total": len(chats), "pendientes": pendientes})

        except Exception as e:
            logger.error(f"Error Twilio API en /api/chats/whatsapp: {e}", exc_info=True)

    # ── Fallback: BD local ────────────────────────────────────────────────────
    try:
        chats = await obtener_conversaciones_recientes(limite=20)
        pendientes = sum(1 for c in chats if c["pendiente"])
        return JSONResponse(content={"chats": chats, "total": len(chats), "pendientes": pendientes})
    except Exception as e2:
        logger.error(f"Error DB en /api/chats/whatsapp: {e2}", exc_info=True)
        return JSONResponse(content={"chats": [], "total": 0, "pendientes": 0})


@app.get("/webhook")
async def webhook_verificacion(request: Request):
    """Verificación GET del webhook (Meta Cloud API lo requiere)."""
    resultado = await proveedor.validar_webhook(request)
    if resultado is not None:
        return PlainTextResponse(str(resultado))
    return {"status": "ok"}


@app.post("/webhook")
async def webhook_handler(request: Request):
    """
    Recibe mensajes de WhatsApp, genera respuesta con Claude y la envía.
    Compatible con Twilio y Meta Cloud API.
    """
    try:
        mensajes = await proveedor.parsear_webhook(request)

        for msg in mensajes:
            if msg.es_propio or not msg.texto:
                continue

            logger.info(f"📨 [{msg.telefono}]: {msg.texto}")

            # Historial antes de guardar el mensaje actual
            historial = await obtener_historial(msg.telefono)

            # Generar respuesta con Claude
            respuesta = await generar_respuesta(msg.texto, historial)

            # Guardar en memoria
            await guardar_mensaje(msg.telefono, "user", msg.texto)
            await guardar_mensaje(msg.telefono, "assistant", respuesta)

            # Enviar por WhatsApp
            enviado = await proveedor.enviar_mensaje(msg.telefono, respuesta)
            if enviado:
                logger.info(f"✅ Respuesta enviada a {msg.telefono}")
            else:
                logger.warning(f"⚠️ No se pudo enviar a {msg.telefono}")

        return {"status": "ok"}

    except Exception as e:
        logger.error(f"Error en webhook: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
