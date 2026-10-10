"""READ-ONLY latency measurement of the drive link. Writes NOTHING.

    python scripts/bench_comm_latency.py                          # MOTOR_* from .env
    python scripts/bench_comm_latency.py --port ftdi://schneider:rs485/1 --slave 248
    python scripts/bench_comm_latency.py -n 500 --timeout 0.5

Opens the port through the project's own transport (``src.motor.atv320.
serial_master`` via ``drive_link.py``, i.e. the buffered FTDI port for an
``ftdi://`` URL), reads ETA N times (default 200) through the application's
driver, and prints min / median / p95 / max latency and the error count.

What it is for: pyftdi's stock serial port reports ``in_waiting == 0`` for
ever, so pymodbus waited out the whole timeout on every read - 2.0 s per
register, 200/200 correct in 401 s on the bench. With the buffered port a read
should cost its wire time plus a USB latency period, ~20-30 ms. This script is
how that claim gets measured on the real cable instead of assumed.

Why it is safe to run with the drive powered: every transaction is a Modbus
function-3 READ of ETA (3201). No command word, no setpoint, no parameter is
written, and ``open()`` itself only reads ETA. The port is released with the
driver's plain port close, NOT ``ATV320Drive.close()``: that one is a stop
sequence and writes LFRD/CMD, which a read-only tool has no business doing.

A failed read is counted, never retried and never timed into the latency
statistics (its duration is the timeout, which says nothing about the link's
speed). If the driver latches the link down after consecutive failures, the
script reopens it and says so, so one burst of noise does not end the run.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
from collections import Counter
from dataclasses import dataclass

from drive_link import add_link_arguments, arg_int, build_drive, describe_error, link_from_args

from src.clock import RealClock
from src.motor.atv320 import ATV320Drive, transaction_worst_case
from src.motor.drive import decode_status_word
from src.result import Err, Ok
from src.units import RegisterAddress, Seconds, StatusWord, elapsed

DEFAULT_READS = 200


@dataclass(frozen=True, slots=True)
class LatencyReport:
    """What one run measured. Latencies are successful reads only, in seconds."""

    latencies: tuple[Seconds, ...]
    errors: Counter[str]
    reopens: int
    wall: Seconds

    @property
    def failed(self) -> int:
        return sum(self.errors.values())


def percentile(sorted_values: tuple[Seconds, ...], fraction: float) -> Seconds:
    """Nearest-rank percentile. ``sorted_values`` must be non-empty and sorted."""
    rank = max(1, min(len(sorted_values), round(fraction * len(sorted_values) + 0.5)))
    return sorted_values[rank - 1]


def _ms(value: Seconds) -> str:
    return f"{value * 1000.0:7.1f} ms"


def print_report(report: LatencyReport, reads: int) -> None:
    ok = len(report.latencies)
    print(f"\n{ok}/{reads} lectures OK, {report.failed} erreurs, {report.reopens} reouvertures")
    print(f"duree totale {report.wall:.2f} s")
    if ok:
        ordered = tuple(sorted(report.latencies))
        print(f"  min     {_ms(ordered[0])}")
        print(f"  mediane {_ms(Seconds(statistics.median(ordered)))}")
        print(f"  p95     {_ms(percentile(ordered, 0.95))}")
        print(f"  max     {_ms(ordered[-1])}")
    for reason, count in report.errors.most_common():
        print(f"  x{count:<4} {reason}")


async def measure(
    drive: ATV320Drive, eta: RegisterAddress, reads: int, clock: RealClock
) -> LatencyReport:
    """``reads`` timed reads of ``eta``. Each failure is counted, never retried."""
    latencies: list[Seconds] = []
    errors: Counter[str] = Counter()
    reopens = 0
    run_started = clock.monotonic()
    for index in range(reads):
        if drive.link_lost:
            reopens += 1
            reopened = await drive.open()
            if isinstance(reopened, Err):
                errors[f"reouverture: {describe_error(reopened.error)}"] += 1
                continue
        started = clock.monotonic()
        outcome = await drive.read_register(eta)
        took = elapsed(started, clock.monotonic())
        match outcome:
            case Ok(value):
                latencies.append(took)
                if index == 0:
                    state = decode_status_word(StatusWord(value)).name
                    print(f"premiere lecture: ETA=0x{value:04X} -> {state}")
            case Err(error):
                # Group by variant and message without the measured duration,
                # so 30 timeouts show up as one line, not 30.
                errors[describe_error(error).split(" (apres")[0]] += 1
    return LatencyReport(
        latencies=tuple(latencies),
        errors=errors,
        reopens=reopens,
        wall=elapsed(run_started, clock.monotonic()),
    )


async def run(args: argparse.Namespace, reads: int) -> int:
    link = link_from_args(args)
    clock = RealClock()
    built = build_drive(link, clock)
    drive = built.drive
    print(f"lien: {link.describe()}")
    print(
        f"pire cas theorique par transaction: {transaction_worst_case(link.settings):.3f} s "
        "(borne, pas une mesure)"
    )
    print("LECTURE SEULE: aucune ecriture ne sera envoyee au variateur.\n")

    opened = await drive.open()
    if isinstance(opened, Err):
        print(f"ECHEC a l'ouverture: {describe_error(opened.error)}")
        print("Port occupe (SoMove ?), libusb absent, mauvaise adresse, variateur hors tension ?")
        built.release_port()
        return 3
    try:
        report = await measure(drive, link.registers.eta, reads, clock)
    finally:
        built.release_port()
    print_report(report, reads)
    return 0 if report.failed == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Latence du lien Modbus ATV320 (lecture seule)")
    add_link_arguments(parser)
    parser.add_argument("-n", "--reads", default=str(DEFAULT_READS), help="nombre de lectures")
    args = parser.parse_args()
    try:
        reads = arg_int(args, "reads")
        if reads < 1:
            parser.error("--reads doit etre >= 1")
        return asyncio.run(run(args, reads))
    except ValueError as exc:
        print(f"configuration refusee: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
