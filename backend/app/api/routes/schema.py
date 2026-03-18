from fastapi import APIRouter, Depends

from app.connectors.base import SchemaContext
from app.dependencies import get_connector, get_schema_context, get_semantic_registry
from app.models import SchemaResponse
from app.security.auth import AuthUser, get_current_user
from app.semantic import SemanticRegistry

router = APIRouter(tags=["schema"])


@router.get("/schema", response_model=SchemaResponse)
async def get_schema(
    connector=Depends(get_connector),
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
    )
