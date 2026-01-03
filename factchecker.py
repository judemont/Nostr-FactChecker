from typing import Dict, List, Optional, cast
from mistralai import (
    AgentsCompletionRequestMessages,
    AgentsCompletionRequestMessagesTypedDict,
    AssistantMessage,
    AssistantMessageTypedDict,
    ContentChunk,
    ImageURLChunk,
    ImageURLChunkTypedDict,
    Mistral,
    SystemMessageTypedDict,
    TextChunk,
    TextChunkTypedDict,
    ToolMessageTypedDict,
    UserMessageTypedDict,
    ToolCall,
)
from ddgs import DDGS
import json
from datetime import datetime
import requests
from bs4 import BeautifulSoup

class FactChecker:
    def __init__(self, api_key: str, agent_id: str):
        self.client = Mistral(api_key=api_key)
        self.agent_id = agent_id
        self.warning_message = (
            "Caution: I’m just a tool. I don’t hold absolute truth or authority. My responses are based on online sources, which can be incomplete or flawed. Always verify independently."
        )

    def get_webpage_content(self, url: str, max_length: int = 10000) -> str:
        """Fetch and return the text content of a webpage."""
        try:
            response = requests.get(url, timeout=5)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')
            text = ' '.join(p.get_text().replace('\n', ' ').strip() for p in soup.find_all("p"))
            return json.dumps({"url": url, "content": text[:max_length]})
        except requests.RequestException as error:
            return json.dumps({"error": f"Failed to fetch webpage content: {error}"})


    def perform_web_search(self, query: str, num_results: int = 7) -> str:
        if num_results <= 0:
            return json.dumps({"error": "num_results must be a positive integer"})

        try:
            results = []
            search_results = DDGS().text(query, max_results=num_results)
            for result in search_results:
                results.append({"url": result["href"], "title": result["title"], "body": result["body"]})
            return json.dumps(results)
        except Exception as error:
            return json.dumps({"error": f"Web search failed: {error}"})

    def handle_tool_calls(
        self, tool_calls: List[ToolCall], messages: list[AgentsCompletionRequestMessagesTypedDict]
    ) -> list[AgentsCompletionRequestMessagesTypedDict]:
        """Process tool calls and return formatted tool results."""
        for tool_call in tool_calls:
            if tool_call.function.name in ["web_search", "search_web"] :
                query = json.loads(str(tool_call.function.arguments))["query"]
                try:
                    search_results = self.perform_web_search(query)
                except Exception as e:
                    search_results = json.dumps({"error": f"Web search failed: {e}"})
             #   print("Search results:", search_results)
                messages.append(
                    ToolMessageTypedDict(
                        role="tool",
                        content=search_results,
                        tool_call_id=tool_call.id,
                        name=tool_call.function.name,
                    )
                )
                
            elif tool_call.function.name == "get_webpage_content":
                url = json.loads(str(tool_call.function.arguments))["url"]
                try:
                    webpage_content = self.get_webpage_content(url)
                except Exception as e:
                    webpage_content = json.dumps({"error": f"Failed to get webpage content: {e}"})
                messages.append(
                    ToolMessageTypedDict(
                        role="tool",
                        content=webpage_content,
                        tool_call_id=tool_call.id,
                        name=tool_call.function.name,
                    )
                )
                print("Webpage content:", webpage_content)
            else:
                messages.append(
                    ToolMessageTypedDict(
                        role="tool",
                        content=f"Error: Unknown tool '{tool_call.function.name}'",
                        tool_call_id=tool_call.id,
                        name=tool_call.function.name,
                    )
                )
        return messages


    def formate_result(self, result: str) -> str:
        """Format the fact-checking result."""
        result = result.strip().replace("**", "").replace("__", "").replace("[", "").replace("]", " ") # Remove markdown
        return f"Fact-Check Results:\n{result}\n\n{self.warning_message}"


    def check_fact(
        self, statement: str, image_urls: Optional[List[str]] = None
    ) -> str:
        """Main method to check a factual statement."""
        import time
        sanitized_statement = statement.strip().replace('"', "'").replace("\n", " ")

        # Initialize messages with system prompt and user query
        messages: list[AgentsCompletionRequestMessagesTypedDict] = []
        
        today = datetime.today().strftime('%Y-%m-%d')
        messages.append(
            SystemMessageTypedDict(
                role="system",
                content=[
                    TextChunkTypedDict(
                        type="text",
                        text=(
                            f"Today's date is {today}."
                        ),
                    )
                ],
            )
        )
        messages.append(
              UserMessageTypedDict(
                role="user",
                content=[TextChunkTypedDict(type="text", text=f"'{sanitized_statement}'")],
            ),
        )
        
        # Add image URLs if provided
        if image_urls:
            messages.append(
                UserMessageTypedDict(
                    role="user",
                    content=[
                            ImageURLChunkTypedDict(image_url=url, type="image_url") 
                        for url in image_urls
                        ]
                    )
            )

        try:
            # Initial API call
            response = self.client.agents.complete(
                messages=list(messages),
                agent_id=self.agent_id,
                stream=False,
            )
            time.sleep(1.5)

            # Add the assistant's response to messages
            messages.append(cast(AssistantMessageTypedDict, response.choices[0].message.model_dump()))

            # Handle tool calls if present
            while response.choices[0].message.tool_calls:
                messages = self.handle_tool_calls(
                    response.choices[0].message.tool_calls, messages
                )
                # Call the API again with tool results
                response = self.client.agents.complete(
                    messages=messages,
                    agent_id=self.agent_id,
                    stream=False,
                )
                time.sleep(1.5)

                # Add the new response to messages
                messages.append(response.choices[0].message.model_dump())

            # Return the final answer
            if not response.choices[0].message.content:
                raise RuntimeError("No content returned after tool calls.")

            return self.formate_result(str(response.choices[0].message.content))

        except Exception as error:
            raise RuntimeError(f"Fact-checking failed: {error}") from error
            raise RuntimeError(f"Fact-checking failed: {error}") from error