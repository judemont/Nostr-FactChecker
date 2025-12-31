from mistralai import AgentsCompletionRequestMessagesTypedDict, ContentChunk, ImageURLChunk, ImageURLChunkTypedDict, Mistral, TextChunk, TextChunkTypedDict, UserMessageTypedDict

class FactChecker:
    def __init__(self, api_key: str, agent_id: str):
        self.client: Mistral = Mistral(api_key=api_key)
        self.agent_id = agent_id
        self.end_message = "Warning: This tool is still in beta and may produce inaccurate results. Please always verify the information from reliable sources."
        
    def check_fact(self, statement: str, images_URLs: list[str] | None = None) -> str:
        sanitized_statement = statement.strip().replace('"', "'")
                    
        images_messages = []
        if images_URLs is not None:
            images_messages = [
                UserMessageTypedDict(
                    role="user",
                    content=[
                            ImageURLChunkTypedDict(image_url=url, type="image_url") 
                        for url in images_URLs
                        ]
                    )
            ]
         

        text_messages = [
            UserMessageTypedDict(
                role="user",
                content=[
                    TextChunkTypedDict( type="text", text=f"Please verify the following statement: '{sanitized_statement}'")
                ]
            )
        ]
        
        try:
            res = self.client.agents.complete(
                messages=[
                    *images_messages,
                    *text_messages
                ],
                agent_id=self.agent_id,
                stream=False,
            )
            
        except Exception as e:
            raise RuntimeError("Fact-checking service request failed") from e
        
        if res.choices[0].message.content is None:
            raise RuntimeError("Fact-checking service returned no content")
        
        return f"{res.choices[0].message.content}\n\n\n\n{self.end_message}"