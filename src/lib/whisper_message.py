def build_whisper_message(
    caller_name: str | None = None,
    job_type: str | None = None,
    urgency: str | None = None,
    summary: str | None = None,
) -> str:
    name = caller_name or "Unknown caller"
    job = job_type or "unspecified job"
    tier = "Emergency" if urgency == "emergency" else "Routine"
    return f"{name} calling about {job}. {tier}. {summary or ''}".strip()
