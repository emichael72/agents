# System Info

Reports on the machine running the tools, in sections of `key  value` lines. Read-only; takes
about a second, mostly measuring CPU utilization.

| Section     | Contents                                                                                                                   |
|-------------|----------------------------------------------------------------------------------------------------------------------------|
| `system`    | Hostname, OS release, kernel, architecture, virtualization, uptime and boot time, local time and time zone, user           |
| `cpu`       | Model, physical and logical cores, utilization and I/O wait (measured over 0.5 s), load average, clock speed, temperatures |
| `memory`    | RAM used, total and available; buffers and page cache; swap                                                                |
| `disk`      | Each real file system: used, total, free, type and device                                                                  |
| `network`   | Each interface: state, addresses, traffic since boot; the default gateway                                                  |
| `gpu`       | Graphics adapters (`lspci`); utilization, memory and temperature for NVIDIA GPUs (`nvidia-smi`)                            |
| `processes` | Process count and the five busiest by CPU                                                                                  |
| `software`  | Python (the one running the tool and the agents' `.venv`), gcc, make, git, doxygen, node, npm, gh, uv, docker              |

**Usage Example:**

```bash
python3 sysinfo/sysinfo.py                     # everything
python3 sysinfo/sysinfo.py --section memory    # one section
```

Uses only the Python standard library: it reads `/proc`, `/sys` and `/etc/os-release`, and runs
`ip`, `ps`, `lspci`, `nvidia-smi`, `systemd-detect-virt` and the tools' `--version` when present.
Anything missing is left out rather than failing.
