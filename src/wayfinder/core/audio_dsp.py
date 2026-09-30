"""NumPy versions of the three scipy.signal calls the recorder makes.

``butter`` (2nd order), ``filtfilt`` and ``resample_poly`` reproduce SciPy's
algorithms and defaults exactly (tests/test_audio_dsp.py checks them against
SciPy on every device rate the recorder resamples). The recorder prefers SciPy
when it is installed, so Linux and macOS are unchanged; the Windows bundle
leaves SciPy out (~72 MB) and runs these instead.

The IIR filter avoids a per-sample Python loop: its response is the zero-state
part (an FFT convolution with the impulse response, cut once it has decayed
below 1e-13 of its peak) plus the zero-input part from the initial state
(a short recursion over the same decay length). Both are exact to float64
rounding for a stable filter.
"""

from __future__ import annotations

from math import gcd, pi, sqrt, tan

import numpy as np

_DECAY_TOL = 1e-13
_MAX_RESPONSE = 1 << 18


def butter(order: int, wn: float, btype: str = "low"):
    """scipy.signal.butter(order, wn, btype) for a 2nd-order low/high-pass (wn relative
    to Nyquist): the bilinear transform with pre-warping, as SciPy computes it."""
    if order != 2 or btype not in ("low", "lowpass", "high", "highpass"):
        raise NotImplementedError("only 2nd-order low/high-pass Butterworth is needed")
    if not 0 < wn < 1:
        raise ValueError("wn must be between 0 and 1")
    k = tan(pi * wn / 2.0)
    norm = 1.0 / (1.0 + sqrt(2.0) * k + k * k)
    a = np.array([1.0, 2.0 * (k * k - 1.0) * norm, (1.0 - sqrt(2.0) * k + k * k) * norm])
    if btype.startswith("high"):
        b = np.array([norm, -2.0 * norm, norm])
    else:
        b = np.array([k * k * norm, 2.0 * k * k * norm, k * k * norm])
    return b, a


def _normalize(b, a):
    b = np.atleast_1d(np.asarray(b, dtype=np.float64))
    a = np.atleast_1d(np.asarray(a, dtype=np.float64))
    if a[0] == 0:
        raise ValueError("a[0] must be nonzero")
    b, a = b / a[0], a / a[0]
    n = max(len(a), len(b))
    return np.pad(b, (0, n - len(b))), np.pad(a, (0, n - len(a)))


def lfilter_zi(b, a) -> np.ndarray:
    """scipy.signal.lfilter_zi: the step response's steady-state filter state."""
    b, a = _normalize(b, a)
    n = len(a)
    companion = np.zeros((n - 1, n - 1))
    companion[0, :] = -a[1:]
    companion[1:, :-1] += np.eye(n - 2)
    i_minus_a = np.eye(n - 1) - companion.T
    return np.linalg.solve(i_minus_a, b[1:] - a[1:] * b[0])


def _recurse(b, a, x, state, length: int) -> np.ndarray:
    """Direct form II transposed, like scipy.signal.lfilter, for *length* samples."""
    z = np.array(state, dtype=np.float64)
    order = len(z)
    y = np.zeros(length)
    for n in range(length):
        xn = x[n] if n < len(x) else 0.0
        yn = b[0] * xn + z[0]
        for i in range(order - 1):
            z[i] = b[i + 1] * xn - a[i + 1] * yn + z[i + 1]
        z[order - 1] = b[order] * xn - a[order] * yn
        y[n] = yn
    return y


def _decay_length(b, a) -> int:
    """Samples until the impulse response has decayed below tolerance."""
    order = len(a) - 1
    z = np.zeros(order)
    peak, quiet, n = 0.0, 0, 0
    x = 1.0
    while n < _MAX_RESPONSE:
        yn = b[0] * x + z[0]
        for i in range(order - 1):
            z[i] = b[i + 1] * x - a[i + 1] * yn + z[i + 1]
        z[order - 1] = b[order] * x - a[order] * yn
        x = 0.0
        n += 1
        peak = max(peak, abs(yn))
        quiet = quiet + 1 if abs(yn) <= _DECAY_TOL * peak and np.all(np.abs(z) <= _DECAY_TOL * peak) else 0
        if quiet >= 8:
            return n
    raise ValueError("filter does not decay (unstable or too narrow)")


def lfilter(b, a, x, zi=None) -> np.ndarray:
    """scipy.signal.lfilter(b, a, x, zi=zi)[0] for a stable IIR filter."""
    b, a = _normalize(b, a)
    x = np.asarray(x, dtype=np.float64)
    length = _decay_length(b, a)
    impulse = np.zeros(length)
    impulse[0] = 1.0
    h = _recurse(b, a, impulse, np.zeros(len(a) - 1), length)
    size = 1 << int(len(x) + length - 1).bit_length()
    y = np.fft.irfft(np.fft.rfft(x, size) * np.fft.rfft(h, size), size)[: len(x)]
    if zi is not None:
        tail = min(len(x), length)
        y[:tail] += _recurse(b, a, np.zeros(0), zi, tail)
    return y


def _odd_ext(x: np.ndarray, n: int) -> np.ndarray:
    left = 2 * x[0] - x[n:0:-1]
    right = 2 * x[-1] - x[-2:-(n + 2):-1]
    return np.concatenate((left, x, right))


def filtfilt(b, a, x) -> np.ndarray:
    """scipy.signal.filtfilt(b, a, x) with its defaults (odd padding, 3 x taps)."""
    x = np.asarray(x, dtype=np.float64)
    padlen = 3 * max(len(np.atleast_1d(a)), len(np.atleast_1d(b)))
    if x.shape[0] <= padlen:
        raise ValueError(f"the length of the input must be more than padlen ({padlen})")
    ext = _odd_ext(x, padlen)
    zi = lfilter_zi(b, a)
    y = lfilter(b, a, ext, zi * ext[0])
    y = lfilter(b, a, y[::-1], zi * y[-1])[::-1]
    return y[padlen:-padlen]


def _firwin_lowpass(numtaps: int, cutoff: float, beta: float) -> np.ndarray:
    """scipy.signal.firwin(numtaps, cutoff, window=("kaiser", beta)): unit DC gain."""
    m = np.arange(numtaps) - (numtaps - 1) / 2.0
    h = cutoff * np.sinc(cutoff * m) * np.kaiser(numtaps, beta)
    return h / h.sum()


def resample_poly(x, up: int, down: int, window=("kaiser", 5.0)) -> np.ndarray:
    """scipy.signal.resample_poly(x, up, down) with its default Kaiser (beta 5) filter
    and zero padding: the same filter, padding and output alignment."""
    if window[0] != "kaiser":
        raise NotImplementedError("only the default Kaiser window is supported")
    x = np.asarray(x, dtype=np.float64)
    g = gcd(int(up), int(down))
    up, down = int(up) // g, int(down) // g
    if up == down == 1:
        return x.copy()
    n_in = x.shape[0]
    n_out = n_in * up // down + bool(n_in * up % down)
    max_rate = max(up, down)
    half_len = 10 * max_rate
    h = _firwin_lowpass(2 * half_len + 1, 1.0 / max_rate, window[1]) * up
    n_pre_pad = down - half_len % down
    n_pre_remove = (half_len + n_pre_pad) // down

    def full_len(len_h: int) -> int:
        return ((n_in - 1) * up + len_h - 1) // down + 1

    n_post_pad = 0
    while full_len(len(h) + n_pre_pad + n_post_pad) < n_out + n_pre_remove:
        n_post_pad += 1
    h = np.concatenate((np.zeros(n_pre_pad), h, np.zeros(n_post_pad)))

    # Polyphase upfirdn: output m sums h[t - k*up] * x[k] with t = m*down. Since
    # gcd(up, down) == 1, the outputs m, m+up, m+2up... share one filter phase and
    # read the input at a stride of `down`: one matrix-vector product per phase
    # over a zero-copy sliding-window view.
    taps = -(-len(h) // up)
    padded = np.concatenate((np.zeros(taps), x, np.zeros(taps + down * up)))
    windows = np.lib.stride_tricks.sliding_window_view(padded, taps)
    out = np.empty(n_out)
    for r in range(min(up, n_out)):
        t = (n_pre_remove + r) * down
        coeffs = np.zeros(taps)
        phase = h[t % up::up]
        coeffs[: len(phase)] = phase
        count = len(range(r, n_out, up))
        # windows[s] = padded[s:s+taps]; newest sample k0 sits at padded[k0 + taps].
        rows = windows[t // up + 1:: down][:count]
        out[r::up] = rows @ coeffs[::-1]
    return out
