"""
app/services/connection_manager.py
────────────────────────────────────
ConnectionManager: gestiona todas las conexiones WebSocket activas
agrupadas por diagrama (cada diagrama es una "sala" de colaboración).

Responsabilidades:
  - Registrar y eliminar conexiones WS por diagrama.
  - Hacer broadcast a todos los participantes de una sala.
  - Enviar mensajes unicast a un usuario específico.
  - Rastrear qué usuario está detrás de cada WebSocket.
  - Exponer el mapa de sesiones activas para el lock service.

Diseño:
  - Singleton (instancia global importada por el router WS).
  - Thread-safe a nivel asyncio (no necesita locks ya que FastAPI/uvicorn
    corre en un único event loop de asyncio por defecto).
  - No guarda estado en DB — la BD solo guarda BloqueoExclusion.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from typing import Optional

from fastapi import WebSocket
from pydantic import BaseModel

logger = logging.getLogger(__name__)


# ── Registro de una sesión WebSocket activa ───────────────────────────────────
class SessionInfo:
    """Metadatos de una conexión WebSocket activa."""
    def __init__(self, websocket: WebSocket, id_usuario: str, id_diagrama: str):
        self.websocket   = websocket
        self.id_usuario  = id_usuario
        self.id_diagrama = id_diagrama

    def __repr__(self) -> str:
        return f"<Session usuario={self.id_usuario!r} diagrama={self.id_diagrama!r}>"


# ══════════════════════════════════════════════════════════════════════════════
# CONNECTION MANAGER
# ══════════════════════════════════════════════════════════════════════════════

class ConnectionManager:
    """
    Gestiona el ciclo de vida de las conexiones WebSocket.

    Estructura interna:
        _rooms: dict[id_diagrama, list[SessionInfo]]
            → Todas las sesiones activas agrupadas por sala (diagrama).

        _sessions: dict[WebSocket, SessionInfo]
            → Lookup inverso WebSocket → SessionInfo.
    """

    def __init__(self) -> None:
        # Sala → lista de sesiones activas
        self._rooms: dict[str, list[SessionInfo]] = defaultdict(list)
        # Lookup rápido: websocket → sesión
        self._sessions: dict[WebSocket, SessionInfo] = {}

    # ── Conexión / desconexión ────────────────────────────────────────────────

    async def connect(
        self,
        websocket: WebSocket,
        id_diagrama: str,
        id_usuario: str,
    ) -> SessionInfo:
        """
        Acepta la conexión WS y la registra en la sala del diagrama.
        Retorna el objeto SessionInfo creado.
        """
        await websocket.accept()
        session = SessionInfo(
            websocket=websocket,
            id_usuario=id_usuario,
            id_diagrama=id_diagrama,
        )
        self._rooms[id_diagrama].append(session)
        self._sessions[websocket] = session
        logger.info(
            "WS CONNECT  | usuario=%s | diagrama=%s | sala_size=%d",
            id_usuario, id_diagrama, len(self._rooms[id_diagrama]),
        )
        return session

    def disconnect(self, websocket: WebSocket) -> Optional[SessionInfo]:
        """
        Elimina la sesión del registro.
        Retorna el SessionInfo eliminado (para poder liberar bloqueos).
        Si la sala queda vacía la elimina del dict.
        """
        session = self._sessions.pop(websocket, None)
        if session is None:
            return None

        sala = self._rooms.get(session.id_diagrama, [])
        if session in sala:
            sala.remove(session)

        if not sala:
            self._rooms.pop(session.id_diagrama, None)

        logger.info(
            "WS DISCONNECT | usuario=%s | diagrama=%s",
            session.id_usuario, session.id_diagrama,
        )
        return session

    # ── Envío de mensajes ─────────────────────────────────────────────────────

    async def send_to(
        self,
        websocket: WebSocket,
        message: BaseModel | dict,
    ) -> None:
        """Envía un mensaje a UN cliente específico."""
        data = (
            message.model_dump_json()
            if isinstance(message, BaseModel)
            else json.dumps(message, default=str)
        )
        try:
            await websocket.send_text(data)
        except Exception as exc:
            logger.warning("send_to falló para %s: %s", websocket, exc)

    async def broadcast(
        self,
        id_diagrama: str,
        message: BaseModel | dict,
        exclude: Optional[WebSocket] = None,
    ) -> None:
        """
        Envía un mensaje a TODOS los clientes de una sala.

        Args:
            id_diagrama: Sala destino.
            message:     Mensaje Pydantic o dict.
            exclude:     WebSocket a excluir del broadcast (normalmente el emisor).
        """
        data = (
            message.model_dump_json()
            if isinstance(message, BaseModel)
            else json.dumps(message, default=str)
        )
        sesiones = self._rooms.get(id_diagrama, [])
        disconnected: list[SessionInfo] = []

        for session in sesiones:
            if session.websocket is exclude:
                continue
            try:
                await session.websocket.send_text(data)
            except Exception as exc:
                logger.warning(
                    "broadcast falló para usuario=%s: %s",
                    session.id_usuario, exc,
                )
                disconnected.append(session)

        # Limpiar sesiones muertas detectadas durante el broadcast
        for dead in disconnected:
            self._rooms[id_diagrama].remove(dead)
            self._sessions.pop(dead.websocket, None)

    async def broadcast_all(
        self,
        id_diagrama: str,
        message: BaseModel | dict,
    ) -> None:
        """Broadcast a TODOS, incluyendo el emisor."""
        await self.broadcast(id_diagrama, message, exclude=None)

    # ── Consultas de estado ───────────────────────────────────────────────────

    def get_session(self, websocket: WebSocket) -> Optional[SessionInfo]:
        """Retorna el SessionInfo asociado a un WebSocket."""
        return self._sessions.get(websocket)

    def get_users_in_room(self, id_diagrama: str) -> list[str]:
        """Lista de IDs de usuario activos en una sala."""
        return [s.id_usuario for s in self._rooms.get(id_diagrama, [])]

    def room_size(self, id_diagrama: str) -> int:
        """Número de conexiones activas en una sala."""
        return len(self._rooms.get(id_diagrama, []))

    def find_websocket_by_user(
        self,
        id_diagrama: str,
        id_usuario: str,
    ) -> Optional[WebSocket]:
        """Busca el WebSocket de un usuario en una sala (primera coincidencia)."""
        for session in self._rooms.get(id_diagrama, []):
            if session.id_usuario == id_usuario:
                return session.websocket
        return None


# ── Instancia global (singleton) ──────────────────────────────────────────────
manager = ConnectionManager()
