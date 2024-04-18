import httpx
import os
import asyncio
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

COZE_ENDPOINT = os.getenv("COZE_ENDPOINT", "")
COZE_API_KEY = os.getenv("COZE_API_KEY", "")
DEFAULT_COZE_BOT_ID = os.getenv("DEFAULT_COZE_BOT_ID", "")


class Message:
    def __init__(self, content):
        self.content = content


class Choice:
    def __init__(self, message):
        self.message = message


class Completion:
    def __init__(self, choices):
        self.choices = choices

    @staticmethod
    def from_response(response):
        # print(f"Transforming response: {response}")
        primary_response = next(
            (
                msg
                for msg in response.get("messages", [])
                if msg.get("type") == "answer"
            ),
            None,
        )
        content = (
            primary_response.get("content", "No answer found in response.")
            if primary_response
            else "No answer found in response."
        )
        return Completion([Choice(Message(content))])


class AsyncCoze:
    def __init__(self, api_key: str, timeout=30.0):
        self.api_key = api_key
        self.client = httpx.AsyncClient(timeout=timeout)
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Connection": "keep-alive",
        }
        self.endpoint = COZE_ENDPOINT
        self.chat = self.Chat(self)

    class Chat:
        def __init__(self, outer):
            self.completions = self.Completions(outer)

        class Completions:
            def __init__(self, outer):
                self.outer = outer

            async def create(self, **model_params):
                # print(f"Sending API Request: {model_params}")
                response = await self.outer.client.post(
                    self.outer.endpoint,
                    headers=self.outer.headers,
                    json=model_params,
                )
                # print(f"API Response Received: {response.json()}")
                return Completion.from_response(response.json())


# used for testing
async def main():
    client = AsyncCoze(api_key=COZE_API_KEY)
    query = """{"input": "The tallest building in the world as of April 2023 is the Burj Khalifa."}"""

    model_params = {
        "bot_id": DEFAULT_COZE_BOT_ID,
        "user": "KyleToh",
        "query": query,
        "stream": False,
    }
    completion = await client.chat.completions.create(**model_params)
    response = completion.choices[0].message.content
    print(f"Final Extracted Response: {response}")


if __name__ == "__main__":
    asyncio.run(main())
