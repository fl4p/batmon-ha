# Storing batmon telemetry compactly in QuestDB (scaled integers)

> **Status (2026-06): implemented as `QuestDBSink` in `bmslib/sinks.py`; takes
> effect once the fresh scaled-int QuestDB tables are deployed via the migration
> rerun (`tools/influxdb-migration`, scaler pending).**
>
> Flipping a field from float to integer on the wire is *protocol*-valid
> (`field=42i` is standard line protocol), but both InfluxDB and QuestDB pin a
> field/column's type on first write. batmon has already ingested these fields as
> floats, so emitting them as integers now triggers `field type conflict`
> rejections (InfluxDB) or a cast back to float (QuestDB) -- the change cannot be
> retrofitted onto a series that already has float history.
>
> Decision: do **not** change the wire types in the running InfluxDB sink. We are
> moving this deployment to QuestDB anyway, so the integer/scaled-int types get
> defined correctly at table-creation time on the fresh QuestDB schema (see
> `tm-questdb/schema/tables.sql`), and ingestion starts clean against those
> columns. The trial edit to the generic `publish_sample()` was reverted.
>
> The fresh-schema cutover is implemented as a dedicated `QuestDBSink` in
> `bmslib/sinks.py` (subclass of `InfluxDBSink`): it applies the scale map below
> (`QUESTDB_INT_SCALE`), types `problem_code` as LONG / `switches_*` as BOOLEAN,
> and drops any field absent from the schema (ILP auto-create is on).
> `TelemetrySink` now extends it. **Prerequisite:** the matching columns in
> `tables.sql` must be `INT`/`LONG`, not `FLOAT` -- emitting an integer into a
> FLOAT column coerces it back to a float and loses the win, and the unit change
> (V->mV, A->mA, etc.) means it must be a fresh table, never mixed with existing
> FLOAT-volt history.

This is an implementation note for making batmon's telemetry compress well when
the InfluxDB sink writes into **QuestDB** (a drop-in InfluxDB-line-protocol
endpoint). QuestDB can convert cold partitions to Parquet with the **pco** numeric
codec, and pco rewards one thing above all: **feed it integers, not "nice"
decimals stored as floats.**

## TL;DR

In `bmslib/sinks.py`, `publish_sample()` currently does this:

```python
for k, v in fields.items():
    if isinstance(v, int):
        fields[k] = float(v)      # (1) every int -> float
    elif isinstance(v, float):
        fields[k] = round(v, 3)   # (2) float rounded to 3 decimals
```

Both lines hurt QuestDB/pco compression:

- **(1) int -> float** turns clean integers into floats for no reason. (It exists
  to keep a field's InfluxDB type stable across BMSes; see "Why it coerces"
  below.)
- **(2) round(v, 3)** caps precision at 0.001 -- good intent -- but stored as a
  *float* that 0.001 grid is the worst case for pco: `0.001` has no exact binary
  representation, so e.g. `104.910` is stored as `104.90999...` with
  pseudo-random low mantissa bits that pco must preserve as entropy. The decimal
  "niceness" is invisible to a binary codec.

**Fix:** emit the value at its real precision as a **scaled integer**
(`int(round(v * scale))`) instead of a rounded float. The integer is exact, has
no exponent/mantissa noise, and pco compresses it far better. The HA/MQTT side
can keep using floats; this is only about what the InfluxDB/QuestDB sink writes.

## Why it helps (measured)

On a real 22.7M-row export of batmon data, comparing each field stored as `f32`
(today) vs as a lossless scaled `INT`, pco bytes/value:

| field            | scale | float b/v | int b/v | gain   | verdict                |
|------------------|-------|-----------|---------|--------|------------------------|
| `voltage`        | x1000 | 1.012     | 0.719   | 1.4x   | **scale to int (mV)**  |
| `soc`            | x100  | 0.788     | 0.404   | 1.9x   | **scale to int**       |
| `mos_temperature`| x100  | 0.476     | 0.230   | 2.1x   | **scale to int (c-C)** |
| `balance_current`| x1000 | 1.321     | 1.148   | 1.2x   | scale to int           |
| `capacity`       | x100  | 0.129     | 0.085   | 1.5x   | scale to int           |
| `current`        | x1000 | 0.989     | 1.159   | **0.9x** | scaled to mA anyway (~10% larger, see note) |
| `power`,`charge`,`uptime`,`total_charge_throughput` | -- | -- | -- | -- | **keep float** (range too wide for an int32 grid) |
| `num_samples`,`runtime`,`num_cycles` | x1 | -- | ~1.0x | already integer-valued |

Two non-obvious results, both important:

- **`current` gets *worse* as a scaled int** (0.989 -> 1.159). It is signed and
  oscillates around zero; scaling to milliamps adds entropy pco then has to
  encode. **Measure each field; do not blanket-scale.** Signed, noisy fields
  often compress better left as floats. The shipped `QuestDBSink` nonetheless
  scales `current` to mA (a deliberate choice: uniform integer columns and a
  fixed mA unit, accepting ~10% more bytes on this one field). If its Parquet
  size ever dominates, drop it from `QUESTDB_INT_SCALE` and make the column FLOAT.
- The big absolute win is `voltage` (~4 MB over the export); most other fields
  are mostly-NULL and already compress to near-zero, so scaling them barely
  moves the file. Prioritise the dense, always-present fields.

## Shipped implementation

`QuestDBSink` (`bmslib/sinks.py`) is the single source of truth. A field must
always be written with the same type/scale (QuestDB, like InfluxDB, pins a
column's type on first write), so the maps below are fixed. The actually-shipped
scale map -- note it scales more fields than the measured table strictly
justifies (uniform integer columns; the unmeasured ones cost little either way):

```python
QUESTDB_INT_SCALE = {
    "voltage": 1000, "current": 1000, "balance_current": 1000,   # -> mV / mA
    "soc": 100, "soh": 100,                                       # -> centi-%
    "capacity": 100, "aged_capacity": 100, "cycle_capacity": 100,# -> centi-Ah
    "mos_temperature": 100,                                       # -> centi-degC
    **{("temperatures_%d" % i): 100 for i in range(8)},          # -> centi-degC
}
QUESTDB_LONG_FIELDS  = {"problem_code"}                          # int, no scale
QUESTDB_BOOL_FIELDS  = {"switches_charge", "switches_discharge", ...}  # 13 flags
QUESTDB_FLOAT_FIELDS = {"power", "_power", "charge",             # wide range ->
    "total_charge_throughput", "num_cycles", "num_samples",      #   keep FLOAT
    "battery_charging", "problem", "uptime", "runtime"}
```

Routing per field: scaled-int (`int(round(v*scale))`) -> LONG -> BOOLEAN ->
FLOAT, and anything not in any set is **dropped** (ILP auto-create is on, so a
stray field would re-create a column the schema deliberately removed). Per-cell
`voltage_cell000..031` and the `cells.voltage` column are already integer mV, so
they pass through unscaled (x1).

Decode on the read side is just `stored / scale` (the unit per column is in
`tm-questdb/schema/tables.sql`). The transform is lossy only below the chosen
scale, which matches the previous `round(v, 3)` precision cap.

## Why the generic sink coerces int -> float

Different BMS drivers report the same logical field as `int` on one model and
`float` on another. InfluxDB (and QuestDB) pin a field's column type on first
write, so a field that is sometimes `123i` and sometimes `123.0` triggers a
type-conflict error. The generic `InfluxDBSink` coerces everything to `float` to
sidestep that (and `QuestDBSink` keeps that fallback for unscaled fields). The
fixed `QUESTDB_INT_SCALE` map preserves the guarantee while choosing `int`
deliberately for the fields where it helps -- the type is stable because the
scale is constant.

## Cell voltages -- already integers, but clip glitches

`publish_voltages()` already writes `voltage_cell%03i` as `int` (millivolts) --
ideal, nothing to change. One data-quality note: the export showed four cells
with impossible spikes (48000-62000 mV; a real Li-ion cell is <= ~4200 mV).
Clip/drop physically-impossible cell readings at the source -- it keeps the data
honest and also keeps values inside a 16-bit range if QuestDB ever stores the
column natively.

## What does NOT help (don't bother)

- **Don't narrow the integer type for compression.** pco is width-agnostic: the
  same values as LONG/INT/SHORT compress to an identical Parquet blob, and
  QuestDB writes SHORT as INT32 in Parquet anyway. Emit a plain integer; the type
  width only matters for QuestDB's native (pre-Parquet) `.d` files, not for pco.
- **Don't pre-delta or hand-transform.** pco auto-deltas and detects common
  multiples; a manual delta earns nothing.
- For wide-range positive fields (`power`, `total_charge_throughput`), a linear
  int grid overflows; if those ever need shrinking, a `round(ln(x)*scale)` log
  grid (positive only) is the tool -- but they are not worth it today.

See the upstream rationale and full ruleset in the QuestDB fork:
`docs/pco-guide.md` (in fl4p/questdb).
