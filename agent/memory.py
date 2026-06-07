# agent/memory.py — Historial de conversaciones con SQLite/PostgreSQL

import os
from datetime import datetime
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import String, Text, DateTime, select, Integer, func, not_
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./maraga-agent.db")

# Railway usa postgresql://, asyncpg necesita postgresql+asyncpg://
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

engine = create_async_engine(DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class Mensaje(Base):
    """Mensaje en la base de datos."""
    __tablename__ = "mensajes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telefono: Mapped[str] = mapped_column(String(50), index=True)
    role: Mapped[str] = mapped_column(String(20))   # "user" o "assistant"
    content: Mapped[str] = mapped_column(Text)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


async def inicializar_db():
    """Crea las tablas si no existen."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def guardar_mensaje(telefono: str, role: str, content: str):
    """Guarda un mensaje en el historial."""
    async with async_session() as session:
        session.add(Mensaje(
            telefono=telefono,
            role=role,
            content=content,
            timestamp=datetime.utcnow()
        ))
        await session.commit()


async def obtener_historial(telefono: str, limite: int = 20) -> list[dict]:
    """
    Recupera los últimos N mensajes de una conversación en orden cronológico.
    """
    async with async_session() as session:
        query = (
            select(Mensaje)
            .where(Mensaje.telefono == telefono)
            .order_by(Mensaje.timestamp.desc())
            .limit(limite)
        )
        result = await session.execute(query)
        mensajes = result.scalars().all()
        mensajes.reverse()  # orden cronológico
        return [{"role": m.role, "content": m.content} for m in mensajes]


async def obtener_conversaciones_recientes(limite: int = 20) -> list[dict]:
    """
    Retorna la última conversación de cada contacto de WhatsApp,
    ordenadas por recencia. Excluye sesiones del dashboard.
    """
    async with async_session() as session:
        # Subquery: ID del último mensaje por teléfono
        sub = (
            select(
                Mensaje.telefono,
                func.max(Mensaje.id).label("last_id"),
            )
            .where(not_(Mensaje.telefono.like("dashboard-%")))
            .group_by(Mensaje.telefono)
            .subquery()
        )
        stmt = (
            select(Mensaje)
            .join(sub, Mensaje.id == sub.c.last_id)
            .order_by(Mensaje.timestamp.desc())
            .limit(limite)
        )
        result = await session.execute(stmt)
        rows = result.scalars().all()
        return [
            {
                "telefono": m.telefono,
                "ultimo_mensaje": m.content[:120],
                "role": m.role,
                "timestamp": m.timestamp.isoformat() + "Z",
                "pendiente": m.role == "user",
            }
            for m in rows
        ]


async def limpiar_historial(telefono: str):
    """Borra todo el historial de una conversación (útil para tests)."""
    async with async_session() as session:
        result = await session.execute(
            select(Mensaje).where(Mensaje.telefono == telefono)
        )
        for msg in result.scalars().all():
            await session.delete(msg)
        await session.commit()
