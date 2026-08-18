import os   
import dotenv

from src.providers.openrouter import OpenRouterProvider

dotenv.load_dotenv()
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

if __name__ == "__main__":
    # Example usage
    messages = [
        {"role": "user", "content": "Hello!"}
    ]
    model = "openai/gpt-4o-mini"
    api_key = OPENROUTER_API_KEY

    provider = OpenRouterProvider()
    generator = provider.invoke(messages, model, api_key, stream=True)

    # Print each chunk as it arrives
    final_result = None
    for chunk in generator:
        if isinstance(chunk, dict):
            # This is the final result with usage info
            final_result = chunk
        else:
            # This is a chunk of content (string)
            print(chunk, end="", flush=True)
    
    print(f"\n\n{'='*50}")
    print(f"Final result: {final_result}")
    print(f"Usage: {final_result.get('usage') if final_result else 'N/A'}")
