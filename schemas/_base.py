from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

# Text that must say something: surrounding whitespace is trimmed, and blank is an error.
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class Schema(BaseModel):
    """Base for every agent output: immutable, and unknown fields are errors.

    Rejecting unknown fields catches an agent inventing keys or misspelling one, instead of
    silently dropping it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
