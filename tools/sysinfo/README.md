# System information

Shows information about the machine running the tools: OS, CPU, memory, disks, network, GPU, busy processes, and
installed development tools; and about the model the agent runs on.

From the repository root:

~~~bash
python3 tools/sysinfo/sysinfo.py
python3 tools/sysinfo/sysinfo.py --section=memory
~~~

The first command shows everything. Available sections are `system`, `cpu`, `memory`, `disk`, `network`, `gpu`,
`processes`, `software`, and `model`.

The tool reads Linux system files and uses installed commands when available. Missing information is omitted. It is
read-only and takes about a second, mostly to sample CPU usage.

The machine sections describe minion, where the tools run. The `model` section describes the agent's model: each agent
puts its model id, server, profile, `max_tokens` and sampling settings in the tools' environment (`AGENT_MODEL`,
`AGENT_MODEL_SERVER`, `AGENT_MODEL_PROFILE`, `AGENT_MAX_TOKENS`, `AGENT_SAMPLING`; never the API key). When the server is
LM Studio, the section adds what it reports about the model: name, architecture, parameters, format, quantization, size,
context length, loaded instances, and capabilities (vision, tool use, reasoning). Run outside an agent, it says the model
is unknown.
