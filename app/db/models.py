"""
app/db/models.py
────────────────
Modelos SQLAlchemy que mapean el esquema relacional completo del sistema.

Convenciones:
  - UUIDs como clave primaria (server_default con gen_random_uuid()).
  - Nombres de tablas en snake_case.
  - Enumeraciones nativas de PostgreSQL via SQLAlchemy Enum.
  - Relaciones bidireccionales declaradas para facilitar el ORM.
  - Timestamps de auditoría (created_at) en tablas clave.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Column, String, Boolean, Float, DateTime,
    ForeignKey, Enum as SAEnum, Text, event,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy import TypeDecorator
from sqlalchemy.orm import relationship
import enum

from app.db.database import Base, _is_sqlite


# ── Tipo UUID portátil (PostgreSQL nativo ó String en SQLite) ─────────────────
class PortableUUID(TypeDecorator):
    """UUID guardado como texto en SQLite y como UUID nativo en PostgreSQL."""
    impl = String(36)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return str(value)

    def process_result_value(self, value, dialect):
        return str(value) if value else None


def UUID(as_uuid=False):  # noqa: N802 — reemplaza la función de psycopg2
    """Retorna el tipo UUID adecuado según el motor activo."""
    if _is_sqlite:
        return PortableUUID()
    from sqlalchemy.dialects.postgresql import UUID as PG_UUID
    return PG_UUID(as_uuid=as_uuid)


def _make_enum(enum_class, name):
    """Crea un Enum SQLAlchemy compatible con SQLite (nativo=False) y PostgreSQL."""
    if _is_sqlite:
        return SAEnum(enum_class, name=name)          # Sin create_type en SQLite
    return SAEnum(enum_class, name=name, create_type=True)  # Tipo nativo PostgreSQL


# ══════════════════════════════════════════════════════════════════════════════
# ENUMERACIONES
# ══════════════════════════════════════════════════════════════════════════════

class TipoCardinalidad(str, enum.Enum):
    """Cardinalidad de una relación entre entidades del diagrama."""
    UNO_A_UNO   = "1:1"
    UNO_A_N     = "1:N"
    N_A_UNO     = "N:1"
    N_A_M       = "N:M"


class TipoRecurso(str, enum.Enum):
    """Tipos de recursos que pueden ser bloqueados para exclusión mutua."""
    ENTIDAD   = "ENTIDAD"
    ATRIBUTO  = "ATRIBUTO"
    RELACION  = "RELACION"


class EstadoComando(str, enum.Enum):
    """Estado de procesamiento de un comando proveniente de la app móvil."""
    PENDIENTE  = "PENDIENTE"
    APLICADO   = "APLICADO"
    CONFLICTO  = "CONFLICTO"


class TipoArchivo(str, enum.Enum):
    """Tipo de artefacto generado o importado."""
    ZIP_SPRING_BOOT = "ZIP_SPRING_BOOT"
    XMI_EXPORT      = "XMI_EXPORT"
    XMI_IMPORT      = "XMI_IMPORT"


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _uuid() -> str:
    """Genera un nuevo UUID v4 como string. Usado como default en Python."""
    return str(uuid.uuid4())


def _now() -> datetime:
    """Retorna el instante actual en UTC (timezone-aware)."""
    return datetime.now(timezone.utc)


# ══════════════════════════════════════════════════════════════════════════════
# MODELOS
# ══════════════════════════════════════════════════════════════════════════════

class Usuario(Base):
    """
    Representa a un usuario registrado en el sistema.
    Un usuario puede ser creador de múltiples proyectos,
    poseer bloqueos activos y enviar comandos móviles.
    """
    __tablename__ = "usuario"

    id = Column(
        UUID(as_uuid=False),
        primary_key=True,
        default=_uuid,
        comment="Identificador único del usuario (UUID v4)",
    )
    nombre = Column(String(120), nullable=False)
    email  = Column(String(255), nullable=False, unique=True, index=True)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)

    # ── Relaciones ─────────────────────────────────────────────────────────────
    proyectos_creados = relationship(
        "Proyecto",
        back_populates="creador",
        cascade="all, delete-orphan",
    )
    bloqueos_activos = relationship(
        "BloqueoExclusion",
        back_populates="usuario_bloqueador",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<Usuario id={self.id!r} email={self.email!r}>"


# ─────────────────────────────────────────────────────────────────────────────

class Proyecto(Base):
    """
    Agrupa uno o más diagramas ER bajo un nombre de proyecto.
    Cada proyecto pertenece a un usuario creador.
    """
    __tablename__ = "proyecto"

    id = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    nombre = Column(String(200), nullable=False)
    id_creador = Column(
        UUID(as_uuid=False),
        ForeignKey("usuario.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)

    # ── Relaciones ─────────────────────────────────────────────────────────────
    creador   = relationship("Usuario", back_populates="proyectos_creados")
    diagramas = relationship(
        "Diagrama",
        back_populates="proyecto",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<Proyecto id={self.id!r} nombre={self.nombre!r}>"


# ─────────────────────────────────────────────────────────────────────────────

class Diagrama(Base):
    """
    Diagrama Entidad-Relación perteneciente a un proyecto.
    Contiene entidades, relaciones y artefactos generados.
    """
    __tablename__ = "diagrama"

    id = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    id_proyecto = Column(
        UUID(as_uuid=False),
        ForeignKey("proyecto.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    nombre     = Column(String(200), nullable=False)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=_now,
        onupdate=_now,
        nullable=False,
    )

    # ── Relaciones ─────────────────────────────────────────────────────────────
    proyecto          = relationship("Proyecto", back_populates="diagramas")
    entidades         = relationship(
        "EntidadConceptual",
        back_populates="diagrama",
        cascade="all, delete-orphan",
    )
    relaciones        = relationship(
        "Relacion",
        back_populates="diagrama",
        cascade="all, delete-orphan",
    )
    cola_comandos     = relationship(
        "ColaComandosMovil",
        back_populates="diagrama",
        cascade="all, delete-orphan",
    )
    artefactos        = relationship(
        "ArchivoArtefacto",
        back_populates="diagrama",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<Diagrama id={self.id!r} nombre={self.nombre!r}>"


# ─────────────────────────────────────────────────────────────────────────────

class EntidadConceptual(Base):
    """
    Nodo visual del diagrama ER.
    Almacena posición (x, y) para renderizado en el canvas de Angular/JointJS.
    """
    __tablename__ = "entidad_conceptual"

    id = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    id_diagrama = Column(
        UUID(as_uuid=False),
        ForeignKey("diagrama.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    nombre = Column(String(120), nullable=False)
    pos_x  = Column(Float, nullable=False, default=0.0,
                    comment="Posición X en el canvas (píxeles)")
    pos_y  = Column(Float, nullable=False, default=0.0,
                    comment="Posición Y en el canvas (píxeles)")

    # ── Relaciones ─────────────────────────────────────────────────────────────
    diagrama   = relationship("Diagrama", back_populates="entidades")
    atributos  = relationship(
        "Atributo",
        back_populates="entidad",
        cascade="all, delete-orphan",
    )

    # Una entidad puede ser origen o destino de relaciones
    relaciones_como_origen  = relationship(
        "Relacion",
        foreign_keys="[Relacion.id_origen]",
        back_populates="entidad_origen",
    )
    relaciones_como_destino = relationship(
        "Relacion",
        foreign_keys="[Relacion.id_destino]",
        back_populates="entidad_destino",
    )

    def __repr__(self) -> str:
        return f"<EntidadConceptual id={self.id!r} nombre={self.nombre!r}>"


# ─────────────────────────────────────────────────────────────────────────────

class Atributo(Base):
    """
    Columna/campo de una entidad conceptual.
    Soporta marcado de PK, FK y tipo de dato libre (string).
    """
    __tablename__ = "atributo"

    id = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    id_entidad = Column(
        UUID(as_uuid=False),
        ForeignKey("entidad_conceptual.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    nombre     = Column(String(120), nullable=False)
    tipo_dato  = Column(String(60),  nullable=False,
                        comment="Ej: VARCHAR(255), INTEGER, BOOLEAN, TIMESTAMP")
    es_pk      = Column(Boolean, nullable=False, default=False,
                        comment="¿Es clave primaria?")
    es_fk      = Column(Boolean, nullable=False, default=False,
                        comment="¿Es clave foránea?")

    # ── Relaciones ─────────────────────────────────────────────────────────────
    entidad = relationship("EntidadConceptual", back_populates="atributos")

    def __repr__(self) -> str:
        return (
            f"<Atributo id={self.id!r} nombre={self.nombre!r} "
            f"tipo={self.tipo_dato!r} pk={self.es_pk} fk={self.es_fk}>"
        )


# ─────────────────────────────────────────────────────────────────────────────

class Relacion(Base):
    """
    Arco dirigido entre dos EntidadConceptual dentro de un diagrama.
    La cardinalidad se almacena como un Enum PostgreSQL nativo.
    """
    __tablename__ = "relacion"

    id = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    id_diagrama = Column(
        UUID(as_uuid=False),
        ForeignKey("diagrama.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    id_origen = Column(
        UUID(as_uuid=False),
        ForeignKey("entidad_conceptual.id", ondelete="CASCADE"),
        nullable=False,
    )
    id_destino = Column(
        UUID(as_uuid=False),
        ForeignKey("entidad_conceptual.id", ondelete="CASCADE"),
        nullable=False,
    )
    tipo_cardinalidad = Column(
        _make_enum(TipoCardinalidad, "tipo_cardinalidad_enum"),
        nullable=False,
        comment="Cardinalidad: 1:1 | 1:N | N:1 | N:M",
    )
    nombre = Column(String(120), nullable=True,
                    comment="Nombre semántico opcional de la relación (ej: 'pertenece_a')")

    # ── Relaciones ─────────────────────────────────────────────────────────────
    diagrama        = relationship("Diagrama", back_populates="relaciones")
    entidad_origen  = relationship(
        "EntidadConceptual",
        foreign_keys=[id_origen],
        back_populates="relaciones_como_origen",
    )
    entidad_destino = relationship(
        "EntidadConceptual",
        foreign_keys=[id_destino],
        back_populates="relaciones_como_destino",
    )

    def __repr__(self) -> str:
        return (
            f"<Relacion id={self.id!r} "
            f"{self.id_origen!r} --[{self.tipo_cardinalidad}]--> {self.id_destino!r}>"
        )


# ─────────────────────────────────────────────────────────────────────────────

class BloqueoExclusion(Base):
    """
    Registro de bloqueo pesimista sobre un recurso del diagrama.

    Cuando un usuario toma el control de una entidad/atributo/relación,
    se inserta un registro aquí. Los WebSockets consultan esta tabla
    para rechazar ediciones concurrentes sobre el mismo recurso.

    El campo `expiracion` permite liberar bloqueos muertos
    si el cliente WebSocket se desconecta sin liberarlos explícitamente.
    """
    __tablename__ = "bloqueo_exclusion"

    id = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    id_recurso = Column(
        UUID(as_uuid=False),
        nullable=False,
        index=True,
        comment="UUID del recurso bloqueado (entidad, atributo o relación)",
    )
    tipo_recurso = Column(
        _make_enum(TipoRecurso, "tipo_recurso_enum"),
        nullable=False,
    )
    id_usuario_bloqueador = Column(
        UUID(as_uuid=False),
        ForeignKey("usuario.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    expiracion = Column(
        DateTime(timezone=True),
        nullable=False,
        comment="Timestamp UTC en el que el bloqueo expira automáticamente",
    )
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)

    # ── Relaciones ─────────────────────────────────────────────────────────────
    usuario_bloqueador = relationship("Usuario", back_populates="bloqueos_activos")

    def __repr__(self) -> str:
        return (
            f"<BloqueoExclusion recurso={self.id_recurso!r} "
            f"tipo={self.tipo_recurso} usuario={self.id_usuario_bloqueador!r} "
            f"expira={self.expiracion}>"
        )


# ─────────────────────────────────────────────────────────────────────────────

class ColaComandosMovil(Base):
    """
    Cola de eventos enviados desde la aplicación móvil en modo offline.

    La app móvil almacena comandos localmente y los envía al backend
    cuando recupera conexión. El backend los aplica en orden o marca
    CONFLICTO si detecta inconsistencias con el estado actual del diagrama.
    """
    __tablename__ = "cola_comandos_movil"

    id = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    id_diagrama = Column(
        UUID(as_uuid=False),
        ForeignKey("diagrama.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    texto_comando = Column(
        Text,
        nullable=False,
        comment="Comando serializado (JSON string) enviado por la app móvil",
    )
    estado = Column(
        _make_enum(EstadoComando, "estado_comando_enum"),
        nullable=False,
        default=EstadoComando.PENDIENTE,
    )
    created_at  = Column(DateTime(timezone=True), default=_now, nullable=False)
    procesado_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="Timestamp de cuando se aplicó o marcó en conflicto",
    )

    # ── Relaciones ─────────────────────────────────────────────────────────────
    diagrama = relationship("Diagrama", back_populates="cola_comandos")

    def __repr__(self) -> str:
        return (
            f"<ColaComandosMovil id={self.id!r} "
            f"estado={self.estado} diagrama={self.id_diagrama!r}>"
        )


# ─────────────────────────────────────────────────────────────────────────────

class ArchivoArtefacto(Base):
    """
    Referencia a un artefacto generado o importado asociado a un diagrama.

    Ejemplos de artefactos:
      - ZIP con proyecto Spring Boot generado.
      - Archivo XMI exportado para Enterprise Architect.
      - Archivo XMI importado que originó el diagrama.

    La URL de almacenamiento apunta a Supabase Storage.
    """
    __tablename__ = "archivo_artefacto"

    id = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    id_diagrama = Column(
        UUID(as_uuid=False),
        ForeignKey("diagrama.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tipo_archivo = Column(
        _make_enum(TipoArchivo, "tipo_archivo_enum"),
        nullable=False,
    )
    url_almacenamiento = Column(
        Text,
        nullable=False,
        comment="URL pública o firmada de Supabase Storage donde reside el archivo",
    )
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)

    # ── Relaciones ─────────────────────────────────────────────────────────────
    diagrama = relationship("Diagrama", back_populates="artefactos")

    def __repr__(self) -> str:
        return (
            f"<ArchivoArtefacto id={self.id!r} "
            f"tipo={self.tipo_archivo} diagrama={self.id_diagrama!r}>"
        )
