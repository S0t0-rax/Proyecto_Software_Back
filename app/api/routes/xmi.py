"""
app/api/routes/xmi.py
──────────────────────
Endpoints REST para Import/Export XMI compatible con Enterprise Architect.

  POST /diagramas/{id}/export-xmi
       → Descarga el diagrama como archivo .xmi

  POST /diagramas/{id}/import-xmi
       → Sube un archivo .xmi y lo importa al diagrama
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import ArchivoArtefacto, TipoArchivo
from app.services.xmi_service import (
    ImportResult,
    export_diagram_to_xmi,
    import_xmi_to_diagram,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/diagramas", tags=["XMI Import/Export"])


# ── Schemas de respuesta ──────────────────────────────────────────────────────

class ImportXMIResponse(BaseModel):
    id_diagrama:        str
    entidades_creadas:  int
    atributos_creados:  int
    relaciones_creadas: int
    advertencias:       list[str]
    mensaje:            str


# ══════════════════════════════════════════════════════════════════════════════
# EXPORT
# ══════════════════════════════════════════════════════════════════════════════

@router.post(
    "/{id_diagrama}/export-xmi",
    summary="Exportar diagrama a XMI 2.1 (Enterprise Architect)",
    description=(
        "Genera un documento XMI 2.1 con todas las entidades, atributos y "
        "relaciones del diagrama. El archivo es compatible con Enterprise "
        "Architect (File → Import/Export → Import XMI)."
    ),
    responses={
        200: {
            "content": {"application/xml": {}},
            "description": "Archivo XMI descargado",
        },
        404: {"description": "Diagrama no encontrado"},
    },
)
async def export_xmi(
    id_diagrama: str,
    db: Session = Depends(get_db),
):
    """
    Exporta el diagrama completo a XMI 2.1.

    El archivo descargado puede abrirse en Enterprise Architect mediante:
    **File → Import/Export → Import XMI → seleccionar el .xmi**
    """
    try:
        xmi_content = export_diagram_to_xmi(db, id_diagrama)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.exception("Error exportando XMI: %s", e)
        raise HTTPException(status_code=500, detail=f"Error exportando: {e}")

    # Registrar artefacto
    try:
        artefacto = ArchivoArtefacto(
            id_diagrama=id_diagrama,
            tipo_archivo=TipoArchivo.XMI_EXPORT,
            url_almacenamiento=f"local://xmi/export-{id_diagrama[:8]}.xmi",
        )
        db.add(artefacto)
        db.commit()
    except Exception as e:
        logger.warning("No se pudo registrar artefacto XMI export: %s", e)

    filename = f"diagram-{id_diagrama[:8]}.xmi"
    return Response(
        content=xmi_content,
        media_type="application/xml",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type": "application/xml; charset=utf-8",
        },
    )


# ══════════════════════════════════════════════════════════════════════════════
# IMPORT
# ══════════════════════════════════════════════════════════════════════════════

@router.post(
    "/{id_diagrama}/import-xmi",
    response_model=ImportXMIResponse,
    summary="Importar XMI 2.1 desde Enterprise Architect",
    description=(
        "Sube un archivo XMI 2.1 exportado desde Enterprise Architect y lo "
        "importa al diagrama indicado. Crea las entidades, atributos y "
        "relaciones encontradas en el XMI. Las posiciones del canvas se "
        "recuperan desde los metadatos EA si están disponibles."
    ),
    responses={
        200: {"description": "XMI importado exitosamente"},
        404: {"description": "Diagrama no encontrado"},
        422: {"description": "XMI inválido o mal formado"},
    },
)
async def import_xmi(
    id_diagrama: str,
    file: UploadFile = File(..., description="Archivo .xmi exportado desde Enterprise Architect"),
    db: Session = Depends(get_db),
):
    """
    Importa un XMI al diagrama.

    **Pasos en EA para exportar:**
    1. Abrir el paquete en EA.
    2. File → Import/Export → Export Package to XMI File.
    3. Seleccionar formato XMI 2.1 / UML 2.x.
    4. Subir el archivo generado aquí.

    > ⚠️ El import **agrega** entidades al diagrama sin eliminar las existentes.
    """
    if file.content_type not in (
        "application/xml", "text/xml", "application/octet-stream"
    ) and not (file.filename or "").endswith((".xmi", ".xml")):
        raise HTTPException(
            status_code=415,
            detail="El archivo debe ser un .xmi o .xml válido",
        )

    try:
        content_bytes = await file.read()
        xmi_content   = content_bytes.decode("utf-8", errors="replace")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error leyendo archivo: {e}")

    try:
        result: ImportResult = import_xmi_to_diagram(
            db=db,
            xmi_content=xmi_content,
            id_diagrama=id_diagrama,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        logger.exception("Error importando XMI: %s", e)
        raise HTTPException(status_code=500, detail=f"Error importando: {e}")

    # Registrar artefacto
    try:
        artefacto = ArchivoArtefacto(
            id_diagrama=id_diagrama,
            tipo_archivo=TipoArchivo.XMI_IMPORT,
            url_almacenamiento=f"local://xmi/import-{id_diagrama[:8]}-{file.filename}",
        )
        db.add(artefacto)
        db.commit()
    except Exception as e:
        logger.warning("No se pudo registrar artefacto XMI import: %s", e)

    return ImportXMIResponse(
        id_diagrama=id_diagrama,
        entidades_creadas=result.entidades_creadas,
        atributos_creados=result.atributos_creados,
        relaciones_creadas=result.relaciones_creadas,
        advertencias=result.advertencias,
        mensaje=(
            f"Import completado: {result.entidades_creadas} entidades, "
            f"{result.atributos_creados} atributos, "
            f"{result.relaciones_creadas} relaciones creadas."
        ),
    )
