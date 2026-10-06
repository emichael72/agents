# Greet User

Greets a user by name. Without `--name`, it greets the current shell user (`$USER`). When an
agent runs it, `AGENT_NAME` names that agent, and the greeting says which agent sent it.

**Usage Example:**

```bash
bash greet_user/greet_user.sh --name Alice
```