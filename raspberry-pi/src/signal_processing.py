"""On-device biosignal treatment for the Raspberry Pi.

Raw BITalino ADC data is filtered, converted to physical units, downsampled and
turned into clinical metrics HERE, so the server only ever receives clean,
display-ready data (and lower volume). This mirrors the BITalino lab-guide
pipeline, which uses the BioSPPy library.

Per channel we produce:
  * a treated waveform (causal IIR filter, streaming/stateful, decimated to
    ``fs_out`` Hz) in physical units where a transfer function is defined, and
  * metrics computed by BioSPPy over a rolling window (heart rate, respiration
    rate, EDA responses, EMG activations, PPG pulse).

The filter is a streaming causal Butterworth (scipy ``lfilter`` with carried
state) rather than BioSPPy's zero-phase ``filtfilt``: it produces an identical
pass-band with no per-batch edge artifacts and no windowing bookkeeping, which is
what a live monitor needs. R-peak/metric extraction still uses BioSPPy at the full
input rate, so the reported numbers match the reference implementation.
"""

from __future__ import annotations

import logging
import math
from collections import deque
from dataclasses import dataclass

import numpy as np
from scipy import signal as sps

logger = logging.getLogger(__name__)

# BioSPPy is the reference treatment used by the BITalino lab guides. It is only
# needed for metric extraction; import lazily so the waveform path still works if
# it is unavailable (e.g. a dev machine without the native deps).
try:
    from biosppy.signals import bvp as bio_bvp
    from biosppy.signals import ecg as bio_ecg
    from biosppy.signals import eda as bio_eda
    from biosppy.signals import emg as bio_emg
    from biosppy.signals import resp as bio_resp

    BIOSPPY_AVAILABLE = True
except Exception as e:
    logger.warning("BioSPPy unavailable, metrics disabled: %s", e)
    BIOSPPY_AVAILABLE = False


# --- BITalino transfer-function constants ---------------------------------
VCC = 3.3  # operating voltage (V)
ADC_BITS = 10  # A1-A4 resolution
ADC_LEVELS = 2**ADC_BITS
ECG_GAIN = 1100  # ECG sensor gain
EMG_GAIN = 1009  # EMG sensor gain
# mV per ADC count for a DC-removed (band-passed) signal: the -0.5 offset cancels.
ECG_MV_PER_COUNT = (VCC / ADC_LEVELS / ECG_GAIN) * 1000.0
EMG_MV_PER_COUNT = (VCC / ADC_LEVELS / EMG_GAIN) * 1000.0


@dataclass
class ChannelSpec:
    """Per-channel treatment configuration."""

    filt_band: str  # 'bandpass' | 'lowpass'
    filt_cutoff: object  # float or [low, high] in Hz
    unit: str  # physical unit of the treated waveform
    mv_per_count: float | None = None  # linear ADC->unit scale (DC-removed)
    metric: str | None = None  # biosppy extractor key
    order: int = 2


# Channel treatments. Cutoffs stay below the 250 Hz output Nyquist (125 Hz) so
# decimation never aliases. ECG/EMG are band-passed (DC removed) and scaled to mV.
SENSOR_SPECS: dict[str, ChannelSpec] = {
    "ECG": ChannelSpec("bandpass", [3.0, 45.0], "mV", ECG_MV_PER_COUNT, "ecg"),
    "EMG": ChannelSpec("bandpass", [10.0, 120.0], "mV", EMG_MV_PER_COUNT, "emg"),
    "EDA": ChannelSpec("lowpass", 5.0, "raw", None, "eda"),
    "RESP": ChannelSpec("bandpass", [0.1, 0.35], "raw", None, "resp"),
    "SpO2": ChannelSpec("bandpass", [0.5, 8.0], "raw", None, "bvp"),
    "LUX": ChannelSpec("lowpass", 5.0, "raw", None, None),
}
DEFAULT_SPEC = ChannelSpec("bandpass", [3.0, 45.0], "raw", None, None)

# How many ECG windows in a row the quality grade must judge before a stretch
# of windows it could not judge is reported as over. The grade runs once per
# batch (five a second in the console), so 25 windows are five seconds. It is
# also what bounds the log: one line when a stretch begins and one when it is
# over, so never more than two lines per 26 windows, whatever the windows are.
UNGRADED_CLEAR_WINDOWS = 25


class ChannelProcessor:
    """Stateful streaming treatment for a single channel."""

    def __init__(
        self, channel: str, fs_in: int = 1000, fs_out: int = 250, metric_window_s: float = 8.0
    ):
        self.channel = channel
        self.spec = SENSOR_SPECS.get(channel, DEFAULT_SPEC)
        self.fs_in = fs_in
        self.fs_out = max(1, fs_out)
        self.decimation = max(1, fs_in // self.fs_out)

        # Design a causal Butterworth filter and initialise its state.
        nyq = fs_in / 2.0
        if self.spec.filt_band == "bandpass":
            wn = [c / nyq for c in self.spec.filt_cutoff]
        else:
            wn = self.spec.filt_cutoff / nyq
        self._b, self._a = sps.butter(self.spec.order, wn, btype=self.spec.filt_band)
        self._zi = sps.lfilter_zi(self._b, self._a) * 0.0
        self._primed = False

        # Decimation phase carried across batches so output is gap-free.
        self._phase = 0

        # Rolling raw window (for BioSPPy metrics) and metric throttle.
        self._window: deque[float] = deque(maxlen=int(metric_window_s * fs_in))
        self._last_metrics: dict = {}
        # Freshness counter: advances ONLY when _compute_metrics produced a new
        # result. process() re-emits the previous dict otherwise, so "the same
        # metrics again" must not read as "a new measurement" downstream. Read
        # through SignalTreatment.metric_seq(); the runtime ignores a repeat.
        self._metric_seq = 0

        # Bounded report of the ECG windows the quality grade could not judge
        # (see _not_graded and _judged): how many in the stretch being reported
        # (0: none open), and how many windows were judged in a row since the
        # last of them (read only while a stretch is open; every ungradable
        # window sets it back to 0).
        self._ungraded = 0
        self._judged_since = 0

    def process(self, raw: list[float]) -> tuple[list[float], dict]:
        """Treat a batch of raw ADC samples.

        Returns (treated_downsampled_values, metrics). The treated values are in
        ``self.spec.unit`` at ``self.fs_out`` Hz.
        """
        if not raw:
            return [], dict(self._last_metrics)

        x = np.asarray(raw, dtype=float)

        # Prime the filter state with the first sample to avoid a startup step.
        if not self._primed:
            self._zi = sps.lfilter_zi(self._b, self._a) * x[0]
            self._primed = True

        filtered, self._zi = sps.lfilter(self._b, self._a, x, zi=self._zi)

        # Convert to physical units (linear scale for DC-removed band-passed data).
        if self.spec.mv_per_count is not None:
            treated = filtered * self.spec.mv_per_count
        else:
            treated = filtered

        # Continuous decimation across batches using a running phase.
        idx = np.arange(len(treated))
        keep = (idx + self._phase) % self.decimation == 0
        self._phase = (self._phase + len(treated)) % self.decimation
        out = treated[keep].tolist()

        # Metrics from BioSPPy over the rolling raw window.
        self._window.extend(raw)
        metrics = self._compute_metrics()
        if metrics is not None:
            self._last_metrics = metrics
            self._metric_seq += 1

        return out, dict(self._last_metrics)

    @property
    def metric_seq(self) -> int:
        """How many fresh metric results this channel has produced. 0 = none yet."""
        return self._metric_seq

    def _compute_metrics(self) -> dict | None:
        key = self.spec.metric
        if not key or not BIOSPPY_AVAILABLE:
            return None
        # Need a few seconds of data for stable extraction.
        if len(self._window) < self.fs_in * 3:
            return None

        sig = np.asarray(self._window, dtype=float)
        try:
            if key == "ecg":
                quality = self._ecg_quality(sig)
                metrics: dict = {"quality": quality}
                # Only report a heart rate on a trustworthy signal, never a
                # fabricated number from hum, saturation or a flat lead.
                if quality == "good":
                    out = bio_ecg.ecg(signal=sig, sampling_rate=self.fs_in, show=False)
                    hr = out["heart_rate"]
                    rp = out["rpeaks"]
                    if len(hr):
                        metrics["heartRate"] = round(float(np.median(hr)))
                    if len(rp) >= 3:
                        rr = np.diff(rp) / self.fs_in * 1000.0
                        metrics["hrv"] = round(float(np.sqrt(np.mean(np.diff(rr) ** 2))))
                return metrics
            if key == "resp":
                out = bio_resp.resp(signal=sig, sampling_rate=self.fs_in, show=False)
                rr = out["resp_rate"]
                if len(rr):
                    return {"respRate": round(float(np.median(rr) * 60.0), 1)}
                return None
            if key == "eda":
                out = bio_eda.eda(signal=sig, sampling_rate=self.fs_in, show=False)
                return {"scrCount": len(out["peaks"])}
            if key == "emg":
                out = bio_emg.emg(signal=sig, sampling_rate=self.fs_in, show=False)
                return {"activations": len(out["onsets"])}
            if key == "bvp":
                out = bio_bvp.bvp(signal=sig, sampling_rate=self.fs_in, show=False)
                hr = out["heart_rate"]
                if len(hr):
                    return {"pulse": round(float(np.median(hr)))}
                return None
        except Exception as e:
            logger.debug("Metric extraction failed for %s: %s", self.channel, e)
            return None
        return None

    def _ecg_quality(self, raw: np.ndarray, mains_hz: float = 50.0) -> str:
        """Classify raw ECG so the UI can guide the operator.

        Returns one of no_signal / mains_dominated / noisy / good. This is the
        piece BioSPPy lacks, so we never present hum or a flat lead as a heartbeat.

        ``good`` is the only grade a heart rate is extracted under, so it is
        returned only when every test below could be run and was passed. A
        window that cannot be judged (not one-dimensional, shorter than a
        second, holding a NaN or an infinity, or whose mains test cannot be
        computed) is ``no_signal``: "I do not know" never reads as "fine".
        Every comparison against NaN is false, so a non-finite window is
        refused before the tests that compare.

        Each refusal goes through :meth:`_not_graded` and each judged window
        through :meth:`_judged`: the log gets the reason when a stretch of
        ungradable windows begins and their number once it is over, never a
        line per window.
        """
        if raw.ndim != 1:
            self._not_graded(f"{raw.ndim} dimensions where a signal has one")
            return "no_signal"
        if raw.size < self.fs_in:
            self._not_graded(f"{raw.size} samples are less than a second at {self.fs_in} Hz")
            return "no_signal"
        finite = int(np.count_nonzero(np.isfinite(raw)))
        if finite != raw.size:
            self._not_graded(f"{raw.size - finite} of its {raw.size} samples are not finite")
            return "no_signal"
        if np.std(raw) < 3 or (raw.max() - raw.min()) <= 5:
            self._judged()
            return "no_signal"
        clip = float(np.mean((raw <= 3) | (raw >= 1020)))
        if clip > 0.5:
            self._judged()
            return "noisy"
        removed = self._mains_share(raw, mains_hz)
        if removed is None:
            return "no_signal"
        self._judged()
        if removed > 0.6:
            return "mains_dominated"
        return "good"

    def _not_graded(self, reason: str) -> None:
        """Count a window the grade could not judge; write why when a stretch of them begins.

        One warning for the first window of a stretch, with its reason, and
        none for those that follow: a non-finite sample stays eight seconds in
        the window, and a line per window would be five lines a second on the
        Pi's SD card for as long as the cause lasts. :meth:`_judged` writes
        the line that closes the stretch. The reason never holds a sample
        value: counts, rates and scipy's own message only.
        """
        if self._ungraded == 0:
            logger.warning("ECG window not graded: %s", reason)
        self._ungraded += 1
        self._judged_since = 0

    def _judged(self) -> None:
        """Count a window the grade could judge; close a stretch of windows it could not.

        A stretch is over once :data:`UNGRADED_CLEAR_WINDOWS` windows in a row
        were judged: one warning then says how many were not. Counting them in
        a row is what bounds the log: an ungradable window now and then
        cannot write a line each.
        """
        if self._ungraded == 0:
            return
        self._judged_since += 1
        if self._judged_since >= UNGRADED_CLEAR_WINDOWS:
            logger.warning("ECG windows graded again after %d that could not be", self._ungraded)
            self._ungraded = 0

    def _mains_share(self, raw: np.ndarray, mains_hz: float) -> float | None:
        """Share of the DC-corrected variance that a notch at ``mains_hz`` removes.

        Above 0.6 the electrodes are picking up powerline hum instead of the
        heart. ``None``, with the reason handed to :meth:`_not_graded`, when
        the share cannot be established: a mains frequency no notch can be
        designed for at this sampling rate (NaN included), a filter scipy
        refuses, or a variance that is not a finite positive number (of the
        window, which would be divided by, or of what the notch gave back).
        The caller must not grade such a window ``good``.
        """
        if not 0 < mains_hz < self.fs_in * 0.45:
            self._not_graded(
                f"no mains notch at {mains_hz} Hz for a signal sampled at {self.fs_in} Hz"
            )
            return None
        hp = raw - float(np.mean(raw))
        try:
            b, a = sps.iirnotch(mains_hz, 30.0, self.fs_in)
            notched = sps.filtfilt(b, a, hp)
        except ValueError as e:
            # What scipy raises when it refuses a filter or a signal: a
            # frequency out of range, an unstable notch, a signal shorter than
            # the filter's padding.
            self._not_graded(f"the mains notch failed: {e}")
            return None
        vh = float(np.var(hp))
        vn = float(np.var(notched))
        if not (math.isfinite(vh) and math.isfinite(vn) and vh > 0):
            self._not_graded("its variance is not a finite positive number")
            return None
        return 1 - vn / vh


class SignalTreatment:
    """Manages per-channel processors for a session."""

    def __init__(self, fs_in: int = 1000, fs_out: int = 250):
        self.fs_in = fs_in
        self.fs_out = fs_out
        self._processors: dict[str, ChannelProcessor] = {}

    def metric_seq(self, channel: str) -> int:
        """Freshness counter of ``channel``'s metrics; 0 before any fresh result.

        Advances only when the metrics were recomputed, never when the previous
        dict is merely re-emitted. The typed boundary (src/ecg_pipeline.py)
        passes this to the runtime as the heart-rate sample's ``seq``.
        """
        proc = self._processors.get(channel)
        return 0 if proc is None else proc.metric_seq

    def treat_batch(self, samples: list[dict]) -> tuple[list[dict], dict]:
        """Treat a batch of raw per-channel samples.

        Args:
            samples: [{"channel": str, "values": list[float]}] of RAW ADC data.

        Returns:
            (treated_samples, metrics_by_channel) where treated_samples is
            [{"channel", "values" (treated @ fs_out), "unit"}] and
            metrics_by_channel is {channel: {metric: value}}.
        """
        treated: list[dict] = []
        metrics_by_channel: dict = {}
        for s in samples:
            channel = s["channel"]
            proc = self._processors.get(channel)
            if proc is None:
                proc = ChannelProcessor(channel, self.fs_in, self.fs_out)
                self._processors[channel] = proc
            values, metrics = proc.process(s.get("values", []))
            treated.append(
                {
                    "channel": channel,
                    "values": values,
                    "unit": proc.spec.unit,
                }
            )
            if metrics:
                metrics_by_channel[channel] = metrics
        return treated, metrics_by_channel
