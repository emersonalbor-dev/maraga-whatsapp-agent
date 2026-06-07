# tests/test_local.py — Chat de prueba en terminal (sin WhatsApp real)
# Uso: python tests/test_local.py

import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Cargar .env antes de importar módulos del agente
from dotenv import load_dotenv
load_dotenv()

from agent.brain import generar_respuesta
from agent.memory import inicializar_db, guardar_mensaje, obtener_historial, limpiar_historial

TELEFONO_TEST = "test-director-001"


async def main():
    await inicializar_db()

    print()
    print("=" * 58)
    print("   Mara — Agente WhatsApp de Maraga | Test Local")
    print("=" * 58)
    print()
    print("  Escribe como si fueras un cliente o director.")
    print("  Comandos:")
    print("    'limpiar'  → borra el historial de conversación")
    print("    'salir'    → termina el test")
    print()
    print("-" * 58)
    print()

    while True:
        try:
            mensaje = input("Tú: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n\nTest finalizado.")
            break

        if not mensaje:
            continue
        if mensaje.lower() == "salir":
            print("\nTest finalizado.")
            break
        if mensaje.lower() == "limpiar":
            await limpiar_historial(TELEFONO_TEST)
            print("[Historial borrado]\n")
            continue

        historial = await obtener_historial(TELEFONO_TEST)
        print("\nMara: ", end="", flush=True)
        respuesta = await generar_respuesta(mensaje, historial)
        print(respuesta)
        print()

        await guardar_mensaje(TELEFONO_TEST, "user", mensaje)
        await guardar_mensaje(TELEFONO_TEST, "assistant", respuesta)


if __name__ == "__main__":
    asyncio.run(main())
