"""
Agent state definition for LangGraph.
"""

from __future__ import annotations

from typing import Annotated

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict


class AgentState(TypedDict):
    """State carried through every node in the agent graph."""

    # add_messages appends new messages instead of overwriting the list
    messages: Annotated[list[BaseMessage], add_messages]
