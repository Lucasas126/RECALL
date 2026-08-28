tools that the agent should be able to see, the description for the agent, and their actual functionalities in the background

# main agent toolset

recall_mode
- parameters: none
- agent description: a subagent with your latest context enters recall mode, where it enters your mind palace and focuses on retrieving a specific memory or piece of information and returns it to you.
- background behaviour: a subagent is spawned with a reduced context (only the last n messages). a system prompt injects the mode instructions, and a recall-specific, read-only toolset is exposed (see recall mode toolset below) while the editing tools are temporarily hidden. the subagent explores the mind palace until it can answer, then calls the exit tool passing a string with the retrieved information.
- return: the main agent is back in normal mode with the retrieved information available. it retains no trace of the recall process, only the information the subagent extracted.


query_memory
- parameters: query (str), top_k (int), sources (list of str, optional)
- agent description: performs a vectorial (semantic) search on your memory to find relevant information based on the query. optionally restrict it to specific sources.
- background behaviour: a vectorial search is performed across the configured sources (mind palace, conversations, obsidian, user folder) to retrieve the top_k most relevant chunks. if sources is given, only those are searched.
- return: the top_k most relevant chunks, each with its content, score and origin (source + path) so the result can be read or edited afterwards.


list
- parameters: source (str, optional), path (str, optional)
- agent description: lists the available sources, or the files and folders inside a given source and path, so you can discover what memories exist before reading them.
- background behaviour: with no arguments, returns the configured sources (mind palace, conversations, obsidian). with a source (and optional path), walks that location and returns its entries.
- return: the list of sources, or the files and folders at the requested location.


read
- parameters: memory_path (str), lines (int or list of int, optional)
- agent description: retrieves the content of a specific memory based on its path, must be in the available sources.
- background behaviour: the agent accesses a file in the available sources corresponding to memory_path (e.g. a mind palace note or an obsidian file). obsidian reads respect the safe/auto overlay: in safe mode the agent's edited version is returned, not the user's original.
- return: the content of the memory, optionally limited to the specified lines.


search
- parameters: query (str), source (str, optional)
- agent description: performs a keyword/text search across your memories to find exact matches (complements query_memory, which is semantic).
- background behaviour: a literal/regex text search is run over the files in the available sources, or a single source if given, returning the matching files and lines.
- return: the matching entries, each with source, path, line number and the matching line.


list_tags
- parameters: source (str, optional)
- agent description: lists the tags used across your notes (mind palace and obsidian), so you can navigate by topic. your own mind palace follows an obsidian-like structure, so you should tag your notes.
- background behaviour: scans the frontmatter and inline #tags of the notes in the available sources, or a single source if given, and aggregates them.
- return: the list of tags, each with how many notes use it.


find_notes_by_tag
- parameters: tags (list of str), source (str, optional), mode (str, optional: "any" or "all", default "any")
- agent description: finds the notes tagged with the given tags. mode "any" returns notes with at least one of the tags; mode "all" returns only notes that have all of the specified tags.
- background behaviour: scans the notes in the available sources, or a single source, and returns those whose frontmatter or inline #tags match the given tags according to mode.
- return: the matching notes, each with source and path.


write
- parameters: memory_path (str), content (str)
- agent description: creates a new memory or overwrites an existing one at the given path, must be in the available sources.
- background behaviour: writes content to the file at memory_path, creating it if it does not exist. obsidian writes respect the safe/auto overlay: in safe mode the write is redirected to the agent's shadow copy, never touching the user's file. after writing, the vector index for that file is refreshed.
- return: confirmation, with the resolved path that was actually written.


edit
- parameters: memory_path (str), old_string (str), new_string (str)
- agent description: edits part of an existing memory by replacing a specific fragment, without rewriting the whole file.
- background behaviour: locates old_string in the file at memory_path (it must match exactly and be unique, using surrounding context) and replaces it with new_string. this is how editors/agents like copilot apply partial edits, avoiding full-content rewrites. same overlay and index-refresh rules as write apply.
- return: confirmation, with the resolved path and whether the replacement was applied.


move
- parameters: memory_path (str), new_path (str)
- agent description: moves or renames a memory from one path to another, must stay within the available sources. use it to reorganize your mind palace.
- background behaviour: moves the file from memory_path to new_path and updates its entry in the vector index. obsidian backlinks are updated: any [[note]] reference pointing to the old path is rewritten to the new one. obsidian writes respect the safe/auto overlay: in auto mode the file is moved directly; in safe mode only the agent's shadow copy is moved, never the user's original.
- return: confirmation, with the old and new resolved paths and how many backlinks were updated.


delete
- parameters: memory_path (str)
- agent description: deletes a memory at the given path. from your point of view it stops existing (it disappears from list, read and search).
- background behaviour: for agent-owned sources (e.g. the mind palace) the file is removed and its chunks are dropped from the vector index. for obsidian in auto mode the file is deleted directly. for obsidian in safe mode the user's original is never touched: instead a tombstone shadow copy is written in the ai-agent folder with the content "deleted", and the file is removed from the index and hidden from list (so for the agent it no longer exists, while the user can still see the deletion was intended).
- return: confirmation that the memory was deleted.


web_search
- parameters: query (str), top_k (int)
- agent description: performs a web search based on the query and retrieves the top_k most relevant results.
- background behaviour: the agent sends the query to a web search engine and retrieves the top_k most relevant results.
- return: the top_k most relevant web search results are returned.

web_retrieve
- parameters: url (str)
- agent description: retrieves the content of a specific web page based on its URL.
- background behaviour: the agent accesses the web page corresponding to the provided URL and fetches its content.
- return: the content of the web page corresponding to the provided URL is returned.


## recall mode toolset

in recall mode the subagent keeps only search/read tools — it can look things up but cannot modify anything.
- exposed: list, read, search, query_memory, list_tags, find_notes_by_tag, web_search, web_retrieve
- hidden: write, edit, move, delete (and recall_mode itself)

## dream mode toolset
in dream mode all tools are available except for recall_mode itself.
- exposed: list, read, search, query_memory, list_tags, find_notes_by_tag, write, edit, move, delete, web_search, web_retrieve
- hidden: recall_mode