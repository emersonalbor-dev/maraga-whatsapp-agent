# agent/tools.py — Herramientas del agente Mara (Maraga)

import os
import json
import yaml
import logging
from datetime import datetime

logger = logging.getLogger("maraga-agent")

# Teléfonos de directores autorizados para consultas internas
# Configurar en .env: DIRECTOR_PHONES=+521234567890,+529876543210
_DIRECTORES = set(
    p.strip()
    for p in os.getenv("DIRECTOR_PHONES", "").split(",")
    if p.strip()
)


def es_director(telefono: str) -> bool:
    """Verifica si el número está en la lista de directores autorizados."""
    if not _DIRECTORES:
        # Si no hay directores configurados, cualquiera puede consultar
        # (útil para pruebas). Cambia esto en producción.
        return True
    return telefono in _DIRECTORES


def cargar_dashboard_ventas() -> dict:
    """Lee los datos de ventas desde knowledge/ventas-dashboard-2026.json."""
    ruta = os.path.join("knowledge", "ventas-dashboard-2026.json")
    try:
        with open(ruta, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        logger.warning(f"Archivo de dashboard no encontrado: {ruta}")
        return {}
    except json.JSONDecodeError as e:
        logger.error(f"Error leyendo dashboard JSON: {e}")
        return {}


def formatear_datos_mes(datos_mes: dict) -> str:
    """Convierte los datos de un mes a texto legible para el agente."""
    if not datos_mes:
        return "No hay datos disponibles para ese mes."

    label = datos_mes.get("label", "ese mes")
    lineas = [f"📊 *{label}*\n"]

    for plataforma in ["mercadolibre", "walmart", "amazon"]:
        d = datos_mes.get(plataforma)
        if not d:
            continue

        nombre = {
            "mercadolibre": "MercadoLibre",
            "walmart": "Walmart",
            "amazon": "Amazon"
        }[plataforma]

        nota = f" ({d['nota']})" if d.get("nota") else ""
        lineas.append(
            f"*{nombre}{nota}*: "
            f"${d['total']:,.0f} MXN | "
            f"{d['ordenes']} órdenes | "
            f"{d.get('unidades', '?')} uds"
        )

        tops = d.get("top_productos", [])
        if tops:
            lineas.append("  Top productos:")
            for i, p in enumerate(tops[:5], 1):
                uds = f" — {p['unidades']} uds" if p.get("unidades") else ""
                lineas.append(f"  {i}. {p['producto']}: ${p['ingresos']:,.0f}{uds}")

        lineas.append("")

    total = datos_mes.get("total_consolidado")
    if total:
        lineas.append(f"*Total consolidado: ${total:,.0f} MXN*")

    return "\n".join(lineas)


def consultar_ventas(mes: str | None = None) -> str:
    """
    Retorna datos de ventas para el mes especificado o el resumen general.

    Args:
        mes: Clave del mes (ej: "2026-05") o None para resumen global
    """
    data = cargar_dashboard_ventas()
    if not data:
        return "No pude cargar los datos del dashboard en este momento."

    if mes:
        datos_mes = data.get("datos_por_mes", {}).get(mes)
        if datos_mes:
            return formatear_datos_mes(datos_mes)
        else:
            meses_disponibles = list(data.get("datos_por_mes", {}).keys())
            return (
                f"No tengo datos para '{mes}'. "
                f"Meses disponibles: {', '.join(meses_disponibles)}"
            )

    # Sin mes específico — resumen de todos
    lineas = ["📊 *Resumen de ventas 2026*\n"]
    datos = data.get("datos_por_mes", {})
    for clave, info in datos.items():
        total = info.get("total_consolidado", 0)
        label = info.get("label", clave)
        lineas.append(f"• {label}: ${total:,.0f} MXN")

    resumen = data.get("resumen_2026", {})
    if resumen:
        lineas.append(f"\n🏆 Mejor mes ML: {resumen.get('mejor_mes_ml', 'N/A')}")
        lineas.append(f"🏆 Mejor mes Walmart: {resumen.get('mejor_mes_walmart', 'N/A')}")
        lineas.append(f"⭐ Producto estrella ML: {resumen.get('producto_estrella_ml', 'N/A')}")

    return "\n".join(lineas)


def buscar_en_knowledge(consulta: str) -> str:
    """
    Busca información relevante en los archivos del directorio /knowledge.
    Retorna extractos del contenido más relevante.
    """
    resultados = []
    knowledge_dir = "knowledge"

    if not os.path.exists(knowledge_dir):
        return ""

    for archivo in os.listdir(knowledge_dir):
        ruta = os.path.join(knowledge_dir, archivo)
        if archivo.startswith(".") or not os.path.isfile(ruta):
            continue
        # Saltar el JSON de ventas (se maneja por separado)
        if archivo == "ventas-dashboard-2026.json":
            continue
        try:
            with open(ruta, "r", encoding="utf-8") as f:
                contenido = f.read()
                if consulta.lower() in contenido.lower():
                    resultados.append(f"[{archivo}]:\n{contenido[:800]}")
        except (UnicodeDecodeError, IOError):
            continue

    return "\n---\n".join(resultados) if resultados else ""
