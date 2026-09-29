"""
app/services/springboot_generator.py
──────────────────────────────────────
Servicio de generación de proyectos Spring Boot desde un diagrama ER.

Pipeline:
  1. Consultar BD → obtener EntidadConceptual + Atributos del diagrama.
  2. Transformar → mapear tipos SQL a Java, calcular nombres de clase/campo.
  3. Renderizar → Jinja2 rellena los 6 templates por entidad + archivos globales.
  4. Empaquetar → construir estructura Maven en memoria y comprimir en ZIP.
  5. Retornar → bytes del ZIP listos para StreamingResponse.

Mapeo de tipos SQL → Java (extensible en TYPE_MAP):
  VARCHAR, TEXT           → String
  INTEGER, BIGINT, INT    → Long / Integer
  BOOLEAN                 → Boolean
  FLOAT, DOUBLE, NUMERIC  → Double / BigDecimal
  TIMESTAMP, DATE         → LocalDateTime / LocalDate
  UUID                    → UUID
"""
from __future__ import annotations

import io
import logging
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from sqlalchemy.orm import Session

from app.db.models import Atributo, Diagrama, EntidadConceptual

logger = logging.getLogger(__name__)

# ── Directorio de templates ────────────────────────────────────────────────────
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates" / "springboot"


# ══════════════════════════════════════════════════════════════════════════════
# MAPEO DE TIPOS SQL → JAVA
# ══════════════════════════════════════════════════════════════════════════════

# Cada entrada: patrón regex (sobre tipo_dato en MAYÚSCULAS) → java_type, import opcional
_TYPE_RULES: list[tuple[str, str, Optional[str]]] = [
    # Strings
    (r"^VARCHAR",        "String",         None),
    (r"^CHAR",           "String",         None),
    (r"^TEXT",           "String",         None),
    (r"^CLOB",           "String",         None),
    # Enteros
    (r"^BIGINT",         "Long",           None),
    (r"^BIGSERIAL",      "Long",           None),
    (r"^INTEGER",        "Integer",        None),
    (r"^INT",            "Integer",        None),
    (r"^SMALLINT",       "Short",          None),
    (r"^SERIAL",         "Integer",        None),
    # Decimales
    (r"^NUMERIC",        "BigDecimal",     "java.math.BigDecimal"),
    (r"^DECIMAL",        "BigDecimal",     "java.math.BigDecimal"),
    (r"^FLOAT",          "Double",         None),
    (r"^DOUBLE",         "Double",         None),
    (r"^REAL",           "Float",          None),
    # Booleano
    (r"^BOOLEAN",        "Boolean",        None),
    (r"^BOOL",           "Boolean",        None),
    # Temporales
    (r"^TIMESTAMP",      "LocalDateTime",  "java.time.LocalDateTime"),
    (r"^DATETIME",       "LocalDateTime",  "java.time.LocalDateTime"),
    (r"^DATE",           "LocalDate",      "java.time.LocalDate"),
    (r"^TIME",           "LocalTime",      "java.time.LocalTime"),
    # UUID
    (r"^UUID",           "UUID",           "java.util.UUID"),
    # Binarios
    (r"^BYTEA",          "byte[]",         None),
    (r"^BLOB",           "byte[]",         None),
    # JSON
    (r"^JSON",           "String",         None),
]

# Extrae la longitud de VARCHAR(n) → n
_LENGTH_RE = re.compile(r"\((\d+)\)")


def _map_sql_to_java(tipo_dato: str) -> tuple[str, Optional[str], Optional[int]]:
    """
    Convierte un tipo SQL a (java_type, import_statement_or_None, max_length_or_None).

    Ejemplos:
        "VARCHAR(255)"  → ("String", None, 255)
        "INTEGER"       → ("Integer", None, None)
        "UUID"          → ("UUID", "java.util.UUID", None)
        "NUMERIC(10,2)" → ("BigDecimal", "java.math.BigDecimal", None)
        "DESCONOCIDO"   → ("String", None, None)   ← fallback seguro
    """
    raw = tipo_dato.strip().upper()
    max_len: Optional[int] = None

    m = _LENGTH_RE.search(raw)
    if m:
        try:
            max_len = int(m.group(1))
        except ValueError:
            pass

    for pattern, java_type, java_import in _TYPE_RULES:
        if re.match(pattern, raw):
            return java_type, java_import, max_len

    logger.warning("Tipo SQL desconocido %r → fallback a String", tipo_dato)
    return "String", None, max_len


# ══════════════════════════════════════════════════════════════════════════════
# DATACLASSES DE CONTEXTO PARA JINJA2
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class AtributoCtx:
    """Contexto de un atributo para los templates Jinja2."""
    nombre: str
    tipo_dato: str          # tipo original del diagrama
    es_pk: bool
    es_fk: bool
    java_type: str
    java_import: Optional[str]
    max_length: Optional[int]
    nullable: bool          # True si NO es PK
    field_name: str         # camelCase del nombre
    generation_strategy: str = "IDENTITY"  # para @GeneratedValue

    @property
    def has_import(self) -> bool:
        return self.java_import is not None


@dataclass
class EntidadCtx:
    """Contexto completo de una entidad para los templates Jinja2."""
    nombre: str
    class_name: str         # PascalCase
    field_name: str         # camelCase (para variables)
    atributos: list[AtributoCtx] = field(default_factory=list)

    # PK info
    pk_java_type: str = "Long"
    pk_import: Optional[str] = None

    @property
    def has_temporal_fields(self) -> bool:
        temporals = {"LocalDateTime", "LocalDate", "LocalTime"}
        return any(a.java_type in temporals for a in self.atributos)

    @property
    def has_uuid_fields(self) -> bool:
        return any(a.java_type == "UUID" for a in self.atributos)

    @property
    def has_big_decimal(self) -> bool:
        return any(a.java_type == "BigDecimal" for a in self.atributos)


# ══════════════════════════════════════════════════════════════════════════════
# TRANSFORMADORES DE NOMBRES
# ══════════════════════════════════════════════════════════════════════════════

def _to_pascal_case(name: str) -> str:
    """
    Convierte un nombre a PascalCase.
    'orden_detalle' → 'OrdenDetalle',  'usuario' → 'Usuario'
    """
    return "".join(word.capitalize() for word in re.split(r"[_\s\-]+", name) if word)


def _to_camel_case(name: str) -> str:
    """
    Convierte un nombre a camelCase.
    'OrdenDetalle' → 'ordenDetalle',  'id_usuario' → 'idUsuario'
    """
    pascal = _to_pascal_case(name)
    return pascal[0].lower() + pascal[1:] if pascal else name


def _generation_strategy(java_type: str) -> str:
    """Elige la estrategia de generación de ID según el tipo Java."""
    if java_type == "UUID":
        return "UUID"
    if java_type in ("Long", "Integer"):
        return "IDENTITY"
    return "IDENTITY"


# ══════════════════════════════════════════════════════════════════════════════
# CARGA DE DATOS DESDE BD
# ══════════════════════════════════════════════════════════════════════════════

def _cargar_diagrama(db: Session, id_diagrama: str) -> Diagrama:
    """Carga el Diagrama con sus entidades y atributos (eager join)."""
    diagrama = (
        db.query(Diagrama)
        .filter(Diagrama.id == id_diagrama)
        .first()
    )
    if diagrama is None:
        raise ValueError(f"Diagrama con id={id_diagrama!r} no encontrado")
    return diagrama


def _build_entidad_ctx(entidad: EntidadConceptual) -> EntidadCtx:
    """Construye el contexto Jinja2 de una entidad a partir del modelo ORM."""
    atributos_ctx: list[AtributoCtx] = []
    pk_java_type = "Long"
    pk_import: Optional[str] = None

    for attr in entidad.atributos:
        java_type, java_import, max_len = _map_sql_to_java(attr.tipo_dato)

        if attr.es_pk:
            pk_java_type = java_type
            pk_import = java_import
            strategy = _generation_strategy(java_type)
        else:
            strategy = "IDENTITY"

        atributos_ctx.append(
            AtributoCtx(
                nombre=attr.nombre,
                tipo_dato=attr.tipo_dato,
                es_pk=attr.es_pk,
                es_fk=attr.es_fk,
                java_type=java_type,
                java_import=java_import,
                max_length=max_len,
                nullable=not attr.es_pk,
                field_name=_to_camel_case(attr.nombre),
                generation_strategy=strategy,
            )
        )

    return EntidadCtx(
        nombre=entidad.nombre,
        class_name=_to_pascal_case(entidad.nombre),
        field_name=_to_camel_case(entidad.nombre),
        atributos=atributos_ctx,
        pk_java_type=pk_java_type,
        pk_import=pk_import,
    )


# ══════════════════════════════════════════════════════════════════════════════
# MOTOR JINJA2
# ══════════════════════════════════════════════════════════════════════════════

def _build_jinja_env() -> Environment:
    """Crea el entorno Jinja2 apuntando al directorio de templates."""
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        undefined=StrictUndefined,   # Falla explícitamente si falta una variable
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    # Filtros personalizados útiles en templates
    env.filters["lower"] = str.lower
    env.filters["upper"] = str.upper
    env.filters["capitalize"] = str.capitalize
    env.filters["pascal"] = _to_pascal_case
    env.filters["camel"] = _to_camel_case
    return env


# ══════════════════════════════════════════════════════════════════════════════
# EMPAQUETADOR ZIP (en memoria)
# ══════════════════════════════════════════════════════════════════════════════

def _render(env: Environment, template_name: str, ctx: dict) -> str:
    """Renderiza un template Jinja2 con el contexto dado."""
    return env.get_template(template_name).render(**ctx)


def generate_springboot_zip(
    db: Session,
    id_diagrama: str,
    group_id: str = "com.erdiagram",
    artifact_id: str = "generated-project",
    project_name: str = "Generated Project",
) -> bytes:
    """
    Genera un ZIP con la estructura Maven completa de un proyecto Spring Boot
    a partir del diagrama con `id_diagrama`.

    Args:
        db:           Sesión SQLAlchemy activa.
        id_diagrama:  UUID del diagrama a generar.
        group_id:     Group ID Maven (ej: "com.miempresa").
        artifact_id:  Artifact ID Maven (ej: "mi-proyecto").
        project_name: Nombre legible del proyecto.

    Returns:
        bytes del archivo ZIP listo para descargar.

    Raises:
        ValueError: Si el diagrama no existe o no tiene entidades.
    """
    # ── 1. Cargar datos ───────────────────────────────────────────────────────
    diagrama = _cargar_diagrama(db, id_diagrama)
    entidades = diagrama.entidades

    if not entidades:
        raise ValueError(
            f"El diagrama {diagrama.nombre!r} no tiene entidades. "
            "Agrega al menos una entidad antes de generar."
        )

    logger.info(
        "Generando Spring Boot ZIP | diagrama=%s | entidades=%d",
        diagrama.nombre, len(entidades),
    )

    # ── 2. Construir contextos Jinja2 ─────────────────────────────────────────
    entidades_ctx = [_build_entidad_ctx(e) for e in entidades]

    # ── 3. Preparar Jinja2 ────────────────────────────────────────────────────
    env = _build_jinja_env()

    # Paquete base: com.erdiagram.miproyecto
    safe_artifact = re.sub(r"[^a-zA-Z0-9]", "", artifact_id).lower()
    base_package  = f"{group_id}.{safe_artifact}"
    pkg_path      = base_package.replace(".", "/")   # para la ruta de archivos
    main_class    = _to_pascal_case(artifact_id)

    global_ctx = {
        "group_id":       group_id,
        "artifact_id":    artifact_id,
        "project_name":   project_name,
        "base_package":   base_package,
        "main_class_name": main_class,
    }

    # ── 4. Construir ZIP en memoria ───────────────────────────────────────────
    buffer = io.BytesIO()

    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        root = artifact_id  # carpeta raíz dentro del ZIP

        # ── pom.xml ───────────────────────────────────────────────────────────
        zf.writestr(
            f"{root}/pom.xml",
            _render(env, "pom.xml.j2", global_ctx),
        )

        # ── application.properties ────────────────────────────────────────────
        zf.writestr(
            f"{root}/src/main/resources/application.properties",
            _render(env, "application.properties.j2", global_ctx),
        )

        # ── Application.java (main) ───────────────────────────────────────────
        zf.writestr(
            f"{root}/src/main/java/{pkg_path}/{main_class}Application.java",
            _render(env, "Application.java.j2", global_ctx),
        )

        # ── GlobalExceptionHandler.java ───────────────────────────────────────
        zf.writestr(
            f"{root}/src/main/java/{pkg_path}/exception/GlobalExceptionHandler.java",
            _render(env, "GlobalExceptionHandler.java.j2", global_ctx),
        )

        # ── Archivos por entidad ──────────────────────────────────────────────
        for entity_ctx in entidades_ctx:
            entity_render_ctx = {**global_ctx, "entity": entity_ctx}

            class_name = entity_ctx.class_name

            # Entity.java
            zf.writestr(
                f"{root}/src/main/java/{pkg_path}/entity/{class_name}.java",
                _render(env, "Entity.java.j2", entity_render_ctx),
            )

            # Repository.java
            zf.writestr(
                f"{root}/src/main/java/{pkg_path}/repository/{class_name}Repository.java",
                _render(env, "Repository.java.j2", entity_render_ctx),
            )

            # IService.java (interfaz)
            zf.writestr(
                f"{root}/src/main/java/{pkg_path}/service/I{class_name}Service.java",
                _render(env, "IService.java.j2", entity_render_ctx),
            )

            # ServiceImpl.java
            zf.writestr(
                f"{root}/src/main/java/{pkg_path}/service/impl/{class_name}ServiceImpl.java",
                _render(env, "ServiceImpl.java.j2", entity_render_ctx),
            )

            # Controller.java
            zf.writestr(
                f"{root}/src/main/java/{pkg_path}/controller/{class_name}Controller.java",
                _render(env, "Controller.java.j2", entity_render_ctx),
            )

            logger.debug("  ✓ Generada entidad: %s", class_name)

        # ── .gitignore (Maven estándar) ───────────────────────────────────────
        zf.writestr(
            f"{root}/.gitignore",
            "target/\n*.class\n*.jar\n.idea/\n*.iml\n.mvn/\n",
        )

        # ── README.md ─────────────────────────────────────────────────────────
        readme = _build_readme(project_name, artifact_id, base_package, entidades_ctx)
        zf.writestr(f"{root}/README.md", readme)

    buffer.seek(0)
    zip_bytes = buffer.read()

    logger.info(
        "ZIP generado exitosamente | tamaño=%d bytes | archivos=%d",
        len(zip_bytes),
        len(entidades_ctx) * 5 + 5,   # 5 archivos/entidad + 5 globales
    )
    return zip_bytes


# ══════════════════════════════════════════════════════════════════════════════
# README GENERADO
# ══════════════════════════════════════════════════════════════════════════════

def _build_readme(
    project_name: str,
    artifact_id: str,
    base_package: str,
    entidades: list[EntidadCtx],
) -> str:
    lines = [
        f"# {project_name}",
        "",
        "> Proyecto generado automáticamente por **ER Diagram Collaborative Tool**.",
        "",
        "## Requisitos",
        "- Java 17+",
        "- Maven 3.8+",
        "- PostgreSQL 14+",
        "",
        "## Configuración",
        "1. Edita `src/main/resources/application.properties` con tus credenciales de BD.",
        "2. Crea la base de datos: `createdb " + artifact_id.replace("-", "_") + "`",
        "",
        "## Ejecutar",
        "```bash",
        "mvn spring-boot:run",
        "```",
        "",
        "## Estructura del proyecto",
        f"Paquete base: `{base_package}`",
        "",
        "### Entidades generadas",
        "",
        "| Entidad | Endpoint REST |",
        "|---------|--------------|",
    ]
    for e in entidades:
        lines.append(f"| `{e.class_name}` | `/api/{e.nombre.lower()}s` |")

    lines += [
        "",
        "## Arquitectura (4 capas MVC)",
        "```",
        "Controller  →  REST API (HTTP)",
        "    ↓",
        "IService    →  Contrato de negocio",
        "    ↓",
        "ServiceImpl →  Lógica de negocio + @Transactional",
        "    ↓",
        "Repository  →  Spring Data JPA (PostgreSQL)",
        "```",
    ]
    return "\n".join(lines)
