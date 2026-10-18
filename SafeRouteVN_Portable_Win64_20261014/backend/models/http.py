from datetime import datetime, timedelta
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

Identifier = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]*$")]


class HttpModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ExpectedRevision(HttpModel):
    head_version: str = Field(description="Canonical nonnegative int64 decimal string; compared with server state.", pattern=r"^(0|[1-9][0-9]*)$", max_length=19)
    generation: str = Field(pattern=r"^(0|[1-9][0-9]*)$", max_length=19)

    @field_validator("head_version", "generation")
    @classmethod
    def int64_counter(cls, value: str) -> str:
        if int(value) >= 1 << 63:
            raise ValueError("Counter exceeds signed int64")
        return value


class LoadScenarioRequest(HttpModel):
    request_id: Identifier
    replace_session_id: Identifier | None = None


class LoadRunRequest(LoadScenarioRequest):
    pass


class OptimizeRequest(HttpModel):
    request_id: Identifier
    profile: Literal["FASTEST", "BALANCED", "SAFER"] = "BALANCED"
    expected_revision: ExpectedRevision | None = None


class AcceptPlanRequest(HttpModel):
    request_id: Identifier
    expected_revision: ExpectedRevision


class ApplyEventRequest(AcceptPlanRequest):
    pass


class ReplayStepRequest(AcceptPlanRequest):
    target_time: str | None = None

    @field_validator("target_time")
    @classmethod
    def timestamp(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?\+07:00", value):
            raise ValueError("ISO +07:00 with at most six fractional digits required")
        instant = datetime.fromisoformat(value)
        if instant.utcoffset() != timedelta(hours=7):
            raise ValueError("Timezone must be +07:00")
        return value


class CancelJobRequest(LoadScenarioRequest):
    pass


class CompareProfilesRequest(HttpModel):
    request_id: Identifier
    expected_revision: ExpectedRevision | None = None
    job_ids: Annotated[list[Identifier], Field(min_length=3, max_length=3)] | None = None

    @field_validator("job_ids")
    @classmethod
    def distinct_jobs(cls, value):
        if value is not None and len(set(value)) != 3:
            raise ValueError("Three distinct job IDs required")
        return value


class ReplayControlRequest(LoadScenarioRequest):
    pass


class Diagnostic(HttpModel):
    severity: Literal["ERROR", "WARNING"] = "ERROR"
    code: str
    path: str
    message: str


class HttpResponse(HttpModel):
    schema_version: Literal["saferoute-m3-http-response/1"] = "saferoute-m3-http-response/1"
    request_id: str
    status: Literal["OK", "ERROR"]
    data: dict[str, JsonValue] | None
    diagnostics: list[Diagnostic] = Field(default_factory=list)


REQUEST_MODELS = (LoadScenarioRequest, OptimizeRequest, AcceptPlanRequest,
                  ApplyEventRequest, ReplayStepRequest, CancelJobRequest, ReplayControlRequest, CompareProfilesRequest)
