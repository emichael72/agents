# Memory

Memory is a set of short notes shared by the agents between chats. It can hold project decisions, preferences, or things worth remembering.

Notes normally live in `.memory/`. The index is loaded at startup so an agent knows which topics exist; it reads the full note when needed. These files stay local and are ignored by Git.

## Save, read, or forget

From the repository root:

~~~bash
python3 tools/memory/memory.py --action=save --topic=preferences --text="Prefers short answers"
python3 tools/memory/memory.py --action=read --topic=preferences
python3 tools/memory/memory.py --action=forget --topic=preferences
~~~

Read without a topic to see the index. Saving appends a line; `--replace=true` rewrites the note. `--summary` sets its one-line description in the index.

A topic is stored as `<topic>.md` and holds at most 4,000 characters. Names are normalized to lowercase letters, digits, dashes, and underscores. Notes are for lasting information, not secrets.

The folder named memory in [context/paths.json](https://github.com/emichael72/agents/blob/a2fe18a204843563134bb1ed0d7aaf63d558691e/context/paths.json) must allow reading and writing. Typing `exit` in a chat may give the agent a final turn to save notes; Ctrl+C and one-prompt runs skip that turn.
