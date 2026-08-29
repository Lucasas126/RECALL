import sys

import dotenv

from src.agent.agent import Agent

dotenv.load_dotenv()

USER_ID = dotenv.get_key(".env", "USER_ID")
AGENT_ID = dotenv.get_key(".env", "AGENT_ID")

GRAY_ITALIC = "\033[3;90m"
RESET = "\033[0m"

def main():
    # Enable colors and UTF-8 output
    sys.stdout.reconfigure(encoding="utf-8")

    agent = Agent.from_config(USER_ID, AGENT_ID)

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

        result = agent.invoke(user_input)
        if isinstance(result, str):
            print(f"\n{agent.name}: {result}\n")
        else:
            mode = None
            for kind, text in result:
                if kind != mode:
                    if mode == "reasoning":
                        print(RESET, end="")  # close reasoning styling
                    if kind == "reasoning":
                        print(f"\n{GRAY_ITALIC}thinking: ", end="", flush=True)
                    else:
                        print(f"\n{agent.name}: ", end="", flush=True)
                    mode = kind
                print(text, end="", flush=True)
            print(RESET + "\n")


if __name__ == "__main__":
    main()
