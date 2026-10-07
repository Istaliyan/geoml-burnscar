"""Typed configuration for the thin vertical slice."""

from pathlib import Path

from pydantic import BaseModel, Field, model_validator


class SamplePipelineConfig(BaseModel):
    """Paths and chip geometry for one aligned before/after sample."""

    before: Path
    after: Path
    label: Path
    output_dir: Path
    chip_size: int = Field(default=256, gt=0)

    @model_validator(mode="after")
    def paths_are_distinct(self) -> "SamplePipelineConfig":
        if len({self.before, self.after, self.label}) != 3:
            raise ValueError("before, after, and label must be different files")
        return self
