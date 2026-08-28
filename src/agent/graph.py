"""
LangGraph agent graph definition.

Graph structure (basic conversational loop):

  [START] --> chatbot --> [END]

The `chatbot` node calls the LLM and appends its reply to the state.
Tools can be added later by inserting a `tools` node and conditional edges.
"""

from __future__ import annotations

import logging
from typing import Optional

from langchain_core.messages import SystemMessage
from langgraph.graph import END, START, StateGraph

from src.agent.state import AgentState
from src.providers.openrouter_chat import OpenRouterChatModel

logger = logging.getLogger(__name__)

DEFAULT_SYSTEM_PROMPT = (
    "You are RECALL, a helpful AI assistant with memory capabilities. "
    "You are thoughtful, concise, and always honest."
)


def build_graph(
    model: str = "openai/gpt-4o-mini",
    api_key: Optional[str] = None,
    system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    streaming: bool = True,
) -> StateGraph:
    """
    Build and compile the agent StateGraph.

    Parameters
    ----------
    model:         OpenRouter model identifier, e.g. "openai/gpt-4o-mini"
    api_key:       OpenRouter API key (falls back to OPENROUTER_API_KEY env var)
    system_prompt: System instruction prepended to every LLM call
    streaming:     Whether to use streaming internally (not exposed to graph callers yet)

    Returns
    -------
    A compiled LangGraph graph ready to be invoked or streamed.
    """

    llm = OpenRouterChatModel(model=model, api_key=api_key, streaming=streaming)

    # ------------------------------------------------------------------ nodes

    def chatbot(state: AgentState) -> dict:
        """Core LLM node: prepend system prompt and call the model."""
        messages = [SystemMessage(content=system_prompt)] + state["messages"]
        response = llm.invoke(messages)
        logger.debug("LLM response: %s", response.content[:120])
        return {"messages": [response]}

    # ------------------------------------------------------------------ graph

    builder = StateGraph(AgentState)
    builder.add_node("chatbot", chatbot)
    builder.add_edge(START, "chatbot")
    builder.add_edge("chatbot", END)

    return builder.compile()
