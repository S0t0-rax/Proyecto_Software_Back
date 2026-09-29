"""
app/services/xmi_service.py
────────────────────────────
Sistema de Import/Export XMI 2.1 compatible con Enterprise Architect.

El formato XMI (XML Metadata Interchange) es el estándar OMG para intercambiar
modelos UML entre herramientas. Enterprise Architect exporta diagramas de clases
como XMI 2.1 con el perfil UML 2.x.

Mapeo EA ↔ Nuestro modelo:
  EA uml:Class          ←→ EntidadConceptual
  EA uml:Property       ←→ Atributo
  EA uml:Association    ←→ Relacion
  EA xmi:id             ←→ id (UUID)
  EA name               ←→ nombre

Namespaces utilizados:
  xmi:  http://www.omg.org/spec/XMI/20110701
  uml:  http://www.omg.org/spec/UML/20110701
  thecustomprofile: (extensiones EA - ignoradas en import)
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from xml.etree import ElementTree as ET
from xml.dom import minidom

from sqlalchemy.orm import Session

from app.db.models import (
    Atributo,
    Diagrama,
    EntidadConceptual,
    Relacion,
    TipoCardinalidad,
)

logger = logging.getLogger(__name__)

# ── Namespaces XMI/UML ────────────────────────────────────────────────────────
NS_XMI = "http://www.omg.org/spec/XMI/20110701"
NS_UML = "http://www.omg.org/spec/UML/20110701"

NS_MAP = {
    "xmi": NS_XMI,
    "uml": NS_UML,
}

# Registrar para que ElementTree use prefijos legibles
for prefix, uri in NS_MAP.items():
    ET.register_namespace(prefix, uri)


def _xmi(tag: str) -> str:
    return f"{{{NS_XMI}}}{tag}"


def _uml(tag: str) -> str:
    return f"{{{NS_UML}}}{tag}"


def _new_id() -> str:
    return f"EAID_{str(uuid.uuid4()).upper().replace('-', '_')}"


# ── Mapeo de tipo_dato → UML PrimitiveType ────────────────────────────────────
_JAVA_TO_UML_TYPE = {
    "VARCHAR": "String",   "TEXT": "String",   "CHAR": "String",
    "INTEGER": "Integer",  "INT": "Integer",   "BIGINT": "Long",
    "FLOAT": "Double",     "DOUBLE": "Double", "NUMERIC": "Decimal",
    "BOOLEAN": "Boolean",  "BOOL": "Boolean",
    "TIMESTAMP": "Date",   "DATE": "Date",     "UUID": "String",
}

UML_PRIMITIVE_BASE = "http://www.omg.org/spec/UML/20110701/uml.xml"


def _sql_to_uml_primitive(tipo_dato: str) -> str:
    """Convierte tipo SQL al href de UML PrimitiveType usado por EA."""
    base = tipo_dato.upper().split("(")[0].strip()
    uml_type = _JAVA_TO_UML_TYPE.get(base, "String")
    return f"{UML_PRIMITIVE_BASE}#{uml_type}"


def _uml_primitive_to_sql(href: str) -> str:
    """Convierte href de UML PrimitiveType a tipo SQL aproximado."""
    uml_type = href.split("#")[-1] if "#" in href else href
    mapping = {
        "String": "VARCHAR(255)",
        "Integer": "INTEGER",
        "Long": "BIGINT",
        "Double": "DOUBLE",
        "Decimal": "NUMERIC(10,2)",
        "Boolean": "BOOLEAN",
        "Date": "TIMESTAMP",
        "Float": "FLOAT",
    }
    return mapping.get(uml_type, "VARCHAR(255)")


# ══════════════════════════════════════════════════════════════════════════════
# EXPORT — Modelo → XMI
# ══════════════════════════════════════════════════════════════════════════════

def export_diagram_to_xmi(db: Session, id_diagrama: str) -> str:
    """
    Exporta un Diagrama completo a formato XMI 2.1 compatible con EA.

    Returns:
        String XML del documento XMI, listo para guardar como .xmi/.xml.

    Raises:
        ValueError: Si el diagrama no existe.
    """
    diagrama = db.query(Diagrama).filter(Diagrama.id == id_diagrama).first()
    if diagrama is None:
        raise ValueError(f"Diagrama {id_diagrama!r} no encontrado")

    logger.info(
        "Exportando XMI | diagrama=%s | entidades=%d | relaciones=%d",
        diagrama.nombre, len(diagrama.entidades), len(diagrama.relaciones),
    )

    # ── Raíz XMI ─────────────────────────────────────────────────────────────
    root = ET.Element(_xmi("XMI"))
    root.set(_xmi("version"), "2.1")
    root.set("xmlns:xmi", NS_XMI)
    root.set("xmlns:uml", NS_UML)

    # ── Documentación ─────────────────────────────────────────────────────────
    doc = ET.SubElement(root, _xmi("Documentation"))
    doc.set("exporter", "ER Diagram Collaborative Tool")
    doc.set("exporterVersion", "1.0")
    doc.set("exporterID", "er-diagram-tool")

    # ── uml:Model ─────────────────────────────────────────────────────────────
    model = ET.SubElement(root, _uml("Model"))
    model.set(_xmi("type"), "uml:Model")
    model.set(_xmi("id"), f"MID_{id_diagrama.replace('-','_')}")
    model.set("name", "EA_Model")

    # ── Package del diagrama ──────────────────────────────────────────────────
    pkg = ET.SubElement(model, "packagedElement")
    pkg.set(_xmi("type"), "uml:Package")
    pkg.set(_xmi("id"), f"PKG_{id_diagrama.replace('-','_')}")
    pkg.set("name", diagrama.nombre)
    pkg.set("visibility", "public")

    # Mapeo id_entidad → xmi:id para construir asociaciones
    entity_xmi_ids: dict[str, str] = {}

    # ── Exportar entidades como uml:Class ─────────────────────────────────────
    for entidad in diagrama.entidades:
        xmi_id = f"CLS_{entidad.id.replace('-','_')}"
        entity_xmi_ids[entidad.id] = xmi_id

        cls_elem = ET.SubElement(pkg, "packagedElement")
        cls_elem.set(_xmi("type"), "uml:Class")
        cls_elem.set(_xmi("id"), xmi_id)
        cls_elem.set("name", entidad.nombre)
        cls_elem.set("visibility", "public")

        # Posición (extensión EA via xmi:Extension)
        ext = ET.SubElement(cls_elem, _xmi("Extension"))
        ext.set("extender", "Enterprise Architect")
        ext.set("extenderID", "6.5")
        props = ET.SubElement(ext, "element")
        props.set("name", entidad.nombre)
        props.set("scope", "public")
        # Coordenadas para EA (LTRB en puntos)
        coords = ET.SubElement(props, "extendedProperties")
        coords.set("tagged", "0")
        geom = ET.SubElement(props, "xrefs")
        geom.set("value", (
            f"$ea_ntype=0;$ea_nuid={entidad.id};"
            f"$pos_x={int(entidad.pos_x)};$pos_y={int(entidad.pos_y)};"
        ))

        # ── Atributos como uml:Property ───────────────────────────────────────
        for attr in entidad.atributos:
            attr_elem = ET.SubElement(cls_elem, "ownedAttribute")
            attr_elem.set(_xmi("type"), "uml:Property")
            attr_elem.set(_xmi("id"), f"ATTR_{attr.id.replace('-','_')}")
            attr_elem.set("name", attr.nombre)
            attr_elem.set("visibility", "public")

            if attr.es_pk:
                # Stereotipo PK
                stereo = ET.SubElement(attr_elem, _xmi("Extension"))
                stereo.set("extender", "Enterprise Architect")
                tag = ET.SubElement(stereo, "tags")
                ET.SubElement(tag, "tag").set("name", "primaryKey")
                ET.SubElement(tag, "tag").set("value", "true")

            if attr.es_fk:
                stereo = ET.SubElement(attr_elem, _xmi("Extension"))
                stereo.set("extender", "Enterprise Architect")
                tag = ET.SubElement(stereo, "tags")
                ET.SubElement(tag, "tag").set("name", "foreignKey")
                ET.SubElement(tag, "tag").set("value", "true")

            # Tipo como uml:PrimitiveType
            type_elem = ET.SubElement(attr_elem, "type")
            type_elem.set(_xmi("type"), "uml:PrimitiveType")
            type_elem.set("href", _sql_to_uml_primitive(attr.tipo_dato))

            # tipo_dato original como tagged value para round-trip fiel
            tagged = ET.SubElement(attr_elem, _xmi("Extension"))
            tagged.set("extender", "ER Diagram Tool")
            tv = ET.SubElement(tagged, "taggedValue")
            tv.set("tag", "sql_type")
            tv.set("value", attr.tipo_dato)

    # ── Exportar relaciones como uml:Association ──────────────────────────────
    for relacion in diagrama.relaciones:
        assoc_id = f"ASSOC_{relacion.id.replace('-','_')}"
        end1_id  = f"END1_{relacion.id.replace('-','_')}"
        end2_id  = f"END2_{relacion.id.replace('-','_')}"

        assoc_elem = ET.SubElement(pkg, "packagedElement")
        assoc_elem.set(_xmi("type"), "uml:Association")
        assoc_elem.set(_xmi("id"), assoc_id)
        assoc_elem.set("name", relacion.nombre or "")
        assoc_elem.set("visibility", "public")

        # Cardinalidad como tagged value
        ext = ET.SubElement(assoc_elem, _xmi("Extension"))
        ext.set("extender", "ER Diagram Tool")
        tv = ET.SubElement(ext, "taggedValue")
        tv.set("tag", "cardinalidad")
        tv.set("value", relacion.tipo_cardinalidad.value)

        # memberEnd
        me1 = ET.SubElement(assoc_elem, "memberEnd")
        me1.set(_xmi("idref"), end1_id)
        me2 = ET.SubElement(assoc_elem, "memberEnd")
        me2.set(_xmi("idref"), end2_id)

        # Extremo origen
        src_xmi_id = entity_xmi_ids.get(relacion.id_origen, relacion.id_origen)
        end1_elem = ET.SubElement(assoc_elem, "ownedEnd")
        end1_elem.set(_xmi("type"), "uml:Property")
        end1_elem.set(_xmi("id"), end1_id)
        end1_elem.set("visibility", "public")
        end1_elem.set("type", src_xmi_id)
        end1_elem.set("association", assoc_id)
        _set_multiplicity(end1_elem, relacion.tipo_cardinalidad, side="origen")

        # Extremo destino
        dst_xmi_id = entity_xmi_ids.get(relacion.id_destino, relacion.id_destino)
        end2_elem = ET.SubElement(assoc_elem, "ownedEnd")
        end2_elem.set(_xmi("type"), "uml:Property")
        end2_elem.set(_xmi("id"), end2_id)
        end2_elem.set("visibility", "public")
        end2_elem.set("type", dst_xmi_id)
        end2_elem.set("association", assoc_id)
        _set_multiplicity(end2_elem, relacion.tipo_cardinalidad, side="destino")

    # ── Serializar con pretty-print ───────────────────────────────────────────
    raw = ET.tostring(root, encoding="unicode", xml_declaration=False)
    pretty = minidom.parseString(f'<?xml version="1.0" encoding="UTF-8"?>{raw}')
    return pretty.toprettyxml(indent="  ", encoding=None)


def _set_multiplicity(
    end_elem: ET.Element,
    cardinalidad: TipoCardinalidad,
    side: str,
) -> None:
    """Añade lowerValue/upperValue al extremo de la asociación según cardinalidad."""
    # Mapa: (origen, destino) → (lower_origen, upper_origen, lower_destino, upper_destino)
    card_map = {
        TipoCardinalidad.UNO_A_UNO: ("1", "1", "1",  "1"),
        TipoCardinalidad.UNO_A_N:   ("1", "1", "0",  "*"),
        TipoCardinalidad.N_A_UNO:   ("0", "*", "1",  "1"),
        TipoCardinalidad.N_A_M:     ("0", "*", "0",  "*"),
    }
    lo, hi, ld, hd = card_map.get(cardinalidad, ("0", "*", "0", "*"))
    lower, upper = (lo, hi) if side == "origen" else (ld, hd)

    lv = ET.SubElement(end_elem, "lowerValue")
    lv.set(_xmi("type"), "uml:LiteralInteger")
    lv.set(_xmi("id"), f"LV_{uuid.uuid4().hex[:8]}")
    lv.set("value", lower)

    uv = ET.SubElement(end_elem, "upperValue")
    uv.set(_xmi("type"), "uml:LiteralUnlimitedNatural")
    uv.set(_xmi("id"), f"UV_{uuid.uuid4().hex[:8]}")
    uv.set("value", upper)


# ══════════════════════════════════════════════════════════════════════════════
# IMPORT — XMI → Modelo
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class ImportResult:
    """Resultado del proceso de importación XMI."""
    entidades_creadas: int = 0
    atributos_creados: int = 0
    relaciones_creadas: int = 0
    advertencias: list[str] = field(default_factory=list)
    id_diagrama: str = ""


def import_xmi_to_diagram(
    db: Session,
    xmi_content: str,
    id_diagrama: str,
) -> ImportResult:
    """
    Importa un documento XMI 2.1 (exportado desde EA) al diagrama indicado.

    Estrategia:
    - Parsea el XML buscando uml:Class → EntidadConceptual.
    - Parsea ownedAttribute → Atributo.
    - Parsea uml:Association → Relacion.
    - Recupera posiciones de los xrefs de EA si están disponibles.
    - Ignora silenciosamente extensiones y estereotipos desconocidos.

    Args:
        db:          Sesión SQLAlchemy.
        xmi_content: Contenido del archivo XMI como string.
        id_diagrama: UUID del diagrama donde importar.

    Returns:
        ImportResult con conteos y advertencias.
    """
    result = ImportResult(id_diagrama=id_diagrama)

    # Verificar que el diagrama existe
    diagrama = db.query(Diagrama).filter(Diagrama.id == id_diagrama).first()
    if diagrama is None:
        raise ValueError(f"Diagrama {id_diagrama!r} no encontrado")

    try:
        root = ET.fromstring(xmi_content)
    except ET.ParseError as e:
        raise ValueError(f"XMI inválido: {e}")

    # ── Encontrar el Package principal ───────────────────────────────────────
    pkg = _find_diagram_package(root)
    if pkg is None:
        result.advertencias.append("No se encontró un packagedElement de tipo uml:Package en el XMI.")
        return result

    # ── Mapeo xmi:id → entidad DB (para resolver asociaciones) ───────────────
    xmi_id_to_db_id: dict[str, str] = {}

    # ── Importar clases → EntidadConceptual ──────────────────────────────────
    for cls_elem in pkg.findall("packagedElement"):
        xmi_type = cls_elem.get(_xmi("type"), "")
        if xmi_type != "uml:Class":
            continue

        nombre   = cls_elem.get("name", "SinNombre")
        xmi_id   = cls_elem.get(_xmi("id"), _new_id())
        pos_x, pos_y = _extract_position(cls_elem)

        entidad = EntidadConceptual(
            id=str(uuid.uuid4()),
            id_diagrama=id_diagrama,
            nombre=nombre,
            pos_x=pos_x,
            pos_y=pos_y,
        )
        db.add(entidad)
        db.flush()  # Para obtener entidad.id

        xmi_id_to_db_id[xmi_id] = entidad.id
        result.entidades_creadas += 1

        logger.debug("  Importando clase: %s (xmi:id=%s)", nombre, xmi_id)

        # ── Importar atributos → Atributo ─────────────────────────────────────
        for attr_elem in cls_elem.findall("ownedAttribute"):
            attr_xmi_type = attr_elem.get(_xmi("type"), "")
            if "Property" not in attr_xmi_type:
                continue

            attr_nombre = attr_elem.get("name", "campo")
            es_pk       = _has_tag(attr_elem, "primaryKey", "true")
            es_fk       = _has_tag(attr_elem, "foreignKey", "true")
            sql_type    = _get_tagged_value(attr_elem, "sql_type")

            if not sql_type:
                # Inferir desde uml:PrimitiveType href
                type_elem = attr_elem.find("type")
                if type_elem is not None:
                    href = type_elem.get("href", "")
                    sql_type = _uml_primitive_to_sql(href)
                else:
                    sql_type = "VARCHAR(255)"
                    result.advertencias.append(
                        f"Atributo {attr_nombre!r} sin tipo definido → VARCHAR(255)"
                    )

            atributo = Atributo(
                id=str(uuid.uuid4()),
                id_entidad=entidad.id,
                nombre=attr_nombre,
                tipo_dato=sql_type,
                es_pk=es_pk,
                es_fk=es_fk,
            )
            db.add(atributo)
            result.atributos_creados += 1

    db.flush()

    # ── Importar asociaciones → Relacion ──────────────────────────────────────
    for assoc_elem in pkg.findall("packagedElement"):
        xmi_type = assoc_elem.get(_xmi("type"), "")
        if xmi_type != "uml:Association":
            continue

        owned_ends = assoc_elem.findall("ownedEnd")
        if len(owned_ends) < 2:
            result.advertencias.append(
                f"Asociación {assoc_elem.get(_xmi('id'),'?')} ignorada: "
                "necesita exactamente 2 extremos."
            )
            continue

        end1_type = owned_ends[0].get("type", "")
        end2_type = owned_ends[1].get("type", "")

        id_origen  = xmi_id_to_db_id.get(end1_type)
        id_destino = xmi_id_to_db_id.get(end2_type)

        if not id_origen or not id_destino:
            result.advertencias.append(
                f"Asociación ignorada: no se pudo resolver origen={end1_type!r} "
                f"o destino={end2_type!r}"
            )
            continue

        # Cardinalidad desde tagged value o inferida de multiplicidades
        card_str = _get_tagged_value(assoc_elem, "cardinalidad")
        card     = _parse_cardinalidad(card_str, owned_ends)

        relacion = Relacion(
            id=str(uuid.uuid4()),
            id_diagrama=id_diagrama,
            id_origen=id_origen,
            id_destino=id_destino,
            tipo_cardinalidad=card,
            nombre=assoc_elem.get("name") or None,
        )
        db.add(relacion)
        result.relaciones_creadas += 1

    db.commit()

    logger.info(
        "XMI importado | diagrama=%s | entidades=%d | atributos=%d | relaciones=%d",
        id_diagrama,
        result.entidades_creadas,
        result.atributos_creados,
        result.relaciones_creadas,
    )
    return result


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS DE PARSING
# ══════════════════════════════════════════════════════════════════════════════

def _find_diagram_package(root: ET.Element) -> Optional[ET.Element]:
    """Busca el primer packagedElement de tipo uml:Package en el árbol."""
    # Buscar en uml:Model primero
    for model in root.iter(_uml("Model")):
        for child in model:
            if child.get(_xmi("type")) == "uml:Package":
                return child
    # Fallback: buscar directamente
    for elem in root.iter("packagedElement"):
        if elem.get(_xmi("type")) == "uml:Package":
            return elem
    return None


def _extract_position(cls_elem: ET.Element) -> tuple[float, float]:
    """Extrae pos_x, pos_y desde los xrefs de EA si están disponibles."""
    for ext in cls_elem.findall(_xmi("Extension")):
        for child in ext.iter("xrefs"):
            val = child.get("value", "")
            pos_x = _extract_xref_value(val, "pos_x")
            pos_y = _extract_xref_value(val, "pos_y")
            if pos_x is not None:
                return float(pos_x), float(pos_y or 0)
    return 0.0, 0.0


def _extract_xref_value(xrefs_str: str, key: str) -> Optional[str]:
    """Extrae un valor de la cadena $key=value; del formato EA xrefs."""
    import re
    m = re.search(rf"\${key}=([^;]+);", xrefs_str)
    return m.group(1) if m else None


def _has_tag(elem: ET.Element, tag_name: str, expected_value: str) -> bool:
    """Verifica si existe un tagged value con el valor esperado."""
    for ext in elem.findall(_xmi("Extension")):
        for tag in ext.iter("tag"):
            if tag.get("name") == tag_name and tag.get("value") == expected_value:
                return True
    return False


def _get_tagged_value(elem: ET.Element, tag_name: str) -> Optional[str]:
    """Obtiene el valor de un taggedValue por nombre."""
    for ext in elem.findall(_xmi("Extension")):
        for tv in ext.iter("taggedValue"):
            if tv.get("tag") == tag_name:
                return tv.get("value")
    return None


def _parse_cardinalidad(
    card_str: Optional[str],
    owned_ends: list[ET.Element],
) -> TipoCardinalidad:
    """
    Determina la cardinalidad desde el tagged value o inferida de multiplicidades.
    """
    if card_str:
        for c in TipoCardinalidad:
            if c.value == card_str:
                return c

    # Inferir desde upper values de los extremos
    def get_upper(end: ET.Element) -> str:
        uv = end.find("upperValue")
        return uv.get("value", "1") if uv is not None else "1"

    u1 = get_upper(owned_ends[0])
    u2 = get_upper(owned_ends[1])

    if u1 == "1" and u2 == "1":
        return TipoCardinalidad.UNO_A_UNO
    if u1 == "1" and u2 == "*":
        return TipoCardinalidad.UNO_A_N
    if u1 == "*" and u2 == "1":
        return TipoCardinalidad.N_A_UNO
    return TipoCardinalidad.N_A_M
