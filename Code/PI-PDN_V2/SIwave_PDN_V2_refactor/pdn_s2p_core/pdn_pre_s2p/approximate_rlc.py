"""공식 직렬 2포트 S2P에서 수동 Foster 회로를 근사한다. 제조사 공식 netlist가 아니다."""
from __future__ import annotations

import hashlib
from io import StringIO

import numpy as np
import skrf
from scipy.optimize import least_squares

from .netlist import compare_rlc_to_s2p, rlc_impedance_samples


def _network(payload):
    stream = StringIO(payload.decode('utf-8-sig'))
    stream.name = 'source.s2p'
    network = skrf.Network(stream)
    if (network.nports != 2 or len(network.f) < 10 or not np.all(np.isfinite(network.s))
            or not np.all(np.isfinite(network.f)) or not np.all(network.f > 0)
            or not np.all(np.diff(network.f) > 0)
            or not np.allclose(network.z0, network.z0[0, 0])
            or network.z0[0, 0].imag != 0 or network.z0[0, 0].real <= 0):
        raise ValueError('approximation_requires_positive_frequency_two_port_real_reference')
    if np.linalg.svd(network.s, compute_uv=False).max() > 1.00001:
        raise ValueError('approximation_requires_passive_source')
    if (np.max(np.abs(network.s[:, 0, 0] - network.s[:, 1, 1])) > 1e-5
            or np.max(np.abs(network.s[:, 0, 1] - network.s[:, 1, 0])) > 1e-5
            or np.max(np.abs(network.s[:, 0, 0] + network.s[:, 1, 0] - 1)) > 1e-5):
        raise ValueError('approximation_requires_series_two_terminal_topology')
    return network


def validate_derived_rlc(rlc_payload, s2p_payload):
    result = compare_rlc_to_s2p(rlc_payload, s2p_payload)
    network = _network(s2p_payload)
    actual = 2 * network.z0[0, 0].real * (1 / network.s[:, 1, 0] - 1)
    predicted = np.asarray(rlc_impedance_samples(rlc_payload, network.f))
    error = float(np.max(np.abs(predicted - actual) / np.maximum(np.abs(actual), 1e-12)))
    if not np.isfinite(error) or error > 0.10:
        raise ValueError(f'derived_rlc_impedance_mismatch(maxRelativeComplexZError={error:.6g},limit=0.1)')
    return {**result, 'maxRelativeComplexZError': error, 'maxRelativeComplexZErrorLimit': 0.10,
            'extrapolationAllowed': False}


def derive_rlc_from_s2p(payload):
    network = _network(payload)
    w = 2 * np.pi * network.f
    z = 2 * network.z0[0, 0].real * (1 / network.s[:, 1, 0] - 1)
    if np.any(z.real < -1e-7) or z[0].imag >= 0:
        raise ValueError('approximation_requires_passive_capacitive_impedance')
    capacitance = -1 / (w[0] * z[0].imag)
    initial = [.01, 1e-10, capacitance, .03, 1e-5, .02, 1e-6, .02, 1e-7,
               .08, 2e-10, .2, 2e-11, 100, 3e-10]

    def impedance(p):
        values = np.exp(p)
        out = values[0] + 1j * w * values[1] + 1 / (1j * w * values[2])
        for k in range(3, 9, 2):
            out += values[k] / (1 + 1j * w * values[k] * values[k + 1])
        for k in range(9, 15, 2):
            out += 1j * w * values[k + 1] * values[k] / (values[k] + 1j * w * values[k + 1])
        return out

    def residual(p):
        error = (impedance(p) - z) / np.maximum(np.abs(z), .02)
        return np.r_[error.real, error.imag]

    fit = least_squares(residual, np.log(initial), max_nfev=600, ftol=1e-7, xtol=1e-7, gtol=1e-7,
                        bounds=(np.log([1e-8, 1e-15, 1e-13] + [1e-8, 1e-13] * 3 + [1e-8, 1e-15] * 3),
                                np.log([1e6, 1e-3, 1] + [1e6, 1] * 3 + [1e6, 1e-3] * 3)))
    values = np.exp(fit.x)
    lines = ['* Derived approximation from official S2P; not an official vendor netlist',
             '* Valid only within source S2P frequency range; bias/temperature inherited without inference',
             '.SUBCKT MODEL 1 2', f'R1 1 3 {values[0]:.16e}', f'L1 3 4 {values[1]:.16e}',
             f'C1 4 5 {values[2]:.16e}']
    for block, k in enumerate(range(3, 15, 2)):
        a, b = str(5 + block), ('2' if block == 5 else str(6 + block))
        kind = 'C' if k < 9 else 'L'
        lines.extend([f'R{block+2} {a} {b} {values[k]:.16e}', f'{kind}{block+2} {a} {b} {values[k+1]:.16e}'])
    lines.append('.ENDS MODEL')
    circuit = ('\n'.join(lines) + '\n').encode('ascii')
    validation = validate_derived_rlc(circuit, payload)
    return circuit, {**validation, 'algorithm': 'positive_foster_rlc_weighted_complex_impedance_fit_v1',
                     'optimizerConverged': bool(fit.success), 'optimizerEvaluations': fit.nfev,
                     'sourceS2pSha256': hashlib.sha256(payload).hexdigest()}
