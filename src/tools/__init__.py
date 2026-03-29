"""
Tool registry -- conditionally registers tools based on onboarding state.
Same pattern as createTools(deps) in src/tools/index.js.
"""

from .check_caller_history import create_check_caller_history_tool
from .capture_lead import create_capture_lead_tool
from .end_call import create_end_call_tool
from .transfer_call import create_transfer_call_tool
from .check_availability import create_check_availability_tool
from .book_appointment import create_book_appointment_tool


def create_tools(deps: dict) -> list:
    """
    Create all tools for the voice agent session.

    Tool ordering matches the current system:
    - transfer_call, capture_lead, check_caller_history, end_call -- always available
    - check_availability, book_appointment -- only when onboarding_complete
    """
    tools = [
        create_transfer_call_tool(deps),
        create_capture_lead_tool(deps),
        create_check_caller_history_tool(deps),
        create_end_call_tool(deps),
    ]

    if deps.get("onboarding_complete"):
        tools.append(create_check_availability_tool(deps))
        tools.append(create_book_appointment_tool(deps))

    return tools
