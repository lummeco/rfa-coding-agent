"""The resource gate: probes parsed from real command output, decisions taken on the result."""

import pytest

from rfa import gates, tasks
from rfa.gates import Memory, Power

BATTERY_LINE = " -InternalBattery-0 (id=22872163)\t{}%; {} present: true\n"
PMSET_AC = "Now drawing from 'AC Power'\n" + BATTERY_LINE.format(100, "charged; 0:00 remaining")
PMSET_BATTERY = "Now drawing from 'Battery Power'\n" + BATTERY_LINE.format(58, "discharging; 1:52 remaining")
PMSET_DESKTOP = "Now drawing from 'AC Power'\n"
PMSET_SETTINGS = (
    "System-wide power settings:\nCurrently in use:\n standby              1\n"
    " lowpowermode         1\n hibernatefile        /var/vm/sleepimage\n"
)
MEMORY_PRESSURE = (
    "The system has 137438953472 (8388608 pages with a page size of 16384).\n"
    "Pages free: 5231939\n\nSystem-wide memory free percentage: 95%\n"
)


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (PMSET_AC, (True, 100)),
        (PMSET_BATTERY, (False, 58)),
        (PMSET_DESKTOP, (True, None)),  # no battery line: a Mac that can never be on battery
        ("", (True, None)),  # pmset missing or hung: nothing to hold a run back with
    ],
)
def test_pmset_says_where_the_power_comes_from(output, expected):
    assert gates.parse_power(output) == expected


@pytest.mark.parametrize(
    ("line", "low"),
    [
        (" lowpowermode         1", True),
        (" lowpowermode         0", False),
        (" powermode            1", True),  # newer macOS renamed it
        (" powermode            2", False),  # ...where 2 is High Power, not Low
        ("", False),
    ],
)
def test_low_power_mode_is_read_under_either_name(line, low):
    assert gates.parse_low_power(PMSET_SETTINGS.replace(" lowpowermode         1", line)) is low


def test_memory_pressure_gives_both_a_percentage_and_the_gigabytes_behind_it():
    assert gates.parse_free_memory(MEMORY_PRESSURE) == (95, 121.6)
    assert gates.parse_free_memory("nothing we know") == (None, None)


@pytest.mark.parametrize(("output", "level"), [("1\n", "normal"), ("2", "warn"), ("4", "critical"), ("", "unknown")])
def test_the_pressure_level_is_a_word_not_a_number(output, level):
    assert gates.parse_pressure(output) == level


@pytest.mark.parametrize(
    ("state", "ok"),
    [
        (Power(on_ac=True, charge=None, low_power=False), True),
        (Power(on_ac=True, charge=5, low_power=True), True),  # plugged in: neither one matters
        (Power(on_ac=False, charge=80, low_power=False), True),
        (Power(on_ac=False, charge=40, low_power=False), False),
        (Power(on_ac=False, charge=80, low_power=True), False),  # Low Power Mode at any charge
    ],
)
def test_the_charge_only_decides_once_the_charger_is_out(state, ok):
    assert gates.power(state, 50, require_power=False).ok is ok


def test_require_power_ignores_a_full_battery_and_says_why():
    assert gates.power(Power(on_ac=True, charge=100, low_power=False), 50, require_power=True).ok is True
    blocked = gates.power(Power(on_ac=False, charge=100, low_power=False), 50, require_power=True)
    assert (blocked.ok, "wall power" in blocked.detail) == (False, True)


@pytest.mark.parametrize(
    ("state", "ok"),
    [
        (Memory(pressure="normal", free_pct=95, free_gb=121.6), True),
        (Memory(pressure="normal", free_pct=12, free_gb=15.0), False),
        (Memory(pressure="warn", free_pct=95, free_gb=121.6), False),  # pressure outranks the number
        (Memory(pressure="unknown", free_pct=None, free_gb=None), True),  # no reading is not a reason to stop
    ],
)
def test_memory_pressure_outranks_the_free_percentage(state, ok):
    assert gates.memory(state, 30).ok is ok


def test_the_slot_names_whatever_is_holding_it(tmp_path, monkeypatch):
    monkeypatch.setenv("RFA_HOME", str(tmp_path))
    tasks.init()
    assert gates.slot([]) == gates.Gate("slot", True, "free")
    task = tasks.move(tasks.create("rebuild the importer"), "todo", actor="human")
    assert gates.slot([task]) == gates.Gate("slot", False, f"{task.id} is running")
