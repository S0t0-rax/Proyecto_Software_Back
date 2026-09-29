"""
app/main.py
───────────
Punto de entrada de la aplicación FastAPI.
Registra routers, middlewares y lifespan tasks.
"""
from __future__ import annotations

import asyncio
import logging

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.db.database import Base, engine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


# ── Lifespan: startup / shutdown ──────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Acciones al arrancar y apagar la aplicación.
    Startup:
      - Crear tablas (solo en desarrollo; en producción usar Alembic).
      - Iniciar tarea de limpieza de bloqueos expirados.
    Shutdown:
      - Cancelar tarea de limpieza.
    """
    # Importación aquí para evitar circular imports
    from app.services.cleanup_task import cleanup_expired_locks

    logger.info("Iniciando ER Diagram API [%s]", settings.APP_ENV)

    # Crear tablas (dev únicamente)
    if settings.APP_ENV == "development":
        Base.metadata.create_all(bind=engine)
        logger.info("Tablas verificadas/creadas en la base de datos")

    # Iniciar tarea de fondo
    cleanup_task = asyncio.create_task(cleanup_expired_locks())
    logger.info("Cleanup task de bloqueos iniciada")

    yield  # ← La aplicación está corriendo aquí

    # Shutdown
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass
    logger.info("ER Diagram API detenida correctamente")


# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="ER Diagram Collaborative Tool",
    description=(
        "MVP de modelado colaborativo de diagramas Entidad-Relación. "
        "Soporta edición en tiempo real con bloqueo pesimista vía WebSockets, "
        "generación de proyectos Spring Boot y exportación XMI."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

# ── CORS ──────────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ───────────────────────────────────────────────────────────────────
from app.api.routes.ws        import router as ws_router        # WebSocket (Fase 3)
from app.api.routes.generator import router as generator_router  # Generador Spring Boot (Fase 4)
from app.api.routes.xmi       import router as xmi_router        # XMI Import/Export (Fase 5)
# from app.api.routes.proyectos import router as proyectos_router  # REST CRUD futuro
# from app.api.routes.diagramas import router as diagramas_router  # REST CRUD futuro
# from app.api.routes.entidades import router as entidades_router  # REST CRUD futuro

app.include_router(ws_router)
app.include_router(generator_router)
app.include_router(xmi_router)


# ── Endpoints base ────────────────────────────────────────────────────────────
@app.get("/health", tags=["Health"])
def health_check():
    """Comprobación de vida del servicio."""
    return {"status": "ok", "env": settings.APP_ENV}


@app.get("/ws/rooms", tags=["WebSocket"])
def list_active_rooms():
    """
    Lista las salas WebSocket activas y sus usuarios.
    Útil para debugging y monitoreo.
    """
    from app.services.connection_manager import manager
    return {
        "rooms": {
            diagram_id: manager.get_users_in_room(diagram_id)
            for diagram_id in manager._rooms
        }
    }
