import pytest

from lib.device_memory import MAX_PSI_SOME_AVG10, MEMORY_WAIT, MIN_AVAILABLE_BYTES, over_limit

GIB_KB = 1024 * 1024


def meminfo(available_kb: int | str) -> str:
    return (
        "MemTotal:       65536000 kB\n"
        "MemFree:         1234567 kB\n"
        f"MemAvailable:   {available_kb} kB\n"
        "Buffers:          123456 kB\n"
    )


def pressure(some_avg10: float | str, full_avg10: float | str = "0.00") -> str:
    return (
        f"some avg10={some_avg10} avg60=0.00 avg300=0.09 total=1896645106\n"
        f"full avg10={full_avg10} avg60=0.00 avg300=0.05 total=1771352482\n"
    )


AMPLE = meminfo(16 * GIB_KB)
LOW = meminfo(8 * GIB_KB - 1)
CALM = pressure("0.00")
STORMY = pressure("10.01")


def test_the_limits_are_8_gib_available_and_psi_some_avg10_of_10() -> None:
    assert MIN_AVAILABLE_BYTES == 8 * 1024**3
    assert MAX_PSI_SOME_AVG10 == 10.0


@pytest.mark.parametrize(
    ("memory", "psi", "held"),
    [
        (meminfo(8 * GIB_KB - 1), CALM, True),
        (meminfo(8 * GIB_KB), CALM, False),
        (AMPLE, pressure("10.0"), False),
        (AMPLE, pressure("10.00"), False),
        (AMPLE, pressure("10.01"), True),
        (AMPLE, pressure("0.00", full_avg10="99.00"), False),
        (AMPLE, CALM, False),
    ],
)
def test_each_limit_holds_only_past_its_boundary(memory: str, psi: str, held: bool) -> None:
    assert (over_limit(memory, psi) is not None) is held


@pytest.mark.parametrize("psi", [None, "", "some avg10=abc avg60=0.00", "full avg10=50.00\n", "garbage"])
def test_a_missing_or_unparseable_pressure_file_never_cancels_low_memavailable(
    psi: str | None,
) -> None:
    reason = over_limit(LOW, psi)
    assert reason is not None and "MemAvailable" in reason


UNREADABLE_MEMINFO = [
    None,
    "",
    "MemAvailable: lots kB\n",
    "MemAvailable: -5 kB\n",
    "MemAvailable: \u00b2 kB\n",
    f"MemAvailable: {'1' * 4301} kB\n",
    "MemTotal: 65536000 kB\nMemFree: 1 kB\n",
    "garbage",
]


@pytest.mark.parametrize("memory", UNREADABLE_MEMINFO)
def test_a_missing_or_unparseable_meminfo_passes_on_its_own(memory: str | None) -> None:
    assert over_limit(memory, CALM) is None


@pytest.mark.parametrize("memory", UNREADABLE_MEMINFO)
def test_high_psi_holds_even_when_meminfo_is_unreadable(memory: str | None) -> None:
    reason = over_limit(memory, STORMY)
    assert reason is not None and "PSI" in reason


def test_both_files_missing_passes() -> None:
    assert over_limit(None, None) is None


def test_both_over_gives_the_memavailable_reason() -> None:
    reason = over_limit(LOW, STORMY)
    assert reason is not None and "MemAvailable" in reason and "PSI" not in reason


@pytest.mark.parametrize(("memory", "psi"), [(LOW, CALM), (AMPLE, STORMY), (LOW, STORMY), (LOW, None)])
def test_every_reason_starts_with_memory_wait(memory: str, psi: str | None) -> None:
    reason = over_limit(memory, psi)
    assert reason is not None and reason.startswith(MEMORY_WAIT)
    assert MEMORY_WAIT == "memory-wait"
