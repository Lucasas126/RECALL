import logging
import os

import dotenv
from langchain_core.messages import AIMessage, HumanMessage

from src.embeddings.data_source import DataSource, DataSource, DataSourceConfig
from src.embeddings.data_source import DataSourceConfig
from src.agent import build_graph

dotenv.load_dotenv()

logging.basicConfig(level=logging.WARNING)

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")


def run_cli():
    """Simple terminal chat loop backed by the LangGraph agent."""

    graph = build_graph(model=MODEL, api_key=OPENROUTER_API_KEY)

    print("RECALL agent ready. Type 'exit' or 'quit' to stop.\n")

    # Persistent conversation history
    conversation: list = []

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye!")
            break

        if not user_input:
            continue
        if user_input.lower() in {"exit", "quit"}:
            print("Bye!")
            break

        conversation.append(HumanMessage(content=user_input))

        # stream_mode="messages" yields (BaseMessageChunk, metadata) tuples
        # one chunk per token when the model streams
        print("\nRECALL: ", end="", flush=True)
        ai_reply = ""
        for msg_chunk, _meta in graph.stream(
            {"messages": conversation},
            stream_mode="messages",
        ):
            if hasattr(msg_chunk, "content") and msg_chunk.content:
                print(msg_chunk.content, end="", flush=True)
                ai_reply += msg_chunk.content

        print("\n")
        conversation.append(AIMessage(content=ai_reply))


# if __name__ == "__main__":
#     run_cli()


if __name__ == "__main__":

    from src.providers.openrouter import OpenRouterProvider
    
    provider = OpenRouterProvider()

    # Example usage
    config = DataSourceConfig(
        embeddings_model="openai/text-embedding-3-small",
        chunk_size=100,
        file_types=["txt", "md", "jsonl"],
        collection_save_path="./chroma_db",
    )
    source = DataSource(name="example_data_folder", path="./example_data_folder", config=config)
    results = source.query("example query", top_k=5)
    for result in results:
        print(result)