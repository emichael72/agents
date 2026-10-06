# Search Text

Finds the lines matching an extended regular expression in a file, or recursively in a folder,
and prints them as `file:line:text`. Binary files and `.git`, `.venv`, `node_modules` and
`__pycache__` folders are skipped. Output stops after 50 matches.

**Usage Example:**

```bash
bash search_text/search_text.sh AGENT_NAME
```
