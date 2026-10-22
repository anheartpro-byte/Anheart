from src.motor.sto import SimulatedStoChain, SimulatedStoSource, StoSourceKind


def ready_sto() -> SimulatedStoSource:
    return SimulatedStoSource(chain=SimulatedStoChain(), source=StoSourceKind.SIM_READY)
