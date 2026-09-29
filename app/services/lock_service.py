"""
app/services/lock_service.py
─────────────────────────────
Servicio de bloqueo pesimista sobre la tabla bloqueo_exclusion.

Responsabilidades:
  - Adquirir un bloqueo sobre un recurso para un usuario.
  - Liberar un bloqueo (explícito o por fuerza).
  - Verificar si un recurso está bloqueado.
  - Limpiar bloqueos expirados (usado por tarea periódica).
  - Renovar la expiración de un bloqueo activo (heartbeat).

Política de bloqueo:
  - Un recurso solo puede tener UN bloqueo activo a la vez.
  - Si el bloqueo existe pero ya expiró, se considera libre.
  - El propietario del bloqueo puede adquirirlo de nuevo (idempotente).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import and_, delete, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import BloqueoExclusion, TipoRecurso


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _expiracion() -> datetime:
    """Calcula el timestamp de expiración según la config."""
    return _now_utc() + timedelta(seconds=settings.LOCK_EXPIRATION_SECONDS)


# ══════════════════════════════════════════════════════════════════════════════
# OPERACIONES PRINCIPALES
# ══════════════════════════════════════════════════════════════════════════════

def obtener_bloqueo_activo(
    db: Session,
    id_recurso: str,
) -> Optional[BloqueoExclusion]:
    """
    Retorna el bloqueo vigente sobre el recurso, o None si no existe / expiró.
    """
    ahora = _now_utc()
    stmt = (
        select(BloqueoExclusion)
        .where(
            and_(
                BloqueoExclusion.id_recurso == id_recurso,
                BloqueoExclusion.expiracion > ahora,   # Solo bloqueos vivos
            )
        )
    )
    return db.scalars(stmt).first()


def adquirir_bloqueo(
    db: Session,
    id_recurso: str,
    tipo_recurso: TipoRecurso,
    id_usuario: str,
) -> tuple[bool, BloqueoExclusion]:
    """
    Intenta adquirir bloqueo exclusivo sobre `id_recurso` para `id_usuario`.

    Returns:
        (True,  bloqueo) si el bloqueo fue concedido.
        (False, bloqueo) si el recurso ya está bloqueado por OTRO usuario.

    Estrategia:
      1. Buscar bloqueo activo no expirado.
      2a. Si no existe → crear uno nuevo.
      2b. Si existe y es del mismo usuario → renovar expiración (idempotente).
      2c. Si existe y es de otro usuario → denegar.
    """
    bloqueo_existente = obtener_bloqueo_activo(db, id_recurso)

    if bloqueo_existente is None:
        # Limpiar registros expirados para este recurso antes de crear
        _purgar_expirados_de_recurso(db, id_recurso)

        nuevo = BloqueoExclusion(
            id_recurso=id_recurso,
            tipo_recurso=tipo_recurso,
            id_usuario_bloqueador=id_usuario,
            expiracion=_expiracion(),
        )
        db.add(nuevo)
        db.commit()
        db.refresh(nuevo)
        return True, nuevo

    if bloqueo_existente.id_usuario_bloqueador == id_usuario:
        # El mismo usuario: renovar expiración
        bloqueo_existente.expiracion = _expiracion()
        db.commit()
        db.refresh(bloqueo_existente)
        return True, bloqueo_existente

    # Otro usuario tiene el bloqueo → denegar
    return False, bloqueo_existente


def liberar_bloqueo(
    db: Session,
    id_recurso: str,
    id_usuario: str,
) -> Optional[BloqueoExclusion]:
    """
    Libera el bloqueo de `id_recurso` si pertenece a `id_usuario`.

    Returns:
        El bloqueo eliminado si existía y pertenecía al usuario, o None.
    Raises:
        PermissionError si el bloqueo pertenece a otro usuario.
    """
    bloqueo = obtener_bloqueo_activo(db, id_recurso)

    if bloqueo is None:
        return None  # Ya estaba libre (o expiró)

    if bloqueo.id_usuario_bloqueador != id_usuario:
        raise PermissionError(
            f"El recurso {id_recurso!r} está bloqueado por el usuario "
            f"{bloqueo.id_usuario_bloqueador!r}. No puedes liberarlo."
        )

    db.delete(bloqueo)
    db.commit()
    return bloqueo


def liberar_todos_los_bloqueos_de_usuario(
    db: Session,
    id_usuario: str,
) -> list[BloqueoExclusion]:
    """
    Libera TODOS los bloqueos activos de un usuario.
    Se llama cuando el cliente WebSocket se desconecta.

    Returns:
        Lista de bloqueos liberados (para poder broadcast a la sala).
    """
    ahora = _now_utc()
    stmt = select(BloqueoExclusion).where(
        and_(
            BloqueoExclusion.id_usuario_bloqueador == id_usuario,
            BloqueoExclusion.expiracion > ahora,
        )
    )
    bloqueos = list(db.scalars(stmt).all())

    for b in bloqueos:
        db.delete(b)

    db.commit()
    return bloqueos


def renovar_bloqueo(
    db: Session,
    id_recurso: str,
    id_usuario: str,
) -> Optional[BloqueoExclusion]:
    """
    Extiende la expiración de un bloqueo activo del usuario.
    Llamado periódicamente por el heartbeat WebSocket.

    Returns:
        El bloqueo renovado, o None si no existe.
    """
    bloqueo = obtener_bloqueo_activo(db, id_recurso)
    if bloqueo and bloqueo.id_usuario_bloqueador == id_usuario:
        bloqueo.expiracion = _expiracion()
        db.commit()
        db.refresh(bloqueo)
        return bloqueo
    return None


def limpiar_bloqueos_expirados(db: Session) -> list[BloqueoExclusion]:
    """
    Elimina todos los bloqueos cuya `expiracion` ya pasó.
    Retorna la lista de bloqueos eliminados para notificar a las salas.
    Llamado por la tarea periódica de limpieza.
    """
    ahora = _now_utc()
    stmt = select(BloqueoExclusion).where(BloqueoExclusion.expiracion <= ahora)
    expirados = list(db.scalars(stmt).all())

    for b in expirados:
        db.delete(b)

    db.commit()
    return expirados


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS PRIVADOS
# ══════════════════════════════════════════════════════════════════════════════

def _purgar_expirados_de_recurso(db: Session, id_recurso: str) -> None:
    """Elimina registros expirados de un recurso específico (limpieza puntual)."""
    ahora = _now_utc()
    db.execute(
        delete(BloqueoExclusion).where(
            and_(
                BloqueoExclusion.id_recurso == id_recurso,
                BloqueoExclusion.expiracion <= ahora,
            )
        )
    )
    db.commit()
