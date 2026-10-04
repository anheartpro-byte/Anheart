type Biquad = {
  readonly b0: number;
  readonly b1: number;
  readonly b2: number;
  readonly a1: number;
  readonly a2: number;
};

const BUTTER4_Q = [0.5411961, 1.306563] as const;

function lowpassCoeffs(f0: number, fs: number, q: number): Biquad {
  const w0 = (2 * Math.PI * f0) / fs;
  const cos = Math.cos(w0);
  const alpha = Math.sin(w0) / (2 * q);
  const a0 = 1 + alpha;
  return {
    b0: (1 - cos) / 2 / a0,
    b1: (1 - cos) / a0,
    b2: (1 - cos) / 2 / a0,
    a1: (-2 * cos) / a0,
    a2: (1 - alpha) / a0,
  };
}

export function highpassCoeffs(f0: number, fs: number, q: number): Biquad {
  const w0 = (2 * Math.PI * f0) / fs;
  const cos = Math.cos(w0);
  const alpha = Math.sin(w0) / (2 * q);
  const a0 = 1 + alpha;
  return {
    b0: (1 + cos) / 2 / a0,
    b1: -(1 + cos) / a0,
    b2: (1 + cos) / 2 / a0,
    a1: (-2 * cos) / a0,
    a2: (1 - alpha) / a0,
  };
}

export function notchCoeffs(f0: number, fs: number, q = 30): Biquad {
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

function applyBiquad(c: Biquad, x: readonly number[]): number[] {
  const y = new Array<number>(x.length);
  const initialInput = x[0];
  const initialOutput =
    initialInput * ((c.b0 + c.b1 + c.b2) / (1 + c.a1 + c.a2));
  let x1 = initialInput,
    x2 = initialInput,
    y1 = initialOutput,
    y2 = initialOutput;
  for (let i = 0; i < x.length; i++) {
    const xi = x[i];
    const yi = c.b0 * xi + c.b1 * x1 + c.b2 * x2 - c.a1 * y1 - c.a2 * y2;
    x2 = x1;
    x1 = xi;
    y2 = y1;
    y1 = yi;
    y[i] = yi;
  }
  return y;
}

export function filtfilt(c: Biquad, x: readonly number[]): number[] {
  if (x.length === 0) return [];
  const fwd = applyBiquad(c, x);
  const back = applyBiquad(c, fwd.slice().reverse());
  return back.reverse();
}

export function butterLowpass4(
  x: readonly number[],
  f0: number,
  fs: number,
): readonly number[] {
  let out = x;
  for (const q of BUTTER4_Q) out = filtfilt(lowpassCoeffs(f0, fs, q), out);
  return out;
}

export function butterHighpass4(
  x: readonly number[],
  f0: number,
  fs: number,
): readonly number[] {
  let out = x;
  for (const q of BUTTER4_Q) out = filtfilt(highpassCoeffs(f0, fs, q), out);
  return out;
}
