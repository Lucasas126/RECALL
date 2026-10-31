# Features

## Multimodal capabilities

- ~~Allow all models with image support to process video. If a model does not natively accept video directly, split the video into frames and audio. Send the audio to STT, send the frames separately, and inform the model that they come from a decomposed video.~~
- Include message timestamps in conversations so models know the relevant dates. Include [AUDIO] and [VIDEO] tags in the conversation to indicate when a message contains or was generated from audio or video content. This will help models understand the context of the conversation and provide more accurate responses.

## Search and integrations

- Add date filters to web search tools.
- Add Google Calendar MCP integration.

## Automation and behavior

- Add sleep mode.
- Add automations with rules and actions based on conversation or calendar events.

## Configuration and interface

- Remove unnecessary messages from the settings selection and other configuration options to keep the interface clean.

## Chat UI

~~- Option to create new chats: context is empty, except for the current agent system prompt.~~
~~- Option to choose from the different models the user has access to.~~
~~- Option to view and manage existing chats: users can see a list of their previous conversations, with the ability to delete or continue them.~~

## Recall
- make it so associaive memory is inyected into the system prompt while the model has started generating text, so its faster and more efficient. This will allow the model to have access to relevant information without having to wait for the entire context to be processed before generating a response. Make it so the model is aware that this can happen, so it can adjust its behavior accordingly. This will improve latency. Since the process will be parallel, we have to adjust the timings so it makes sense (right now since they are not parallel, they are just summed in the latency metric that we print in the debug panel in the web ui)
