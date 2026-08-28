from pydantic import BaseModel

class LLMProvider(BaseModel):

    def invoke(self, messages: list[dict], model: str, stream=True):
        """
        Invokes the LLM provider API with the given messages and model.
        If stream is True, it will print the response in real-time.
        """
        raise NotImplementedError("This method should be implemented by subclasses.")

    def invoke_embeddings(self, text: str, model: str):
        """
        Invokes the LLM provider API to get embeddings for the given text.
        """
        raise NotImplementedError("This method should be implemented by subclasses.")