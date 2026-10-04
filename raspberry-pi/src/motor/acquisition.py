from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AcquisitionEvidence:
    """Monotone possible request count and sticky proof of this instance's addressing.

    A possible request is not an acknowledgement or proof of delivery. Local
    connection success does not count and cannot establish address proof.
    """

    possible_frames: int
    address_proven: bool
