# agent/brain.py — Cerebro del agente Mara: Claude API + contexto de negocio

import os
import json
import yaml
import logging
from anthropic import AsyncAnthropic
from dotenv import load_dotenv
from agent.tools import cargar_dashboard_ventas, formatear_datos_mes

load_dotenv()
logger = logging.getLogger("maraga-agent")

client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# Meses disponibles para detectar consultas de directores
MESES_MAP = {
    "enero": "2026-01", "ene": "2026-01",
    "febrero": "2026-02", "feb": "2026-02",
    "marzo": "2026-03", "mar": "2026-03",
    "abril": "2026-04", "abr": "2026-04",
    "mayo": "2026-05", "may": "2026-05",
    "junio": "2026-06", "jun": "2026-06",
}

KEYWORDS_DIRECTOR = {
    "cuánto vendimos", "cuanto vendimos", "ventas de", "ingresos de",
    "órdenes de", "ordenes de", "top productos", "top 5",
    "dashboard", "reporte", "informe", "métricas", "metricas",
    "mercadolibre vendió", "walmart vendió", "amazon vendió",
    "cuántas unidades", "cuantas unidades", "ticket promedio",
    "resumen de ventas", "cómo vamos", "como vamos",
    "mejor mes", "peor mes", "comparar meses"
}


def cargar_config() -> dict:
    """Lee config/prompts.yaml."""
    try:
        with open("config/prompts.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.error("config/prompts.yaml no encontrado")
        return {}


def cargar_system_prompt() -> str:
    return cargar_config().get(
        "system_prompt",
        "Eres Mara, asistente de Maraga. Responde siempre en español."
    )


def es_consulta_director(mensaje: str) -> bool:
    """Detecta si el mensaje es una consulta interna de director."""
    msg_lower = mensaje.lower()
    return any(kw in msg_lower for kw in KEYWORDS_DIRECTOR)


def extraer_mes(mensaje: str) -> str | None:
    """Extrae el mes mencionado en el mensaje (si hay uno)."""
    msg_lower = mensaje.lower()
    for palabra, clave in MESES_MAP.items():
        if palabra in msg_lower:
            return clave
    return None


def construir_contexto_director(mensaje: str) -> str:
    """
    Si el mensaje es una consulta de director, agrega datos del dashboard
    al contexto del system prompt para que Claude los use.
    """
    if not es_consulta_director(mensaje):
        return ""

    data = cargar_dashboard_ventas()
    if not data:
        return "\n[DATOS DE VENTAS: No disponibles en este momento]"

    mes = extraer_mes(mensaje)
    if mes:
        datos_mes = data.get("datos_por_mes", {}).get(mes)
        if datos_mes:
            return f"\n\n[DATOS DASHBOARD — {datos_mes.get('label', mes)}]\n{formatear_datos_mes(datos_mes)}"

    # Sin mes específico: resumen general
    lineas = ["\n\n[DATOS DASHBOARD — Resumen 2026]"]
    for clave, info in data.get("datos_por_mes", {}).items():
        total = info.get("total_consolidado", 0)
        label = info.get("label", clave)
        ml = info.get("mercadolibre", {}).get("total", 0)
        wmt = info.get("walmart", {}).get("total", 0)
        amz = info.get("amazon", {}).get("total", 0) if info.get("amazon") else 0
        lineas.append(
            f"• {label}: Total ${total:,.0f} | ML ${ml:,.0f} | WMT ${wmt:,.0f} | AMZ ${amz:,.0f}"
        )
    resumen = data.get("resumen_2026", {})
    if resumen:
        lineas.append(f"\nMejor mes ML: {resumen.get('mejor_mes_ml', 'N/A')}")
        lineas.append(f"Mejor mes Walmart: {resumen.get('mejor_mes_walmart', 'N/A')}")

    return "\n".join(lineas)


async def generar_respuesta(mensaje: str, historial: list[dict]) -> str:
    """
    Genera respuesta con Claude API.

    Args:
        mensaje: Mensaje actual del usuario
        historial: Conversación previa [{"role": ..., "content": ...}]

    Returns:
        Texto de respuesta del agente
    """
    cfg = cargar_config()

    if not mensaje or len(mensaje.strip()) < 2:
        return cfg.get("fallback_message", "Disculpa, ¿puedes reformular tu mensaje?")

    # System prompt base + datos de dashboard si aplica
    system_prompt = cargar_system_prompt()
    contexto_extra = construir_contexto_director(mensaje)
    if contexto_extra:
        system_prompt += contexto_extra
        logger.info("Modo director activado — datos de dashboard inyectados")

    # Construir mensajes
    mensajes = [{"role": m["role"], "content": m["content"]} for m in historial]
    mensajes.append({"role": "user", "content": mensaje})

    try:
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=system_prompt,
            messages=mensajes
        )
        respuesta = response.content[0].text
        logger.info(
            f"Tokens: {response.usage.input_tokens} in / {response.usage.output_tokens} out"
        )
        return respuesta

    except Exception as e:
        logger.error(f"Error Claude API: {e}")
        return cfg.get(
            "error_message",
            "Lo siento, estoy teniendo un problema técnico. Por favor intenta de nuevo. 🔧"
        )
