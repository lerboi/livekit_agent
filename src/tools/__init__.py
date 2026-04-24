"""
Tool registry -- conditionally registers tools based on onboarding state.
Same pattern as createTools(deps) in src/tools/index.js.
"""

from .check_caller_history import create_check_caller_history_tool
from .check_customer_account import create_check_customer_account_tool
from .capture_lead import create_capture_lead_tool
from .end_call import create_end_call_tool
from .transfer_call import create_transfer_call_tool
from .check_slot import create_check_slot_tool
from .check_day import create_check_day_tool
from .next_available_days import create_next_available_days_tool
from .book_appointment import create_book_appointment_tool


def create_tools(deps: dict) -> list:
    """
    Create all tools for the voice agent session.

    Tool ordering:
    - transfer_call, capture_lead, check_caller_history, check_customer_account,
      end_call -- always available
    - check_slot, check_day, next_available_days, book_appointment -- only when
      onboarding_complete. The three availability tools split the former
      monolithic check_availability; see
      .planning/research/check-availability-split-plan.md.
    """
    tools = [
        create_transfer_call_tool(deps),
        create_capture_lead_tool(deps),
        create_check_caller_history_tool(deps),
        create_check_customer_account_tool(deps),
        create_end_call_tool(deps),
    ]

    if deps.get("onboarding_complete"):
        tools.append(create_check_slot_tool(deps))
        tools.append(create_check_day_tool(deps))
        tools.append(create_next_available_days_tool(deps))
        tools.append(create_book_appointment_tool(deps))

    return tools
