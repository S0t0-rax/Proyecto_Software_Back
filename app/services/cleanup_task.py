"""
app/services/cleanup_task.py
─────────────────────────────
Tarea de fondo que limpia bloqueos expirados periódicamente
y notifica a las salas afectadas vía WebSocket.

Se registra como lifespan task en main.py usando asyncio.
Intervalo configurable: LOCK_EXPIRATION_SECONDS / 2 (revisión frecuente).
"""
from __future__ import annotations

import asyncio
import logging

from app.core.config import settings
from app.db.database import SessionLocal
from app.schemas.ws_messages import LockExpiredMessage
from app.services import lock_service
from app.services.connection_manager import manager

logger = logging.getLogger(__name__)


async def cleanup_expired_locks() -> None:
    """
    Loop infinito que cada N segundos:
      1. Consulta bloqueos expirados en DB.
      2. Los elimina.
      3. Hace broadcast LOCK_EXPIRED a las salas afectadas.
    """
    interval = max(settings.LOCK_EXPIRATION_SECONDS // 2, 10)
    logger.info("Cleanup task iniciada | intervalo=%ds", interval)

    while True:
        await asyncio.sleep(interval)
        try:
            db = SessionLocal()
            try:
                expirados = lock_service.limpiar_bloqueos_expirados(db)
            finally:
                db.close()

            if expirados:
                logger.info("Bloqueos expirados eliminados: %d", len(expirados))

            # Notificar a cada sala afectada
            for bloqueo in expirados:
                # Necesitamos el id_diagrama del recurso.
                # La arquitectura actual no guarda id_diagrama en BloqueoExclusion,
                # así que notificamos a todas las salas que tengan al usuario.
                # En Fase 4, si se agrega id_diagrama a BloqueoExclusion,
                # este broadcast será más preciso.
                msg = LockExpiredMessage(
                    id_recurso=bloqueo.id_recurso,
                    tipo_recurso=bloqueo.tipo_recurso.value,
                    id_usuario_previo=bloqueo.id_usuario_bloqueador,
                )
                # Broadcast a todas las salas activas (bajo impacto si hay pocas)
                for id_diagrama in list(manager._rooms.keys()):
                    await manager.broadcast_all(id_diagrama, msg)

        except asyncio.CancelledError:
            logger.info("Cleanup task cancelada (shutdown)")
            break
        except Exception as exc:
            logger.exception("Error en cleanup_expired_locks: %s", exc)
