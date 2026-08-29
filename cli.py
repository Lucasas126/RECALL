import os

import dotenv

from src.config.loader import load_agent
from src.providers.provider import get_provider
import src.providers.openrouter  # noqa: F401  (registers the openrouter provider)

dotenv.load_dotenv()

USER_ID = dotenv.get_key(".env", "USER_ID")
AGENT_ID = dotenv.get_key(".env", "AGENT_ID")


def main():
    agent = load_agent(USER_ID, AGENT_ID)

    provider = get_provider(agent.provider_name, api_key=agent.api_key)

    # conversation history is the memory
    messages = [{"role": "system", "content": agent.personality_prompt}]

    print(f"{agent.name} cli — type 'exit' or 'quit' to stop.\n")

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

        messages.append({"role": "user", "content": user_input})

        print(f"\n{agent.name}: ", end="", flush=True)
        reply = ""
        for chunk in provider.invoke(messages, agent.models.chat, stream=True):
            if isinstance(chunk, str):
                print(chunk, end="", flush=True)
                reply += chunk
        print("\n")

        messages.append({"role": "assistant", "content": reply})


if __name__ == "__main__":
    main()
