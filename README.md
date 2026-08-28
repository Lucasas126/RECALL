# RECALL

recall is a framework for deploying AI agents with context and tools
initially, it will only use telegram to talk to the user/terminal for local development, and openrouter as the AI model provider
for the backend, I was thinking of using langgraph/langchain
main ideas:
- mind palace: it's basically a folder where an agent can have its memories, notes and personal recollections, it's their property and the user can't normally access it
- sleep mode: a process that runs every x hours during which the agent reviews the latest conversations it has had with the user, and performs pending tasks or creates its personal notes in its mind palace
- recall mode: the agent can enter its mind palace, with the intention of retrieving specific memories or pieces of information, but with a context trimmed to the last messages (to avoid saturating its context). To exit, it returns only the necessary information, removing from its context the entire process it needed to extract it.
- dream mode: an async process that happens every x hours during which the agent can explore its mind palace, reviews the latest conversations with the user, explores hypothetical scenarios, and consolidates its knowledge in the mind palace
- associative memory: with all user messages, a subprocess is launched that:
    - asks a subagent to summarize the query in 3-5 simple words
    - those words are vectorized and searched within a vector database for related information
    - the agent is suggested the top n results from that vector query as suggestions so they can search and use them to respond to the user
- vector database with multiple sources:
    - agent's mind palace
    - user-provided file folder
- tools for editing/reading from a user's obsidian vault with two modes:
    - auto mode: the agent can edit/create files directly in the vault
    - safe mode: the agent should never be able to directly edit an obsidian file, it should only be given the impression that it does. That is, if the agent tries to edit/create vault-path/hobbies/arte.md, a copy should be created in vault-path/ai-agent/hobbies/arte.md. And when it tries to read an obsidian file (for example, that same file) it should be given the version edited by them, since it will be the last one they're aware of.
    apart from those two modes, there will be a boolean of "auto commit" to determine whether each change made by the agent should create a commit in the obsidian vault (requires the vault to be a git repository)
- internet access and search, probably with Tavily search
