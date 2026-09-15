import math
import statistics


def round_to_n(x, n):
    # todo compare to np.format_float_positional
    if isinstance(x, str) or not math.isfinite(x) or not x:
        return x

    if n == 0:
        return str(round(x, None))

    digits = -int(math.floor(math.log10(abs(x)))) + (n - 1)

    try:
        # return ('%.*f' % (digits, x))
        return str(round(x, digits or None))  # digits=0 will output 12.0, digits=None => 12
    except ValueError as e:
        print('error', x, n, e)
        raise e


def capitalize_words(s):
    return ' '.join(word[0].upper() + word[1:] for word in s.split())


def is_none_or_nan(val):
    if val is None:
        return True
    if isinstance(val, float) and (math.isnan(val) or not math.isfinite(val)):
        return True
    return False


# units: https://github.com/home-assistant/core/blob/d7ac4bd65379e11461c7ce0893d3533d8d8b8cbf/homeassistant/const.py#L384
sample_desc = {
    "soc/total_voltage": {
        "field": "voltage",
        "device_class": "voltage",
        "state_class": "measurement",
        "unit_of_measurement": "V",
        "precision": 2,
        "significant_digits": 4,  # round_to_n
        "icon": "meter-electric"},
    "soc/current": {
        "field": "current",
        "device_class": "current",
        "state_class": "measurement",
        "unit_of_measurement": "A",
        "precision": 2,
        "significant_digits": 4,
    },
    "soc/balance_current": {
        "field": "balance_current",
        "device_class": "current",
        "state_class": "measurement",
        "unit_of_measurement": "A",
        "precision": 2,
        "significant_digits": 4,
        "icon": "scale-unbalanced"},
    "soc/soc_percent": {
        "field": "soc",
        "device_class": "battery",
        "state_class": "measurement",
        "unit_of_measurement": "%",
        "precision": 2,
        "significant_digits": 4,
        "icon": "battery"},
    "soc/power": {
        "field": "power",
        "device_class": "power",
        "state_class": "measurement",
        "unit_of_measurement": "W",
        "precision": 1,
        "significant_digits": 4,
        "icon": "flash"},
    "soc/capacity": {
        "field": "capacity",
        "device_class": None,
        "state_class": "measurement",
        "unit_of_measurement": "Ah"
    },
    "soc/aged_capacity": {
        "field": "aged_capacity",
        "device_class": None,
        "state_class": None,
        "unit_of_measurement": "Ah",
        "precision": 2,
        "icon": "battery-heart-variant"},
    "soc/soh": {
        "field": "soh",
        "device_class": None,
        "state_class": "measurement",
        "unit_of_measurement": "%",
        "precision": 1,
        "icon": "battery-heart-variant"},
    # Topic key kept as ``soc/cycle_capacity`` (and therefore HA's unique_id /
    # entity_id) so existing user automations and long-term statistics keep
    # working across the rename. The HA display name is auto-derived from
    # ``field`` and will refresh to "Total Charge Throughput".
    "soc/cycle_capacity": {
        "field": "total_charge_throughput",
        "device_class": None,
        "state_class": "total_increasing",
        "unit_of_measurement": "Ah"},
    "soc/num_cycles": {
        "field": "num_cycles",
        "device_class": None,
        "state_class": "measurement",
        "unit_of_measurement": "N",
        "icon": "battery-sync"},
    "mosfet_status/capacity_ah": {
        "field": "charge",
        "device_class": None,
        "state_class": "measurement",
        "unit_of_measurement": "Ah"},
    "mosfet_status/temperature": {
        "field": "mos_temperature",
        "device_class": "temperature",
        "state_class": "measurement",
        "unit_of_measurement": "°C",
        "icon": "thermometer"},
    "bms/uptime": {
        "field": "uptime",
        "device_class": "duration",
        "state_class": "measurement",
        "unit_of_measurement": "s",
        "precision": 0,
        "icon": "clock"},
    "bms/runtime": {
        "field": "runtime",
        "device_class": "duration",
        "state_class": "measurement",
        "unit_of_measurement": "s",
        "precision": 0,
        "icon": "timer-sand"},
    "soc/total_charge_net": {
        "field": "total_charge_net",
        "device_class": None,
        "state_class": "total_increasing",
        "unit_of_measurement": "Ah",
        "icon": "battery-arrow-down"},
    "meter/sample_count": {
        "field": "num_samples",
        "device_class": None,
        "state_class": "measurement",
        "unit_of_measurement": "N",
        "icon": "counter"},
}

meter_desc = {
    # state_class see https://developers.home-assistant.io/docs/core/entity/sensor/#long-term-statistics
    # this enables the meters to appear in HA Energy Grid
    'total_energy': dict(device_class="energy", state_class="total", unit="kWh", icon="meter-electric",
                         name="total energy netted"),
    'total_energy_charge': dict(device_class="energy", state_class="total_increasing", unit="kWh",
                                icon="meter-electric", name="total energy input"),
    'total_energy_discharge': dict(device_class="energy", state_class="total_increasing", unit="kWh",
                                   icon="meter-electric", name="total energy output"),
    'total_charge': dict(device_class=None, state_class="total", unit="Ah", name="total charge netted"),
    'total_cycles': dict(device_class=None, state_class="total_increasing", unit="N", icon="battery-sync",
                         name="total cycle count"),
}


def balancing_cells_str(mask: int) -> str:
    """Bitmask (bit 0 = cell 1) -> "1,5,12", or "none" so the HA sensor never
    gets an empty payload (which it ignores)."""
    cells = [str(i + 1) for i in range(32) if mask & (1 << i)]
    return ','.join(cells) if cells else 'none'


def cell_stats(voltages_mv: list) -> dict:
    if len(voltages_mv) < 2:
        return {}
    x = range(len(voltages_mv))
    high_i = max(x, key=lambda i: voltages_mv[i])
    low_i = min(x, key=lambda i: voltages_mv[i])
    min_mv = voltages_mv[low_i]
    max_mv = voltages_mv[high_i]
    return {
        'min_mv': min_mv,
        'max_mv': max_mv,
        'delta_mv': max_mv - min_mv,
        'min_index': low_i + 1,
        'max_index': high_i + 1,
        'avg_mv': round(sum(voltages_mv) / len(voltages_mv)),
        'median_mv': statistics.median(voltages_mv),
    }
