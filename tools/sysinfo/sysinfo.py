"""
Module: sysinfo.py

Description:
    Reports on the machine running the tools: the system, CPU and its current load, memory, disks,
    network, GPU, busiest processes and the versions of the development software.

    Key design points:
      - Read-only, standard library only (and the tools' shared command line): reads /proc, /sys and /etc/os-release, and runs a few
        system commands when present (ip, ps, nvidia-smi, the tools' --version). Anything missing
        is left out rather than failing.
      - CPU utilization is measured over SAMPLE_SECONDS (two readings of /proc/stat).
      - One section, or all of them; output is "key: value" lines under a heading per section.
      - It runs under the system's python3 (3.9 on RHEL 9), outside the agents' .venv.
"""

import getpass
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

# Import the tools' shared command line from the repository root (the nearest folder above
# holding pyproject.toml).
sys.path.insert(0, str(next(p for p in Path(__file__).resolve().parents
                            if (p / "pyproject.toml").is_file())))
from tools.common.cli import ToolArgumentParser


class SystemReport:
    """
    The report's sections; each is a method of the same name returning (key, value) rows.
    """

    VERSION = "1.0.0"
    SECTIONS = ("system", "cpu", "memory", "disk", "network", "gpu", "processes", "software")
    SAMPLE_SECONDS = 0.5
    REAL_FILESYSTEMS = {"ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "vfat", "exfat", "ntfs", "ntfs3", "f2fs",
                        "nfs", "nfs4", "cifs", "fuseblk"}
    SOFTWARE = [  # (name, command printing its version)
        ("gcc", ["gcc", "--version"]), ("make", ["make", "--version"]), ("git", ["git", "--version"]),
        ("doxygen", ["doxygen", "--version"]), ("node", ["node", "--version"]), ("npm", ["npm", "--version"]),
        ("gh", ["gh", "--version"]), ("uv", ["uv", "--version"]), ("docker", ["docker", "--version"]),
    ]

    def __init__(self, repo_root: Optional[Path] = None) -> None:
        """
        Args:
            repo_root: The agents repository, for its .venv; None finds it: the nearest folder
                above this file holding pyproject.toml.
        """
        self.repo_root = repo_root or next(p for p in Path(__file__).resolve().parents
                                           if (p / "pyproject.toml").is_file())

    @staticmethod
    def run(command: list[str], timeout: float = 5) -> str:
        """
        Run a command and return its output, or "" if it is missing or fails.
        Args:
            command: The program and its arguments.
            timeout: Seconds to wait.
        Returns:
            str: Its standard output, stripped.
        """
        if not shutil.which(command[0]):
            return ""
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return result.stdout.strip() if result.returncode == 0 else ""

    @staticmethod
    def read(path: str) -> str:
        """Read a small system file, or return "" if it cannot be read."""
        try:
            return Path(path).read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return ""

    @staticmethod
    def size(n: float) -> str:
        """Format bytes as KiB, MiB, GiB or TiB with one decimal."""
        for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
            if n < 1024 or unit == "TiB":
                return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
            n /= 1024
        return str(n)

    @staticmethod
    def duration(seconds: float) -> str:
        """Format seconds as e.g. "3d 4h 12m"."""
        minutes, _ = divmod(int(seconds), 60)
        hours, minutes = divmod(minutes, 60)
        days, hours = divmod(hours, 24)
        return " ".join(f"{v}{u}" for v, u in ((days, "d"), (hours, "h"), (minutes, "m")) if v) or "under a minute"

    def system(self) -> list[tuple[str, str]]:
        """The machine, its OS and how long it has been up."""
        release = dict(line.split("=", 1) for line in self.read("/etc/os-release").splitlines() if "=" in line)
        uptime = float(self.read("/proc/uptime").split()[0]) if self.read("/proc/uptime") else 0
        virtualization = self.run(["systemd-detect-virt"]) or "none detected"
        return [
            ("hostname", socket.gethostname()),
            ("os", release.get("PRETTY_NAME", platform.system()).strip('"')),
            ("kernel", platform.release()),
            ("architecture", platform.machine()),
            ("virtualization", virtualization),
            ("uptime", f"{self.duration(uptime)} (booted {time.strftime('%Y-%m-%d %H:%M', time.localtime(time.time() - uptime))})"),
            ("local time", time.strftime("%Y-%m-%d %H:%M:%S %Z (UTC%z)")),
            ("user", getpass.getuser()),
        ]

    def cpu_times(self) -> list[int]:
        """The aggregate CPU time counters from /proc/stat."""
        return [int(v) for v in self.read("/proc/stat").splitlines()[0].split()[1:]]

    def cpu(self) -> list[tuple[str, str]]:
        """The processor, its current utilization, load and temperature."""
        info = self.read("/proc/cpuinfo")
        model = next((line.split(":", 1)[1].strip() for line in info.splitlines() if line.startswith("model name")),
                     platform.processor() or "unknown")
        cores = {(p, c) for p, c in zip(
            [line.split(":")[1].strip() for line in info.splitlines() if line.startswith("physical id")],
            [line.split(":")[1].strip() for line in info.splitlines() if line.startswith("core id")])}
        speeds = [float(line.split(":")[1]) for line in info.splitlines() if line.startswith("cpu MHz")]
        first = self.cpu_times()
        time.sleep(self.SAMPLE_SECONDS)
        second = self.cpu_times()
        delta = [b - a for a, b in zip(first, second)]
        idle = delta[3] + (delta[4] if len(delta) > 4 else 0)  # idle + iowait
        busy = 100 * (1 - idle / sum(delta)) if sum(delta) else 0
        iowait = 100 * delta[4] / sum(delta) if len(delta) > 4 and sum(delta) else 0
        load = os.getloadavg()
        rows = [
            ("model", model),
            ("cores", f"{len(cores) or '?'} physical, {os.cpu_count()} logical"),
            ("utilization", f"{busy:.1f}% (iowait {iowait:.1f}%, measured over {self.SAMPLE_SECONDS}s)"),
            ("load average", f"{load[0]:.2f} {load[1]:.2f} {load[2]:.2f} (1, 5, 15 min)"),
        ]
        if speeds:
            rows.append(("clock", f"{sum(speeds) / len(speeds):.0f} MHz average, {max(speeds):.0f} MHz highest"))
        temperatures = []
        for zone in sorted(Path("/sys/class/thermal").glob("thermal_zone*")):
            kind, value = self.read(f"{zone}/type"), self.read(f"{zone}/temp")
            if value.lstrip("-").isdigit():
                temperatures.append(f"{kind} {int(value) / 1000:.0f}°C")
        if temperatures:
            rows.append(("temperature", ", ".join(temperatures[:4])))
        return rows

    def memory(self) -> list[tuple[str, str]]:
        """RAM and swap use."""
        values = {line.split(":")[0]: int(line.split()[1]) * 1024 for line in self.read("/proc/meminfo").splitlines()
                  if len(line.split()) >= 2 and line.split()[1].isdigit()}
        total, available = values.get("MemTotal", 0), values.get("MemAvailable", 0)
        swap_total, swap_free = values.get("SwapTotal", 0), values.get("SwapFree", 0)
        return [
            ("ram", f"{self.size(total - available)} used of {self.size(total)} ({100 * (total - available) / total:.0f}%), "
                    f"{self.size(available)} available" if total else "unknown"),
            ("cache", f"{self.size(values.get('Cached', 0) + values.get('Buffers', 0))} buffers and page cache"),
            ("swap", f"{self.size(swap_total - swap_free)} used of {self.size(swap_total)}" if swap_total else "none"),
        ]

    def disk(self) -> list[tuple[str, str]]:
        """Each real file system's size and use."""
        rows, seen = [], set()
        for line in self.read("/proc/mounts").splitlines():
            device, mount, kind = line.split()[:3]
            if kind not in self.REAL_FILESYSTEMS or device in seen:
                continue
            seen.add(device)
            try:
                usage = shutil.disk_usage(mount.replace("\\040", " "))
            except OSError:
                continue
            rows.append((mount, f"{self.size(usage.used)} used of {self.size(usage.total)} ({100 * usage.used / usage.total:.0f}%), "
                                f"{self.size(usage.free)} free, {kind} on {device}"))
        return rows or [("disks", "none found")]

    def network(self) -> list[tuple[str, str]]:
        """Interfaces, their addresses and traffic since boot."""
        traffic = {}
        for line in self.read("/proc/net/dev").splitlines()[2:]:
            name, counters = line.split(":", 1)
            fields = counters.split()
            traffic[name.strip()] = (int(fields[0]), int(fields[8]))
        rows = []
        for line in self.run(["ip", "-brief", "address"]).splitlines():
            parts = line.split()
            name, state, addresses = parts[0], parts[1], [a for a in parts[2:] if not a.startswith("fe80")]
            if name == "lo":
                continue
            received, sent = traffic.get(name.split("@")[0], (0, 0))
            rows.append((name, f"{state}, {' '.join(addresses) or 'no address'}, "
                               f"received {self.size(received)}, sent {self.size(sent)}"))
        route = self.run(["ip", "route", "show", "default"]).split()
        if "via" in route:
            rows.append(("default gateway", route[route.index("via") + 1]))
        return rows or [("interfaces", "none found")]

    def gpu(self) -> list[tuple[str, str]]:
        """Graphics adapters, with utilization and memory for NVIDIA GPUs (nvidia-smi)."""
        output = self.run(["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                      "--format=csv,noheader,nounits"])
        rows = []
        for i, line in enumerate(output.splitlines()):
            name, used_pct, mem_used, mem_total, temperature = [v.strip() for v in line.split(",")]
            rows.append((f"gpu{i}", f"{name}, {used_pct}% busy, {mem_used} of {mem_total} MiB, {temperature}°C"))
        # Any graphics hardware, NVIDIA or not, from the PCI bus
        for line in self.run(["lspci"]).splitlines():
            if any(kind in line for kind in ("VGA compatible controller", "3D controller", "Display controller")):
                rows.append(("adapter", line.split(": ", 1)[1]))
        return rows or [("gpu", "none found")]

    def processes(self) -> list[tuple[str, str]]:
        """How many processes run, and the busiest five."""
        count = sum(1 for p in Path("/proc").iterdir() if p.name.isdigit())
        rows = [("count", str(count))]
        top = [line for line in self.run(["ps", "-eo", "pid,pcpu,pmem,args", "--sort=-pcpu", "--no-headers"]).splitlines()
               if line.split()[0] != str(os.getpid())][:5]  # Not this report itself
        for line in top:
            pid, cpu_pct, mem_pct, command = line.split(None, 3)
            command = command if len(command) <= 70 else command[:67] + "..."
            rows.append((f"pid {pid}", f"{cpu_pct}% CPU, {mem_pct}% memory: {command}"))
        return rows

    def software(self) -> list[tuple[str, str]]:
        """Python and the development tools' versions."""
        rows = [("python (running this tool)", f"{platform.python_version()} at {sys.executable}")]
        venv_python = self.repo_root / ".venv" / "bin" / "python"
        if venv_python.exists():
            rows.append(("python (agents .venv)", f"{self.run([str(venv_python), '--version']).removeprefix('Python ')} "
                                                  f"at {venv_python}"))
        for name, command in self.SOFTWARE:
            version = self.run(command).splitlines()
            if version:
                rows.append((name, version[0]))
        return rows

    @classmethod
    def build_parser(cls) -> ToolArgumentParser:
        """
        The command line: "[--section=<name>]".
        Returns:
            ToolArgumentParser: The parser.
        """
        parser = ToolArgumentParser("sysinfo", cls.VERSION, "Report on the machine running the tools.")
        parser.add_argument("--section", help=f"One of: all (default), {', '.join(cls.SECTIONS)}")
        return parser

    def report(self, section: Optional[str] = None) -> str:
        """
        Build the report.
        Args:
            section: One section name, or None (or "all") for every section.
        Returns:
            str: A heading per section, then its "key: value" lines.
        Raises:
            ValueError: If the section is unknown.
        """
        if section and section != "all" and section not in self.SECTIONS:
            raise ValueError(f"Unknown section '{section}'. Use one of: all, {', '.join(self.SECTIONS)}.")
        chosen = self.SECTIONS if not section or section == "all" else (section,)
        blocks = []
        for name in chosen:
            try:
                rows = getattr(self, name)()
            except Exception as section_error:  # One unreadable section should not hide the others
                rows = [("error", str(section_error))]
            width = max(len(key) for key, _ in rows)
            blocks.append(f"[{name}]\n" + "\n".join(f"{key:<{width}}  {value}" for key, value in rows))
        return "\n\n".join(blocks)


def main(argv: Optional[list[str]] = None) -> int:
    """
    Parse the command line and print the report, or "Error: <reason>".
    Args:
        argv: Arguments; None reads sys.argv.
    Returns:
        int: 0 on success, 1 for an unknown section or argument.
    """
    try:
        args = SystemReport.build_parser().parse_args(argv)
        print(SystemReport().report(args.section))
    except ValueError as e:
        print(f"Error: {e}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
