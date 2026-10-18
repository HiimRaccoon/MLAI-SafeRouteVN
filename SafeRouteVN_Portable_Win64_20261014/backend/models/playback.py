"""Additive M3 playback controls; all physical progress remains in M2 SDK."""
from pydantic import Field, field_validator

from backend.models.http import AcceptPlanRequest, ReplayControlRequest

PLAYBACK_SPEEDS = (1, 2, 4, 8, 16, 32, 64, 128)


class PlaybackSpeedRequest(ReplayControlRequest):
    speed: int = Field(strict=True, description="Discrete playback multiplier, from 1 to 128 in powers of two.", json_schema_extra={"enum": list(PLAYBACK_SPEEDS)})

    @field_validator("speed")
    @classmethod
    def allowed_speed(cls, value):
        if value not in PLAYBACK_SPEEDS:
            raise ValueError("Playback speed must be 1, 2, 4, 8, 16, 32, 64 or 128")
        return value


class StartPlaybackRequest(AcceptPlanRequest):
    speed: int = Field(default=1, strict=True, description="Discrete playback multiplier, from 1 to 128 in powers of two.", json_schema_extra={"enum": list(PLAYBACK_SPEEDS)})
    _allowed_speed = field_validator("speed")(PlaybackSpeedRequest.allowed_speed.__func__)


class AutoDeliverRequest(ReplayControlRequest):
    enabled: bool = Field(strict=True)
