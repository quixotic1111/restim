import numpy as np

def burst_gap_frequency_to_pulse_frequency(carrier_frequency, burst_gap_frequency, pulse_width):
    """
    Convert the burst gap frequency, which is 1/burst_gap_duration, to the pulse frequency
    :param carrier_frequency:
    :param burst_gap_frequency:
    :param pulse_width:
    :return:
    """
    duration = 1/burst_gap_frequency + pulse_width/carrier_frequency
    duration = np.clip(duration, 0.001, None)
    return 1/duration

def pulse_frequency_to_burst_gap_frequency(carrier_frequency, pulse_frequency, pulse_width):
    duration = 1/pulse_frequency - pulse_width/carrier_frequency
    duration = np.clip(duration, .001, None)
    return 1/duration
