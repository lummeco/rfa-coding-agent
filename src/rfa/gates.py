"""Whether this Mac should start a run right now.

The daemon runs on a laptop that someone is also using, so a run has to earn its place: not while
the battery is draining away, not when memory is already tight, and never two at once. Each gate
answers with a sentence rather than a flag, because the board, `rfa status` and the menu bar show
the reason, and "no" without a reason is indistinguishable from broken.

Nothing here is remembered. A gate that closed because you pulled the charger opens again when you
plug it back in, on the next tick.

The probes are pure functions over command output, so the decisions are tested on real fixtures and
only `sh()` ever touches the machine.
"""

import os
import re
import subprocess
from dataclasses import dataclass

PRESSURE = {1: "normal", 2: "warn", 4: "critical"}


@dataclass
class Gate:
    name: str
    ok: bool
    detail: str


def sh(*args: str, timeout: float = 10) -> str:
    """A probe that cannot fail: a missing command or a hung one reads as no information."""
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def parse_power(text: str) -> tuple[bool, int | None]:
    """`pmset -g batt` -> (on wall power, charge). A Mac with no battery is always on wall power."""
    match = re.search(r"(\d{1,3})%;", text)
    return "'AC Power'" in text or match is None, int(match.group(1)) if match else None


def parse_low_power(text: str) -> bool:
    """`pmset -g`: ` lowpowermode 1`, or ` powermode 1` on newer macOS, where 2 is High Power."""
    match = re.search(r"^\s*(?:lowpowermode|powermode)\s+(\d)", text, re.MULTILINE)
    return bool(match) and match.group(1) == "1"


def parse_free_memory(text: str) -> tuple[int | None, float | None]:
    """`memory_pressure` -> (percent free, gigabytes free). None when it said nothing we understand."""
    free = re.search(r"System-wide memory free percentage:\s*(\d+)%", text)
    total = re.search(r"The system has (\d+)", text)
    if not free:
        return None, None
    return int(free.group(1)), round(int(total.group(1)) * int(free.group(1)) / 100 / 2**30, 1) if total else None


def parse_pressure(text: str) -> str:
    """`sysctl -n kern.memorystatus_vm_pressure_level`: 1 normal, 2 warn, 4 critical."""
    return PRESSURE.get(int(text.strip()), "unknown") if text.strip().isdigit() else "unknown"


@dataclass
class Power:
    on_ac: bool
    charge: int | None
    low_power: bool


@dataclass
class Memory:
    pressure: str
    free_pct: int | None
    free_gb: float | None


def read_power() -> Power:
    return Power(*parse_power(sh("pmset", "-g", "batt")), parse_low_power(sh("pmset", "-g")))


def read_memory() -> Memory:
    level = parse_pressure(sh("sysctl", "-n", "kern.memorystatus_vm_pressure_level"))
    return Memory(level, *parse_free_memory(sh("memory_pressure")))


def power(state: Power, min_battery: int, require_power: bool) -> Gate:
    """Wall power, or enough battery that a run is not what empties it."""
    if state.on_ac:
        return Gate("power", True, "on wall power")
    if require_power:
        return Gate("power", False, f"on battery ({state.charge}%); runs are set to need wall power")
    if state.low_power:
        return Gate("power", False, f"on battery ({state.charge}%) in Low Power Mode")
    if state.charge is not None and state.charge < min_battery:
        return Gate("power", False, f"battery {state.charge}%, below {min_battery}%")
    return Gate("power", True, f"battery {state.charge}%")


def memory(state: Memory, min_free_pct: int) -> Gate:
    """Room for a container and a local model before macOS starts taking pages off something else."""
    if state.pressure in ("warn", "critical"):
        return Gate("memory", False, f"memory pressure {state.pressure}")
    if state.free_pct is None:
        return Gate("memory", True, "unknown, assuming free")
    detail = f"{state.free_pct}% free" + (f" ({state.free_gb} GB)" if state.free_gb else "")
    if state.free_pct < min_free_pct:
        return Gate("memory", False, f"{detail}, below {min_free_pct}%")
    return Gate("memory", True, detail)


def slot(running: list) -> Gate:
    """One run at a time, whoever started it -- an `rfa work` you typed yourself holds the slot too.

    Planning and coding both drive the same local model: two at once halve its memory and thrash the
    GPU, and finish later than the same two in a row.
    """
    return Gate("slot", not running, f"{running[0].id} is running" if running else "free")


def check(config, running: list) -> list[Gate]:
    """Every gate, in the order that decides fastest. `config` is a daemon.DaemonConfig."""
    return [
        slot(running),
        power(read_power(), config.battery_min, config.require_power),
        memory(read_memory(), config.memory_min_pct),
    ]


class KeepAwake:
    """`caffeinate -i -w <pid>`: the Mac does not idle-sleep out from under a run.

    Held for the run, not for the daemon: a daemon that never lets the machine sleep is worse than
    one that occasionally starts late.
    """

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self.process: subprocess.Popen | None = None

    def __enter__(self) -> "KeepAwake":
        if self.enabled:
            try:
                self.process = subprocess.Popen(
                    ["caffeinate", "-i", "-w", str(os.getpid())],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except OSError:
                self.process = None
        return self

    def __exit__(self, *exc) -> None:
        if self.process is not None:
            self.process.terminate()
