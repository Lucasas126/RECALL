import dotenv

from src.agent.agent import Agent

dotenv.load_dotenv()

USER_ID = dotenv.get_key(".env", "USER_ID")
AGENT_ID = dotenv.get_key(".env", "AGENT_ID")


def main():
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

        print(f"\n{agent.name}: ", end="", flush=True)
        result = agent.invoke(user_input)
        if isinstance(result, str):
            print(result, end="", flush=True)
        else:
            for chunk in result:
                print(chunk, end="", flush=True)
        print("\n")


if __name__ == "__main__":
    main()
