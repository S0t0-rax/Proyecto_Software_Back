"""
app/api/routes/generator.py
────────────────────────────
Endpoints REST para la generación de artefactos desde un diagrama ER:
  POST /diagramas/{id_diagrama}/generate-spring-boot
       → Genera y descarga un ZIP con el proyecto Spring Boot completo.

También registra el artefacto generado en la tabla ArchivoArtefacto
para tener trazabilidad histórica (útil para la Fase de XMI también).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import ArchivoArtefacto, TipoArchivo
from app.services.springboot_generator import generate_springboot_zip

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/diagramas", tags=["Generador Spring Boot"])


@router.post(
    "/{id_diagrama}/generate-spring-boot",
    summary="Genera proyecto Spring Boot desde el diagrama",
    description=(
        "Lee todas las entidades y atributos del diagrama, renderiza los templates "
        "Jinja2 (Entity, Repository, IService, ServiceImpl, Controller) y retorna "
        "un archivo ZIP con la estructura Maven completa lista para compilar con "
        "`mvn spring-boot:run`."
    ),
    response_class=StreamingResponse,
    responses={
        200: {"description": "ZIP descargado exitosamente"},
        404: {"description": "Diagrama no encontrado"},
        422: {"description": "El diagrama no tiene entidades"},
    },
)
async def generate_spring_boot(
    id_diagrama: str,
    group_id: str    = Query(default="com.erdiagram",       description="Maven Group ID"),
    artifact_id: str = Query(default="generated-project",   description="Maven Artifact ID"),
    project_name: str = Query(default="Generated Project",  description="Nombre del proyecto"),
    db: Session      = Depends(get_db),
):
    """
    Endpoint de generación de Spring Boot.

    Query params opcionales:
    - **group_id**: ej. `com.miempresa`
    - **artifact_id**: ej. `mi-sistema`
    - **project_name**: ej. `Mi Sistema de Ventas`

    Retorna un archivo `.zip` para descargar directamente desde el navegador.
    """
    logger.info(
        "Solicitud de generación | diagrama=%s | artifact=%s",
        id_diagrama, artifact_id,
    )

    try:
        zip_bytes = generate_springboot_zip(
            db=db,
            id_diagrama=id_diagrama,
            group_id=group_id,
            artifact_id=artifact_id,
            project_name=project_name,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        logger.exception("Error generando Spring Boot ZIP: %s", e)
        raise HTTPException(status_code=500, detail=f"Error interno al generar: {e}")

    # ── Registrar artefacto generado en BD (trazabilidad) ─────────────────────
    try:
        artefacto = ArchivoArtefacto(
            id_diagrama=id_diagrama,
            tipo_archivo=TipoArchivo.ZIP_SPRING_BOOT,
            # En producción, esto sería la URL de Supabase Storage tras subir el ZIP.
            # Por ahora se registra como generado localmente.
            url_almacenamiento=f"local://generated/{artifact_id}-{id_diagrama[:8]}.zip",
        )
        db.add(artefacto)
        db.commit()
        logger.info("Artefacto registrado en BD | id=%s", artefacto.id)
    except Exception as e:
        logger.warning("No se pudo registrar artefacto en BD: %s", e)
        # No interrumpir la descarga por un error de registro

    # ── Preparar nombre de archivo para la respuesta ──────────────────────────
    filename = f"{artifact_id}.zip"

    import io
    return StreamingResponse(
        content=io.BytesIO(zip_bytes),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(zip_bytes)),
            "X-Diagram-Id": id_diagrama,
            "X-Artifact-Id": artifact_id,
        },
    )
