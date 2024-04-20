*This is feature is experimental.*

Using a BMS without "talking" to the solar charger (via RS434, CAN bus, etc.) usually causes "unhealthy" charge cycles.
JK, JBD and Daly BMS, they cut off the charger if a certain voltage or cell-voltage level is reached. This is not ideal,
because voltage does hardly represent SoC and it can quickly fall after charging stops (especially with LiFePo4). It
ends up in repeating charge on/off loop, which is believed to be bad for the battery.

# Algorithms

Batmon implements charge rules (algorithms) which try to optimize cycling at "healthier" SoC levels to reduce battery
degradation.
You can enable an algorithm for each BMS, and it takes control over the charging switch.

The algorithm toggles switches at trigger points only, so you can still use the BMS switches manually overriding
the algorithm logic.
Note that a newly added algorithm doesn't do anything until a trigger point is reached, so please wait patiently.

To ensure proper SoC levels, algorithms might frequently calibrate. The calibration finishes once 100% SoC is reached.
Calibration interval is currently fixed to 14 days.

To enable an algorithm, add its signature to the BMS device entry in the add-on options:

```
    - address: "xx:yy:zz:00:11"
      type: "jk"
      alias: "jk_bms"
      algorithm: "..."
```

There is currently only one simple algorithm, see below.

# SoC Charge Algorithm

Controls the `charge` switch to limit max SoC and/or adds a charge start hysteresis.
If you know the alDente macOS App, you can compare this
to [Sailing Mode](https://apphousekitchen.com/feature-explanation-sailing-mode/)

Here are 3 scenarios you might use the algorithm for:

1. Limit the max SoC to e.g. 90%

2. "Holiday Mode": Imagine an off-grid system and you are away for a couple of weeks.
   During night only 2 % SoC is used from the battery and solar power will charge the battery to 100% each day.
   The battery is cycled at 100%-98%-100%.
   With the SoC Algorithm you for example implement 80%-70%-80% cycling, which might prolong battery lifetime.

3. Another scenario is "dumb" charger cut-off, where the BMS over-voltage protection kicks in.
   It might release after a couple of minutes as battery open circuit voltage falls over time, causing trickle charge.
   The algorithm will control charging by battery SoC rather than battery voltage, to avoid trickle charge.

## Signature

```
algorithm: "soc CHARGE_STOP% [CHARGE_START%]"
```

## Arguments

- `charge_stop`: at this SoC% the algorithm turns off the charger to avoid charging beyond
- `charge_start`: at this SoC% the algorithm turns the charger on (optional)

Even though the SoC% is below `charge_stop`, charging
is paused until `charge_start` is reached. This can avoid trickle charge by adding a hysteresis.

If `charge_start` is greater than `charge_stop` it is set to `charge_stop` and the hysteresis is disabled.

## Pseudo-Code

```
if soc% >= charge_stop:
   set_charge_switch(off)
else if soc% <= charge_start:
   set_charge_switch(on)
```

## Examples

- `algorithm: soc 90%` limits max SoC to 90% without hysteresis. (notice that this is equal
  to `algorithm: soc 90% 90%` and `algorithm: soc 90% 100%`)
- `algorithm: soc 100% 95%` avoid trickle charge
- `algorithm: soc 80% 70%` "Holiday Mode" as described above, trying to keep SoC between 80 and 70 % (10% DoD)
- `algorithm: soc 75% 25%` targets a 50% DoD (Depth-of-Discharge).
