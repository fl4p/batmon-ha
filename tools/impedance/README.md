# Battery Impedance Measurement
With the current and cell voltages from the BMS we can estimate cell resistance.
Impedance is depends on temperature, decreases about 1.5x per 10°C increase. ([src](https://www.youtube.com/watch?v=_8MzGy_tkEQ&t=69))

TI has chips ([`BQ34Z100-R2`](https://www.ti.com/lit/gpn/BQ34Z100-R2)) implemting their [Impedance Track algorithm](https://www.ti.com/lit/an/slua450a/slua450a.pdf) [[2](https://www.ti.com/lit/wp/slpy002/slpy002.pdf)] [[yt](https://www.youtube.com/watch?v=_8MzGy_tkEQ)]

The algo differentiates three states: charging, discharging and relaxation.

It constructs a lookup table OCV(DOD,T) which stores relaxed open-circuit voltage depending on DoD and temperature (pg 5).

It then computes cell resistance `R(DOD) = dV/I` and update the `R(DOD)` table also for higher DoD.

* Quit Current should not exceed C/20.

The algorithm is rather complex, updating multiple tables.

# Our approach

We simply use moving statistics within a window of recent (U,I) readings.
Only update R when there is a step above threshold
Ideas:
* using OLS regression in a moving window
* simpler: use std(U)/std(I) in a moving window
  * less noise resistant
* OLS with clustering for noise rejection?
* cross correlation (U,I) ? update is O(n) !
# std approach  




https://www.ti.com/lit/wp/slpy002/slpy002.pdf