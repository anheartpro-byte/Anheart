/**
 * Shared ECG signal processing utilities (display/analysis only).
 *
 * The raw ADC samples stored in Convex are never modified, these helpers filter
 * a copy purely for visualization and heart-rate estimation.
 *
 * The treatment mirrors the BITalino lab guide, which processes ECG with the
 * BioSPPy library (`ecg.ecg(signal, sampling_rate)`):
 *   1. band-pass FIR filter [3, 45] Hz (zero-phase),
 *   2. Hamilton R-peak segmenter + peak correction,
 *   3. heart rate from R-R intervals.
 * On top of that we add a signal-quality gate (no_signal / mains_dominated) that
 * BioSPPy lacks, so we never report a fabricated heart rate on hum or a flat lead.
 *
 * References: Hamilton, "Open Source ECG Analysis", 2002; BioSPPy signals.ecg.
 */

export type SignalQuality = "no_signal" | "mains_dominated" | "noisy" | "good";

export const DEFAULT_MAINS_HZ = 50;

// ============================================
// BITalino ECG transfer function (raw ADC -> mV)
// ============================================

export interface EcgTransfer {
  /** Operating voltage (V). BITalino = 3.3 V. */
  vcc?: number;
  /** ECG sensor gain. BITalino ECG = 1100. */
  gain?: number;
  /** ADC resolution in bits. A1-A4 = 10-bit, A5-A6 = 6-bit. */
  nbits?: number;
}

/**
 * Convert a raw BITalino ECG ADC value to millivolts using the sensor datasheet
 * transfer function: ECG(mV) = ((ADC/2^n - 0.5) * VCC / G_ECG) * 1000.
 * Baseline (ADC ~ 2^n/2) maps to 0 mV; full scale is about +/-1.5 mV.
 */
export function adcToMillivolts(adc: number, t: EcgTransfer = {}): number {
  const vcc = t.vcc ?? 3.3;
  const gain = t.gain ?? 1100;
  const nbits = t.nbits ?? 10;
  const levels = 2 ** nbits;
  return ((adc / levels - 0.5) * vcc) / gain * 1000;
}

// ============================================
// Basic stats
// ============================================

function mean(x: number[]): number {
  if (x.length === 0) return 0;
  let s = 0;
  for (const v of x) s += v;
  return s / x.length;
}

function variance(x: number[]): number {
  if (x.length === 0) return 0;
  const m = mean(x);
  let s = 0;
  for (const v of x) s += (v - m) * (v - m);
  return s / x.length;
}

function std(x: number[]): number {
  return Math.sqrt(variance(x));
}

function median(x: number[]): number {
  if (x.length === 0) return 0;
  const s = x.slice().sort((a, b) => a - b);
  const mid = s.length >> 1;
  return s.length % 2 ? s[mid] : (s[mid - 1] + s[mid]) / 2;
}

/** RMSSD heart-rate variability (ms) from RR intervals; null if fewer than 2. */
function rmssd(rr: number[]): number | null {
  if (rr.length < 2) return null;
  let sumSq = 0;
  for (let i = 1; i < rr.length; i++) {
    const d = rr[i] - rr[i - 1];
    sumSq += d * d;
  }
  return Math.round(Math.sqrt(sumSq / (rr.length - 1)));
}

// ============================================
// Biquad IIR filters (RBJ cookbook), used for the Hamilton internal Butterworth
// stages and the mains-hum quality probe.
// ============================================

interface Biquad {
  b0: number;
  b1: number;
  b2: number;
  a1: number;
  a2: number;
}

// Butterworth section Q values for a 4th-order response (two cascaded biquads).
const BUTTER4_Q = [0.5411961, 1.3065630];

function lowpassCoeffs(f0: number, fs: number, q: number): Biquad {
  const w0 = (2 * Math.PI * f0) / fs;
  const cos = Math.cos(w0);
  const alpha = Math.sin(w0) / (2 * q);
  const a0 = 1 + alpha;
  return {
    b0: ((1 - cos) / 2) / a0,
    b1: (1 - cos) / a0,
    b2: ((1 - cos) / 2) / a0,
    a1: (-2 * cos) / a0,
    a2: (1 - alpha) / a0,
  };
}

function highpassCoeffs(f0: number, fs: number, q: number): Biquad {
  const w0 = (2 * Math.PI * f0) / fs;
  const cos = Math.cos(w0);
  const alpha = Math.sin(w0) / (2 * q);
  const a0 = 1 + alpha;
  return {
    b0: ((1 + cos) / 2) / a0,
    b1: (-(1 + cos)) / a0,
    b2: ((1 + cos) / 2) / a0,
    a1: (-2 * cos) / a0,
    a2: (1 - alpha) / a0,
  };
}

function notchCoeffs(f0: number, fs: number, q = 30): Biquad {
  const w0 = (2 * Math.PI * f0) / fs;
  const cos = Math.cos(w0);
  const alpha = Math.sin(w0) / (2 * q);
  const a0 = 1 + alpha;
  return {
    b0: 1 / a0,
    b1: (-2 * cos) / a0,
    b2: 1 / a0,
    a1: (-2 * cos) / a0,
    a2: (1 - alpha) / a0,
  };
}

function applyBiquad(c: Biquad, x: number[]): number[] {
  const y = new Array<number>(x.length);
  let x1 = 0, x2 = 0, y1 = 0, y2 = 0;
  for (let i = 0; i < x.length; i++) {
    const xi = x[i];
    const yi = c.b0 * xi + c.b1 * x1 + c.b2 * x2 - c.a1 * y1 - c.a2 * y2;
    x2 = x1; x1 = xi; y2 = y1; y1 = yi;
    y[i] = yi;
  }
  return y;
}

/** Zero-phase single-biquad filtering (forward + backward). */
function filtfilt(c: Biquad, x: number[]): number[] {
  if (x.length === 0) return [];
  const fwd = applyBiquad(c, x);
  const back = applyBiquad(c, fwd.slice().reverse());
  return back.reverse();
}

/** Zero-phase 4th-order Butterworth low-pass (two cascaded sections). */
function butterLowpass4(x: number[], f0: number, fs: number): number[] {
  let out = x;
  for (const q of BUTTER4_Q) out = filtfilt(lowpassCoeffs(f0, fs, q), out);
  return out;
}

/** Zero-phase 4th-order Butterworth high-pass (two cascaded sections). */
function butterHighpass4(x: number[], f0: number, fs: number): number[] {
  let out = x;
  for (const q of BUTTER4_Q) out = filtfilt(highpassCoeffs(f0, fs, q), out);
  return out;
}

// ============================================
// FIR band-pass (matches BioSPPy: firwin, order 0.3*fs, [3,45] Hz, filtfilt)
// ============================================

/** Design a windowed-sinc (Hamming) band-pass FIR, unity gain at band centre. */
function firwinBandpass(numtaps: number, f1: number, f2: number, fs: number): number[] {
  if (numtaps % 2 === 0) numtaps += 1; // symmetric, odd length
  const nyq = fs / 2;
  const w1 = f1 / nyq; // normalized (Nyquist = 1)
  const w2 = f2 / nyq;
  const M = numtaps - 1;
  const taps = new Array<number>(numtaps);
  for (let n = 0; n < numtaps; n++) {
    const m = n - M / 2;
    const ideal =
      m === 0 ? w2 - w1 : (Math.sin(Math.PI * w2 * m) - Math.sin(Math.PI * w1 * m)) / (Math.PI * m);
    const hamming = 0.54 - 0.46 * Math.cos((2 * Math.PI * n) / M);
    taps[n] = ideal * hamming;
  }
  // Normalize to unity gain at the passband centre frequency.
  const wc = (Math.PI * (w1 + w2)) / 2;
  let re = 0, im = 0;
  for (let n = 0; n < numtaps; n++) {
    re += taps[n] * Math.cos(wc * (n - M / 2));
    im += taps[n] * Math.sin(wc * (n - M / 2));
  }
  const g = Math.hypot(re, im) || 1;
  for (let n = 0; n < numtaps; n++) taps[n] /= g;
  return taps;
}

/**
 * Apply a symmetric FIR with 'same' alignment. Because the FIR is linear-phase,
 * centering the output removes the group delay -> zero-phase result.
 */
function firSame(x: number[], taps: number[]): number[] {
  const n = x.length;
  const m = taps.length;
  const half = (m - 1) >> 1;
  const out = new Array<number>(n).fill(0);
  for (let i = 0; i < n; i++) {
    let acc = 0;
    for (let k = 0; k < m; k++) {
      const j = i + half - k;
      if (j >= 0 && j < n) acc += taps[k] * x[j];
    }
    out[i] = acc;
  }
  return out;
}

// ============================================
// Display filter
// ============================================

function canNotch(mainsHz: number, fs: number): boolean {
  return mainsHz > 0 && mainsHz < fs * 0.45;
}

/**
 * BioSPPy-style band-pass [3, 45] Hz (FIR, order 0.3*fs) applied to a copy of the
 * raw samples. Removes DC baseline, wander, mains hum and HF noise, leaving the
 * QRS complex. Returns a zero-mean signal for charts and R-peak detection. The
 * input array is never mutated.
 */
export function filterEcg(
  values: number[],
  sampleRate: number,
  // eslint-disable-next-line @typescript-eslint/no-unused-vars -- kept for API compatibility
  mainsHz: number = DEFAULT_MAINS_HZ,
): number[] {
  if (values.length < 16 || sampleRate <= 0) return values.slice();
  const numtaps = Math.max(15, Math.floor(0.3 * sampleRate));
  const taps = firwinBandpass(numtaps, 3, 45, sampleRate);
  return firSame(values.map((v) => v), taps);
}

// ============================================
// Hamilton R-peak segmenter (port of BioSPPy ecg.hamilton_segmenter)
// ============================================

/** Local maxima ('max') or minima ('min') indices, via sign changes of the slope. */
function findExtrema(signal: number[], mode: "max" | "min"): number[] {
  const idx: number[] = [];
  for (let i = 1; i < signal.length - 1; i++) {
    if (mode === "max" && signal[i] > signal[i - 1] && signal[i] >= signal[i + 1]) idx.push(i);
    if (mode === "min" && signal[i] < signal[i - 1] && signal[i] <= signal[i + 1]) idx.push(i);
  }
  return idx;
}

/** Normalized Hamming-window smoother with edge reflection ('same' length). */
function hammingSmoother(x: number[], size: number): number[] {
  if (size < 2 || x.length === 0) return x.slice();
  const win = new Array<number>(size);
  let wsum = 0;
  for (let n = 0; n < size; n++) {
    win[n] = 0.54 - 0.46 * Math.cos((2 * Math.PI * n) / (size - 1));
    wsum += win[n];
  }
  for (let n = 0; n < size; n++) win[n] /= wsum;

  const pad = size;
  const left: number[] = [];
  for (let i = pad; i >= 1; i--) left.push(x[Math.min(i, x.length - 1)]);
  const right: number[] = [];
  for (let i = 2; i <= pad + 1; i++) right.push(x[Math.max(0, x.length - i)]);
  const aux = [...left, ...x, ...right];
  const conv = firSame(aux, win);
  return conv.slice(pad, pad + x.length);
}

function diff(x: number[]): number[] {
  const d = new Array<number>(Math.max(0, x.length - 1));
  for (let i = 1; i < x.length; i++) d[i - 1] = x[i] - x[i - 1];
  return d;
}

/**
 * Port of BioSPPy's hamilton_segmenter. `signal` is the [3,45]-band-passed ECG.
 * Returns candidate beat indices (before correct_rpeaks refinement).
 */
function hamiltonSegmenter(signal: number[], fs: number): number[] {
  const length = signal.length;
  const dur = length / fs;
  const v1s = Math.floor(1.0 * fs);
  const v100ms = Math.floor(0.1 * fs);
  const TH_elapsed = Math.ceil(0.36 * fs);
  const sm_size = Math.floor(0.08 * fs);
  let init_ecg = 8;
  if (dur < init_ecg) init_ecg = Math.floor(dur);
  if (init_ecg < 1) init_ecg = 1;

  // Internal filtering: Butterworth LP 25 Hz then HP 3 Hz (order 4, zero-phase).
  let filtered = butterLowpass4(signal, 25, fs);
  filtered = butterHighpass4(filtered, 3, fs);

  // Differentiate + smooth.
  let dx = diff(filtered).map((v) => Math.abs(v * fs));
  dx = hammingSmoother(dx, sm_size);

  const qrspeakbuffer = new Array<number>(init_ecg).fill(0);
  const noisepeakbuffer = new Array<number>(init_ecg).fill(0);
  const rrinterval = new Array<number>(init_ecg).fill(fs);

  const all_peaks = findExtrema(dx, "max");

  // Initialization over the first `init_ecg` one-second windows.
  let a = 0, b = v1s;
  for (let i = 0; i < init_ecg; i++) {
    let best = -Infinity;
    for (let j = a; j < Math.min(b, dx.length); j++) {
      if (j > 0 && j < dx.length - 1 && dx[j] > dx[j - 1] && dx[j] >= dx[j + 1]) {
        if (dx[j] > best) best = dx[j];
      }
    }
    if (best > -Infinity) qrspeakbuffer[i] = best;
    a += v1s; b += v1s;
  }

  let ANP = median(noisepeakbuffer);
  let AQRSP = median(qrspeakbuffer);
  const TH = 0.475;
  let DT = ANP + TH * (AQRSP - ANP);
  let indexqrs = 0, indexnoise = 0, indexrr = 0, npeaks = 0;
  const offset = 0, bpsi = 0, bpe = 0;
  const beats: number[] = [];
  const lim = Math.ceil(0.2 * fs);
  const diff_nr = Math.ceil(0.045 * fs);

  const sliceDiffMax = (center: number): { hasBoth: boolean; slope: number } => {
    let lo: number, hi: number;
    if (center < diff_nr) { lo = 0; hi = center + diff_nr; }
    else if (center + diff_nr >= signal.length) { lo = center - diff_nr; hi = dx.length; }
    else { lo = center - diff_nr; hi = center + diff_nr; }
    const seg = signal.slice(lo, hi);
    const dnow = diff(seg);
    const pos = dnow.filter((v) => v > 0).length;
    const hasBoth = pos !== 0 && pos !== dnow.length;
    let slope = -Infinity;
    for (const v of dnow) if (v > slope) slope = v;
    return { hasBoth, slope };
  };

  for (const f of all_peaks) {
    // 1 - drop peaks dominated by a larger peak within +/-200 ms.
    let dominated = false;
    for (const p of all_peaks) {
      if (p > f - lim && p < f + lim && p !== f && dx[p] > dx[f]) { dominated = true; break; }
    }
    if (dominated) continue;

    if (dx[f] > DT) {
      const now = sliceDiffMax(f);
      if (!now.hasBoth) continue;

      if (npeaks > 0) {
        const prev = beats[npeaks - 1];
        const elapsed = f - prev;
        if (elapsed < TH_elapsed) {
          const prevSlope = sliceDiffMax(prev).slope;
          if (now.slope < 0.5 * prevSlope) continue; // T-wave
        }
        if (dx[f] < 3.0 * median(qrspeakbuffer)) beats.push(f + bpsi);
        else continue;

        if (bpe === 0) {
          rrinterval[indexrr] = beats[npeaks] - beats[npeaks - 1];
          indexrr = (indexrr + 1) % init_ecg;
        }
      } else if (dx[f] < 3.0 * median(qrspeakbuffer)) {
        beats.push(f + bpsi);
      } else {
        continue;
      }

      npeaks += 1;
      qrspeakbuffer[indexqrs] = dx[f];
      indexqrs = (indexqrs + 1) % init_ecg;
    }

    if (dx[f] <= DT) {
      // 5 - secondary detection when no QRS for >= 1.5 RR and > 360 ms.
      const tf = f + bpsi;
      const RRM = median(rrinterval);
      if (beats.length >= 2) {
        const elapsed = tf - beats[npeaks - 1];
        if (elapsed >= 1.5 * RRM && elapsed > TH_elapsed && dx[f] > 0.5 * DT) {
          beats.push(f + offset);
          if (npeaks > 0) {
            rrinterval[indexrr] = beats[npeaks] - beats[npeaks - 1];
            indexrr = (indexrr + 1) % init_ecg;
          }
          npeaks += 1;
          qrspeakbuffer[indexqrs] = dx[f];
          indexqrs = (indexqrs + 1) % init_ecg;
        } else {
          noisepeakbuffer[indexnoise] = dx[f];
          indexnoise = (indexnoise + 1) % init_ecg;
        }
      } else {
        noisepeakbuffer[indexnoise] = dx[f];
        indexnoise = (indexnoise + 1) % init_ecg;
      }
    }

    ANP = median(noisepeakbuffer);
    AQRSP = median(qrspeakbuffer);
    DT = ANP + 0.475 * (AQRSP - ANP);
  }

  // Refine each beat to the dominant local peak (positive or negative) in signal.
  const thres_ch = 0.85;
  const adjacency = 0.05 * fs;
  const rBeats: number[] = [];
  for (const iBeat of beats) {
    let lo: number, add: number;
    if (iBeat - lim < 0) { lo = 0; add = 0; }
    else if (iBeat + lim >= length) { lo = iBeat - lim; add = iBeat - lim; }
    else { lo = iBeat - lim; add = iBeat - lim; }
    const hi = Math.min(iBeat + lim, length);
    const window = signal.slice(lo, hi);
    if (window.length === 0) continue;

    const posIdx = findExtrema(window, "max");
    const negIdx = findExtrema(window, "min");
    const pos = posIdx.map((i) => [window[i], i] as [number, number]).sort((p, q) => q[0] - p[0]);
    const neg = negIdx.map((i) => [window[i], i] as [number, number]).sort((p, q) => p[0] - q[0]);

    const twoPos: [number, number][] = pos.length ? [pos[0]] : [];
    for (let i = 0; i < pos.length - 1; i++) {
      if (Math.abs(pos[0][1] - pos[i + 1][1]) > adjacency) { twoPos.push(pos[i + 1]); break; }
    }
    const twoNeg: [number, number][] = neg.length ? [neg[0]] : [];
    for (let i = 0; i < neg.length - 1; i++) {
      if (Math.abs(neg[0][1] - neg[i + 1][1]) > adjacency) { twoNeg.push(neg[i + 1]); break; }
    }

    const errPos = twoPos.length < 2;
    const errNeg = twoNeg.length < 2;
    const posdiv = errPos ? 0 : Math.abs(twoPos[0][0] - twoPos[1][0]);
    const negdiv = errNeg ? 0 : Math.abs(twoNeg[0][0] - twoNeg[1][0]);
    const nErr = (errPos ? 1 : 0) + (errNeg ? 1 : 0);

    if (nErr === 0) {
      if (posdiv > thres_ch * negdiv) rBeats.push(twoPos[0][1] + add);
      else rBeats.push(twoNeg[0][1] + add);
    } else if (nErr === 2) {
      if (twoPos.length && twoNeg.length) {
        if (Math.abs(twoPos[0][1]) > Math.abs(twoNeg[0][1])) rBeats.push(twoPos[0][1] + add);
        else rBeats.push(twoNeg[0][1] + add);
      }
    } else if (errPos) {
      if (twoNeg.length) rBeats.push(twoNeg[0][1] + add);
    } else {
      if (twoPos.length) rBeats.push(twoPos[0][1] + add);
    }
  }

  return Array.from(new Set(rBeats)).sort((p, q) => p - q);
}

/** Snap each R-peak to the maximum of the filtered signal within +/-tol seconds. */
function correctRpeaks(signal: number[], rpeaks: number[], fs: number, tol = 0.05): number[] {
  const t = Math.round(tol * fs);
  const length = signal.length;
  const out: number[] = [];
  for (const r of rpeaks) {
    const a = r - t;
    if (a < 0) continue;
    const b = r + t;
    if (b > length) break;
    let best = a, bestv = -Infinity;
    for (let i = a; i < b; i++) if (signal[i] > bestv) { bestv = signal[i]; best = i; }
    out.push(best);
  }
  return Array.from(new Set(out)).sort((p, q) => p - q);
}

/** Full BioSPPy-style detection: band-pass -> Hamilton -> peak correction. */
export function detectRPeaks(values: number[], sampleRate: number): number[] {
  if (values.length < sampleRate) return [];
  const bp = filterEcg(values, sampleRate);
  const beats = hamiltonSegmenter(bp, sampleRate);
  return correctRpeaks(bp, beats, sampleRate);
}

// ============================================
// Heart rate & signal quality
// ============================================

export interface HeartRateResult {
  bpm: number | null;
  hrv: number | null;
  rPeaks: number[];
  quality: SignalQuality;
}

/** Max plausible HRV (RMSSD, ms). Above this, "beats" are really artifacts. */
const MAX_PLAUSIBLE_HRV_MS = 300;

/**
 * Estimate heart rate (and HRV) from raw ADC values using the BioSPPy treatment,
 * gated by signal quality so hum / flat leads never yield a fabricated number.
 */
export function computeHeartRate(
  values: number[],
  sampleRate: number,
  mainsHz: number = DEFAULT_MAINS_HZ,
): HeartRateResult {
  const gate = assessSignalQuality(values, sampleRate, mainsHz);
  if (gate === "no_signal" || gate === "mains_dominated") {
    return { bpm: null, hrv: null, rPeaks: [], quality: gate };
  }
  if (values.length < sampleRate * 2) {
    return { bpm: null, hrv: null, rPeaks: [], quality: "noisy" };
  }

  const rPeaks = detectRPeaks(values, sampleRate);

  const rr: number[] = [];
  for (let i = 1; i < rPeaks.length; i++) {
    const ms = ((rPeaks[i] - rPeaks[i - 1]) / sampleRate) * 1000;
    if (ms >= 300 && ms <= 2000) rr.push(ms); // 30-200 BPM
  }
  if (rr.length < 1) {
    return { bpm: null, hrv: null, rPeaks, quality: "noisy" };
  }

  const bpm = Math.round(60000 / median(rr));
  const hrv = rmssd(rr);
  if (bpm < 30 || bpm > 220) {
    return { bpm: null, hrv, rPeaks, quality: "noisy" };
  }
  if (hrv !== null && hrv > MAX_PLAUSIBLE_HRV_MS) {
    // Beat train too irregular to trust (mix of real beats and artifacts).
    return { bpm: null, hrv, rPeaks, quality: "noisy" };
  }
  return { bpm, hrv, rPeaks, quality: "good" };
}

/**
 * Blocking signal-quality gate (the part BioSPPy lacks). Returns `good` when the
 * signal is worth running detection on; `no_signal` for a flat/near-constant lead;
 * `mains_dominated` when powerline hum overwhelms it (poor electrode contact).
 */
export function assessSignalQuality(
  values: number[],
  sampleRate: number,
  mainsHz: number = DEFAULT_MAINS_HZ,
): SignalQuality {
  if (values.length < sampleRate) return "no_signal";

  // Flat / no signal: raw ADC barely moves (e.g. the {0,1,3} unplugged case).
  const rawRange = Math.max(...values) - Math.min(...values);
  if (std(values) < 3 || rawRange <= 5) return "no_signal";

  // Mains-dominated: how much energy a notch removes from the DC-corrected signal.
  // A large fraction means powerline hum is the signal (electrodes not contacting).
  if (canNotch(mainsHz, sampleRate)) {
    const hp = filtfilt(highpassCoeffs(0.5, sampleRate, Math.SQRT1_2), values.map((v) => v));
    const notched = filtfilt(notchCoeffs(mainsHz, sampleRate), hp);
    const vHp = variance(hp);
    if (vHp > 0 && 1 - variance(notched) / vHp > 0.6) return "mains_dominated";
  }

  return "good";
}
