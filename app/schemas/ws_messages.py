"""
app/schemas/ws_messages.py
──────────────────────────
Schemas Pydantic que definen el protocolo de mensajes WebSocket.

FLUJO DE MENSAJES:
  Cliente → Servidor : LOCK_REQUEST, LOCK_RELEASE, PATCH, PING
  Servidor → Cliente : LOCK_GRANTED, LOCK_DENIED, LOCK_RELEASED,
                       LOCK_TAKEN, PATCH_BROADCAST, PONG, ERROR
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


# ══════════════════════════════════════════════════════════════════════════════
# TIPOS DE MENSAJES
# ══════════════════════════════════════════════════════════════════════════════

class ClientMessageType(str, Enum):
    """Tipos de mensaje que el CLIENTE puede enviar al servidor."""
    LOCK_REQUEST = "LOCK_REQUEST"   # Pedir bloqueo sobre un recurso
    LOCK_RELEASE = "LOCK_RELEASE"   # Liberar un bloqueo propio
    PATCH        = "PATCH"          # Enviar cambio a un recurso bloqueado
    PING         = "PING"           # Heartbeat


class ServerMessageType(str, Enum):
    """Tipos de mensaje que el SERVIDOR puede enviar al cliente."""
    LOCK_GRANTED    = "LOCK_GRANTED"    # Bloqueo concedido
    LOCK_DENIED     = "LOCK_DENIED"     # Bloqueo denegado (ya lo tiene otro)
    LOCK_RELEASED   = "LOCK_RELEASED"   # Broadcast: un bloqueo fue liberado
    LOCK_TAKEN      = "LOCK_TAKEN"      # Broadcast: otro usuario tomó un bloqueo
    LOCK_EXPIRED    = "LOCK_EXPIRED"    # Broadcast: bloqueo expiró por timeout
    PATCH_BROADCAST = "PATCH_BROADCAST" # Broadcast: cambio aplicado por otro usuario
    PONG            = "PONG"            # Respuesta de heartbeat
    ERROR           = "ERROR"           # Error genérico


# ══════════════════════════════════════════════════════════════════════════════
# MENSAJES CLIENTE → SERVIDOR
# ══════════════════════════════════════════════════════════════════════════════

class LockRequestMessage(BaseModel):
    """
    El cliente solicita bloqueo exclusivo sobre un recurso.
    Ejemplo:
        {"type": "LOCK_REQUEST", "id_recurso": "uuid", "tipo_recurso": "ENTIDAD"}
    """
    type: ClientMessageType = ClientMessageType.LOCK_REQUEST
    id_recurso: str   = Field(..., description="UUID del recurso a bloquear")
    tipo_recurso: str = Field(..., description="ENTIDAD | ATRIBUTO | RELACION")


class LockReleaseMessage(BaseModel):
    """
    El cliente libera un bloqueo que poseía.
    Ejemplo:
        {"type": "LOCK_RELEASE", "id_recurso": "uuid"}
    """
    type: ClientMessageType = ClientMessageType.LOCK_RELEASE
    id_recurso: str = Field(..., description="UUID del recurso a desbloquear")


class PatchMessage(BaseModel):
    """
    El cliente envía un cambio sobre un recurso que tiene bloqueado.
    `payload` es un dict libre con los campos modificados.
    Ejemplo:
        {"type": "PATCH", "id_recurso": "uuid", "payload": {"nombre": "NuevoNombre", "pos_x": 120}}
    """
    type: ClientMessageType = ClientMessageType.PATCH
    id_recurso: str        = Field(..., description="UUID del recurso modificado")
    tipo_recurso: str      = Field(..., description="ENTIDAD | ATRIBUTO | RELACION")
    payload: dict[str, Any] = Field(..., description="Campos y valores actualizados")


class PingMessage(BaseModel):
    type: ClientMessageType = ClientMessageType.PING


# ── Mensaje genérico de entrada (discriminado por 'type') ─────────────────────
class IncomingMessage(BaseModel):
    """
    Wrapper para parsear el tipo de cualquier mensaje entrante
    antes de hacer dispatch al handler correcto.
    """
    type: str
    id_recurso:   Optional[str]            = None
    tipo_recurso: Optional[str]            = None
    payload:      Optional[dict[str, Any]] = None


# ══════════════════════════════════════════════════════════════════════════════
# MENSAJES SERVIDOR → CLIENTE
# ══════════════════════════════════════════════════════════════════════════════

class LockGrantedMessage(BaseModel):
    type: ServerMessageType        = ServerMessageType.LOCK_GRANTED
    id_recurso: str
    tipo_recurso: str
    id_usuario: str
    expiracion: datetime           # El cliente sabe cuándo expira su bloqueo


class LockDeniedMessage(BaseModel):
    type: ServerMessageType        = ServerMessageType.LOCK_DENIED
    id_recurso: str
    tipo_recurso: str
    id_usuario_bloqueador: str     # Quién tiene el bloqueo actualmente
    expiracion: datetime           # Cuándo expira el bloqueo del otro usuario


class LockReleasedMessage(BaseModel):
    type: ServerMessageType        = ServerMessageType.LOCK_RELEASED
    id_recurso: str
    tipo_recurso: str
    id_usuario: str                # Quién liberó el bloqueo


class LockTakenMessage(BaseModel):
    type: ServerMessageType        = ServerMessageType.LOCK_TAKEN
    id_recurso: str
    tipo_recurso: str
    id_usuario: str                # Quién tomó el bloqueo
    expiracion: datetime


class LockExpiredMessage(BaseModel):
    type: ServerMessageType        = ServerMessageType.LOCK_EXPIRED
    id_recurso: str
    tipo_recurso: str
    id_usuario_previo: str


class PatchBroadcastMessage(BaseModel):
    type: ServerMessageType        = ServerMessageType.PATCH_BROADCAST
    id_recurso: str
    tipo_recurso: str
    id_usuario: str                # Quién realizó el cambio
    payload: dict[str, Any]


class PongMessage(BaseModel):
    type: ServerMessageType        = ServerMessageType.PONG
    timestamp: datetime


class ErrorMessage(BaseModel):
    type: ServerMessageType        = ServerMessageType.ERROR
    code: str
    detail: str
