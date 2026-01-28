from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime


class TableDescription(BaseModel):
    description: str = Field(..., min_length=0, max_length=1000)
    accuracy: float = Field(..., ge=0.0, le=1.0)
    glossary_terms: List[str] = Field(..., min_length=0, max_length=1000)


class SensitivityInfo(BaseModel):
    is_sensitive: bool
    classification: str
    rationale: Optional[str]


class ColumnMetadata(BaseModel):
    name: str
    description: str = Field(..., min_length=0, max_length=1000)
    accuracy: float = Field(..., ge=0.0, le=1.0)
    is_computed: bool
    sensitivity: SensitivityInfo
    glossary_terms: List[str] = Field(..., min_length=0, max_length=1000)


class ModelInfo(BaseModel):
    name: str
    version: str


class TableMetadata(BaseModel):
    table_fqn: str
    table_description: TableDescription
    columns: List[ColumnMetadata]
    model: ModelInfo
    generated_at: datetime


class ColumnStatus(BaseModel):
    name: str
    type: str
    mode: str
    description: Optional[str]
    is_partitioning_column: bool


class TableStatus(BaseModel):
    table_fqn: str
    exists: bool
    is_partitioned: bool
    partition_field: Optional[str]
    row_count: int
    size_mb: float
    description: Optional[str]
    columns: List[ColumnStatus]
    labels: dict
    last_modified: datetime
