# System information

Shows information about the machine running the tools: OS, CPU, memory, disks, network, GPU, busy processes, and installed development tools.

From the repository root:

~~~bash
python3 tools/sysinfo/sysinfo.py
python3 tools/sysinfo/sysinfo.py --section=memory
~~~

The first command shows everything. Available sections are `system`, `cpu`, `memory`, `disk`, `network`, `gpu`, `processes`, and `software`.

The tool reads Linux system files and uses installed commands when available. Missing information is omitted. It is read-only and takes about a second, mostly to sample CPU usage.

In the demo, this describes minion, where the tools run. It does not query the Mac Studio model server.
