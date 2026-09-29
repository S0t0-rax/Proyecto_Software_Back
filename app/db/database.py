"""
app/db/database.py
──────────────────
Configuración del motor SQLAlchemy y fábrica de sesiones.
Se usa como dependencia (Depends) en los endpoints de FastAPI.
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

from app.core.config import settings

# ── Motor de conexión ─────────────────────────────────────────────────────────
# Detecta el driver para configurar opciones específicas por base de datos
_is_sqlite = settings.DATABASE_URL.startswith("sqlite")

_engine_kwargs: dict = {
    "echo": (settings.APP_ENV == "development"),
}
if _is_sqlite:
    # SQLite requiere check_same_thread=False para usarse en FastAPI/threads
    _engine_kwargs["connect_args"] = {"check_same_thread": False}
else:
    # PostgreSQL: verificar conexión antes de usarla (útil con Supabase)
    _engine_kwargs["pool_pre_ping"] = True

engine = create_engine(settings.DATABASE_URL, **_engine_kwargs)

# ── Fábrica de sesiones ───────────────────────────────────────────────────────
SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
)


# ── Clase base declarativa ────────────────────────────────────────────────────
class Base(DeclarativeBase):
    """Clase base de la que heredan todos los modelos SQLAlchemy."""
    pass


# ── Dependencia FastAPI ───────────────────────────────────────────────────────
def get_db():
    """
    Generador que provee una sesión de base de datos por request.
    Garantiza el cierre correcto de la sesión incluso ante excepciones.

    Uso en un endpoint:
        def mi_endpoint(db: Session = Depends(get_db)): ...
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
