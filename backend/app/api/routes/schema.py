from fastapi import APIRouter, Depends, Request

from app.connectors.base import SchemaContext
from app.dependencies import (
    _resolve_connection_id,
    get_connector,
    get_grounding,
    get_schema_context,
    get_semantic_registry,
)
from app.models import SchemaResponse
from app.security.auth import AuthUser, get_current_user
from app.semantic import SemanticRegistry
from app.semantic.suggestions import suggested_questions

router = APIRouter(tags=["schema"])


@router.get("/schema", response_model=SchemaResponse)
async def get_schema(
    request: Request,
    connector=Depends(get_connector),
    grounding=Depends(get_grounding),
    schema_ctx: SchemaContext = Depends(get_schema_context),
    registry: SemanticRegistry = Depends(get_semantic_registry),
    _user: AuthUser = Depends(get_current_user),
) -> SchemaResponse:
    return SchemaResponse(
        connector_type=connector.get_connector_type(),
        tables=schema_ctx.tables,
        row_counts=schema_ctx.row_counts,
        join_paths=[
            {
                "from_table": join.from_table,
                "to_table": join.to_table,
                "on": join.on,
                "type": join.join_type,
            }
            for join in registry.schema.joins
        ],
        metrics=[{"name": metric.name, "display_name": metric.display_name} for metric in registry.schema.metrics],
        dimensions=[
            {"name": dimension.name, "display_name": dimension.display_name}
            for dimension in registry.schema.dimensions
        ],
        time_dimensions=[
            {"name": time_dim.name, "display_name": time_dim.display_name}
            for time_dim in registry.schema.time_dimensions
        ],
        suggested_questions=suggested_questions(registry),
        grounding=grounding.status(_resolve_connection_id(request)) if grounding is not None else "off",
    )
