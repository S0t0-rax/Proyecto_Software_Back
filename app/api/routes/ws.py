"""
app/api/routes/ws.py
─────────────────────
Endpoint WebSocket para edición colaborativa de diagramas ER.

URL:  ws://<host>/ws/diagram/{id_diagrama}?user_id=<uuid>

Protocolo de mensajes (ver app/schemas/ws_messages.py):
  ┌─────────────────┬───────────────────────────────────────────────────────┐
  │ Dirección       │ Tipos                                                 │
  ├─────────────────┼───────────────────────────────────────────────────────┤
  │ Cliente→Servidor│ LOCK_REQUEST, LOCK_RELEASE, PATCH, PING              │
  │ Servidor→Cliente│ LOCK_GRANTED, LOCK_DENIED, LOCK_RELEASED,            │
  │                 │ LOCK_TAKEN, LOCK_EXPIRED, PATCH_BROADCAST,            │
  │                 │ PONG, ERROR                                           │
  └─────────────────┴───────────────────────────────────────────────────────┘

Flujo de bloqueo pesimista:
  1. Cliente envía LOCK_REQUEST {id_recurso, tipo_recurso}.
  2. Servidor llama lock_service.adquirir_bloqueo().
     a. Éxito → LOCK_GRANTED (unicast al solicitante)
                + LOCK_TAKEN (broadcast al resto de la sala).
     b. Fallo  → LOCK_DENIED (unicast, incluye quién lo tiene y cuándo expira).
  3. Cliente envía PATCH (solo si tiene el bloqueo).
     → Servidor valida, aplica y hace PATCH_BROADCAST a la sala.
  4. Cliente envía LOCK_RELEASE o se desconecta.
     → Servidor libera bloqueo en DB + LOCK_RELEASED broadcast.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from sqlalchemy.orm import Session

from app.db.database import SessionLocal
from app.db.models import TipoRecurso
from app.schemas.ws_messages import (
    ClientMessageType,
    ErrorMessage,
    IncomingMessage,
    LockDeniedMessage,
    LockExpiredMessage,
    LockGrantedMessage,
    LockReleasedMessage,
    LockTakenMessage,
    PatchBroadcastMessage,
    PongMessage,
)
from app.services import lock_service
from app.services.connection_manager import manager

logger = logging.getLogger(__name__)
router = APIRouter(tags=["WebSocket"])


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _get_db() -> Session:
    """Crea una sesión de BD síncrona (WebSocket no usa Depends)."""
    return SessionLocal()


def _parse_tipo_recurso(raw: str) -> TipoRecurso | None:
    """Convierte string a TipoRecurso enum, o None si es inválido."""
    try:
        return TipoRecurso(raw.upper())
    except ValueError:
        return None


async def _send_error(ws: WebSocket, code: str, detail: str) -> None:
    await manager.send_to(ws, ErrorMessage(code=code, detail=detail))


# ══════════════════════════════════════════════════════════════════════════════
# HANDLERS DE MENSAJES
# ══════════════════════════════════════════════════════════════════════════════

async def _handle_lock_request(
    ws: WebSocket,
    msg: IncomingMessage,
    id_usuario: str,
    id_diagrama: str,
) -> None:
    """Procesa una solicitud de bloqueo sobre un recurso."""
    if not msg.id_recurso or not msg.tipo_recurso:
        await _send_error(ws, "INVALID_PAYLOAD", "Faltan id_recurso o tipo_recurso")
        return

    tipo = _parse_tipo_recurso(msg.tipo_recurso)
    if tipo is None:
        await _send_error(
            ws, "INVALID_TIPO_RECURSO",
            f"tipo_recurso debe ser ENTIDAD, ATRIBUTO o RELACION. Recibido: {msg.tipo_recurso!r}",
        )
        return

    db = _get_db()
    try:
        concedido, bloqueo = lock_service.adquirir_bloqueo(
            db=db,
            id_recurso=msg.id_recurso,
            tipo_recurso=tipo,
            id_usuario=id_usuario,
        )
    finally:
        db.close()

    if concedido:
        # Notificar al solicitante que obtuvo el bloqueo
        await manager.send_to(
            ws,
            LockGrantedMessage(
                id_recurso=bloqueo.id_recurso,
                tipo_recurso=bloqueo.tipo_recurso.value,
                id_usuario=id_usuario,
                expiracion=bloqueo.expiracion,
            ),
        )
        # Notificar al resto de la sala que el recurso fue tomado
        await manager.broadcast(
            id_diagrama=id_diagrama,
            message=LockTakenMessage(
                id_recurso=bloqueo.id_recurso,
                tipo_recurso=bloqueo.tipo_recurso.value,
                id_usuario=id_usuario,
                expiracion=bloqueo.expiracion,
            ),
            exclude=ws,
        )
        logger.info(
            "LOCK GRANTED | recurso=%s | usuario=%s | diagrama=%s",
            msg.id_recurso, id_usuario, id_diagrama,
        )
    else:
        # Recurso bloqueado por otro usuario
        await manager.send_to(
            ws,
            LockDeniedMessage(
                id_recurso=bloqueo.id_recurso,
                tipo_recurso=bloqueo.tipo_recurso.value,
                id_usuario_bloqueador=bloqueo.id_usuario_bloqueador,
                expiracion=bloqueo.expiracion,
            ),
        )
        logger.info(
            "LOCK DENIED  | recurso=%s | bloqueador=%s | solicitante=%s",
            msg.id_recurso, bloqueo.id_usuario_bloqueador, id_usuario,
        )


async def _handle_lock_release(
    ws: WebSocket,
    msg: IncomingMessage,
    id_usuario: str,
    id_diagrama: str,
) -> None:
    """Procesa la liberación explícita de un bloqueo."""
    if not msg.id_recurso:
        await _send_error(ws, "INVALID_PAYLOAD", "Falta id_recurso")
        return

    db = _get_db()
    try:
        bloqueo = lock_service.liberar_bloqueo(
            db=db,
            id_recurso=msg.id_recurso,
            id_usuario=id_usuario,
        )
    except PermissionError as e:
        await _send_error(ws, "PERMISSION_DENIED", str(e))
        db.close()
        return
    finally:
        db.close()

    if bloqueo:
        await manager.broadcast_all(
            id_diagrama=id_diagrama,
            message=LockReleasedMessage(
                id_recurso=bloqueo.id_recurso,
                tipo_recurso=bloqueo.tipo_recurso.value,
                id_usuario=id_usuario,
            ),
        )
        logger.info(
            "LOCK RELEASED | recurso=%s | usuario=%s | diagrama=%s",
            msg.id_recurso, id_usuario, id_diagrama,
        )


async def _handle_patch(
    ws: WebSocket,
    msg: IncomingMessage,
    id_usuario: str,
    id_diagrama: str,
) -> None:
    """
    Procesa un cambio enviado por el cliente.
    Valida que el usuario tenga el bloqueo antes de aceptar el patch.
    """
    if not msg.id_recurso or not msg.payload:
        await _send_error(ws, "INVALID_PAYLOAD", "Faltan id_recurso o payload")
        return

    db = _get_db()
    try:
        bloqueo = lock_service.obtener_bloqueo_activo(db, msg.id_recurso)
    finally:
        db.close()

    # Verificar que el emisor tiene el bloqueo
    if bloqueo is None or bloqueo.id_usuario_bloqueador != id_usuario:
        await _send_error(
            ws,
            "LOCK_REQUIRED",
            f"Debes tener el bloqueo de {msg.id_recurso!r} para modificarlo. "
            "Envía primero un LOCK_REQUEST.",
        )
        return

    tipo_str = bloqueo.tipo_recurso.value

    # ── Aquí se conectaría el CRUD service (Fase 4) ───────────────────────────
    # Por ahora se hace broadcast del patch tal cual para que el frontend
    # actualice su estado local (CRDT/last-write-wins en el canvas).
    # En Fase 4 se aplicará la mutación real en la base de datos.

    await manager.broadcast(
        id_diagrama=id_diagrama,
        message=PatchBroadcastMessage(
            id_recurso=msg.id_recurso,
            tipo_recurso=tipo_str,
            id_usuario=id_usuario,
            payload=msg.payload,
        ),
        exclude=ws,  # El emisor ya aplicó el cambio localmente
    )
    logger.info(
        "PATCH        | recurso=%s | usuario=%s | campos=%s",
        msg.id_recurso, id_usuario, list(msg.payload.keys()),
    )


async def _handle_ping(ws: WebSocket, id_usuario: str) -> None:
    """Responde a heartbeat y renueva bloqueos activos del usuario."""
    await manager.send_to(
        ws,
        PongMessage(timestamp=datetime.now(timezone.utc)),
    )


async def _liberar_bloqueos_al_desconectar(
    id_usuario: str,
    id_diagrama: str,
) -> None:
    """Libera todos los bloqueos del usuario y notifica a la sala."""
    db = _get_db()
    try:
        bloqueos_liberados = lock_service.liberar_todos_los_bloqueos_de_usuario(
            db=db,
            id_usuario=id_usuario,
        )
    finally:
        db.close()

    for bloqueo in bloqueos_liberados:
        await manager.broadcast_all(
            id_diagrama=id_diagrama,
            message=LockReleasedMessage(
                id_recurso=bloqueo.id_recurso,
                tipo_recurso=bloqueo.tipo_recurso.value,
                id_usuario=id_usuario,
            ),
        )
        logger.info(
            "AUTO-RELEASE  | recurso=%s | usuario=%s (desconexión)",
            bloqueo.id_recurso, id_usuario,
        )


# ══════════════════════════════════════════════════════════════════════════════
# ENDPOINT WEBSOCKET
# ══════════════════════════════════════════════════════════════════════════════

@router.websocket("/ws/diagram/{id_diagrama}")
async def websocket_diagram(
    websocket: WebSocket,
    id_diagrama: str,
    user_id: str = Query(..., description="UUID del usuario que se conecta"),
):
    """
    Endpoint principal de colaboración en tiempo real.

    Conexión:
        ws://localhost:8000/ws/diagram/<diagram_uuid>?user_id=<user_uuid>

    Ciclo de vida:
        1. Aceptar conexión y registrar en la sala.
        2. Loop: recibir mensaje → dispatch al handler → responder/broadcast.
        3. Al desconectar (normal o por error): liberar bloqueos y limpiar sala.
    """
    session = await manager.connect(
        websocket=websocket,
        id_diagrama=id_diagrama,
        id_usuario=user_id,
    )
    logger.info(
        "SALA %s | usuarios activos: %s",
        id_diagrama, manager.get_users_in_room(id_diagrama),
    )

    try:
        while True:
            # ── Recibir texto (JSON) ───────────────────────────────────────────
            raw = await websocket.receive_text()

            try:
                data = json.loads(raw)
                msg  = IncomingMessage(**data)
            except (json.JSONDecodeError, Exception) as exc:
                await _send_error(websocket, "PARSE_ERROR", str(exc))
                continue

            # ── Dispatch por tipo de mensaje ──────────────────────────────────
            match msg.type:
                case ClientMessageType.LOCK_REQUEST:
                    await _handle_lock_request(websocket, msg, user_id, id_diagrama)

                case ClientMessageType.LOCK_RELEASE:
                    await _handle_lock_release(websocket, msg, user_id, id_diagrama)

                case ClientMessageType.PATCH:
                    await _handle_patch(websocket, msg, user_id, id_diagrama)

                case ClientMessageType.PING:
                    await _handle_ping(websocket, user_id)

                case _:
                    await _send_error(
                        websocket,
                        "UNKNOWN_MESSAGE_TYPE",
                        f"Tipo de mensaje no reconocido: {msg.type!r}",
                    )

    except WebSocketDisconnect:
        logger.info("WebSocketDisconnect | usuario=%s | diagrama=%s", user_id, id_diagrama)
    except Exception as exc:
        logger.exception("Error inesperado en WS | usuario=%s: %s", user_id, exc)
    finally:
        # Siempre limpiar, aunque sea por error
        manager.disconnect(websocket)
        await _liberar_bloqueos_al_desconectar(user_id, id_diagrama)
