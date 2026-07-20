from pydantic import BaseModel


class TherapistResult(BaseModel):
    name: str
    credentials: str | None = None
    location: str | None = None
    profile_url: str
    description: str | None = None
    accepting_new_clients: bool | None = None
    telehealth: bool | None = None
    source: str  # "inclusive_therapists" | "psychology_today"
