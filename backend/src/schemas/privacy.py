from pydantic import BaseModel, Field


class PrivacyReleaseInput(BaseModel):
    study_id: int = Field(gt=0)
    name: str = Field(min_length=2, max_length=120)
    epsilon: float = Field(gt=0)
