"""Unit models for the e-Boekhouden SDK."""

from __future__ import annotations

from pydantic import model_validator
from typing_extensions import Self

from eboekhouden.models.base import EBoekhoudenModel, PaginatedResponse


class Unit(EBoekhoudenModel):
    """Unit model."""

    id: int
    singular: str | None = None
    plural: str | None = None
    description: str | None = None

    @model_validator(mode="after")
    def populate_description_and_singular(self) -> Self:
        """Keep description and singular synchronized across API versions."""
        if self.description is None and self.singular is not None:
            self.description = self.singular
        elif self.singular is None and self.description is not None:
            self.singular = self.description
        return self


class UnitList(PaginatedResponse[Unit]):
    """Paginated list of units."""

    pass
