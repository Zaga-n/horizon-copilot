"""The one immutable model base and text constraint shared by every chat domain value."""

from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict


class StrictModel(BaseModel):
    """Immutable validated wire and persistence values."""

    model_config = ConfigDict(frozen=True, extra="forbid")


def reject_nul(value: str) -> str:
    """PostgreSQL text cannot store NUL, so such input is invalid before any state change."""
    if "\x00" in value:
        raise ValueError("text must not contain NUL characters")
    return value


type StorableText = Annotated[str, AfterValidator(reject_nul)]
