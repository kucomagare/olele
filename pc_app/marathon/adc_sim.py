# ADC front end: per-channel gain+offset, then one shared ADC's clip+quantize.
# Forward needs a channel (gain/offset are per-channel); reverse doesn't,
# since gain/offset sit upstream of the ADC's own code<->voltage mapping.
WIRE_BITS = 32  # packet_format.json's fixed slot width

import numpy as np

import config

_GAIN = {1: "GAIN_CH1", 2: "GAIN_CH2"}
_OFFSET = {1: "V_OFFSET_CH1", 2: "V_OFFSET_CH2"}


def _code_max():
    return (1 << config.ADC_BITS) - 1


def digitize(analog_mv, channel, dtype):
    """mV (ECG+noise+sine summed) -> wire-domain dtype: gain -> offset ->
    clip[VREF_MINUS,VREF_PLUS] -> quantize(ADC_BITS) -> zero-padded."""
    gain = getattr(config, _GAIN[channel])
    offset = getattr(config, _OFFSET[channel])
    analog_v = (analog_mv / 1000.0) * gain + offset

    clipped = np.clip(analog_v, config.VREF_MINUS, config.VREF_PLUS)
    span = config.VREF_PLUS - config.VREF_MINUS
    code_max = _code_max()
    code = np.round((clipped - config.VREF_MINUS) / span * code_max)
    wire = code.astype(np.uint64) << (WIRE_BITS - config.ADC_BITS)
    return wire.astype(dtype)


def to_volts(wire_code, vref_minus=None, vref_plus=None, adc_bits=None):
    """Wire code(s) -> ADC input voltage. Channel-independent (see top).
    Optional overrides let a saved capture replay its OWN recorded
    VREF/ADC_BITS instead of this session's live config -- same reasoning
    as sat.py's model_params(): a dump analysed later must not silently
    reinterpret itself under whatever the knobs are now."""
    vref_minus = config.VREF_MINUS if vref_minus is None else vref_minus
    vref_plus = config.VREF_PLUS if vref_plus is None else vref_plus
    adc_bits = config.ADC_BITS if adc_bits is None else adc_bits
    code_max = (1 << adc_bits) - 1
    code = np.asarray(wire_code).astype(np.uint64) >> (WIRE_BITS - adc_bits)
    return vref_minus + code.astype(np.float64) / code_max * (vref_plus - vref_minus)


def from_volts(volts):
    """Inverse of to_volts -- volts -> nearest wire code."""
    span = config.VREF_PLUS - config.VREF_MINUS
    code = (np.asarray(volts) - config.VREF_MINUS) / span * _code_max()
    return code * (1 << (WIRE_BITS - config.ADC_BITS))
