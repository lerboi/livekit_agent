"""The appointments agent's tool surface: eight tools, always the same eight."""

from ....tools.end_call import create_end_call_tool
from .availability import (
    create_check_day_tool,
    create_check_slot_tool,
    create_next_available_days_tool,
)
from .book import create_book_appointment_tool
from .manage import (
    create_cancel_appointment_tool,
    create_find_appointment_tool,
    create_reschedule_appointment_tool,
)

TOOL_NAMES = (
    "check_slot",
    "check_day",
    "next_available_days",
    "book_appointment",
    "find_appointment",
    "reschedule_appointment",
    "cancel_appointment",
    "end_call",
)


def create_tools(deps: dict) -> list:
    return [
        create_check_slot_tool(deps),
        create_check_day_tool(deps),
        create_next_available_days_tool(deps),
        create_book_appointment_tool(deps),
        create_find_appointment_tool(deps),
        create_reschedule_appointment_tool(deps),
        create_cancel_appointment_tool(deps),
        create_end_call_tool(deps),
    ]
