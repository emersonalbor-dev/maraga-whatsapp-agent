# agent/main.py — Servidor FastAPI + Webhook de WhatsApp para Mara (Maraga)

import os
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

from agent.brain import generar_respuesta
from agent.memory import inicializar_db, guardar_mensaje, obtener_historial, obtener_conversaciones_recientes
from agent.providers import obtener_proveedor
from agent.ventas_api import get_ventas_mes_actual, set_amazon_cache

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


AMAZON_PUSH_SECRET = os.getenv("AMAZON_PUSH_SECRET", "maraga-amazon-2026")


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

    # ── Intento principal: Twilio Messages API ────────────────────────────────
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
