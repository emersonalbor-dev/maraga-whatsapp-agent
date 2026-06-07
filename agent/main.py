# agent/main.py — Servidor FastAPI + Webhook de WhatsApp para Mara (Maraga)

import os
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

from agent.brain import generar_respuesta
from agent.memory import inicializar_db, guardar_mensaje, obtener_historial
from agent.providers import obtener_proveedor
from agent.ventas_api import get_ventas_mes_actual

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
