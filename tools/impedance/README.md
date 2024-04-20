this is all vip and some notes

# Battery Impedance Measurement

With the current and cell voltages from the BMS we can estimate cell resistance.
Impedance is depends on temperature, decreases about 1.5x per 10°C
increase. ([src](https://www.youtube.com/watch?v=_8MzGy_tkEQ&t=69))

Theres DC and AC impedance https://batteryuniversity.com/article/bu-902-how-to-measure-internal-resistance
AC is easier to implement than DC, DC requires relaxation considerations.

`R(f,T,DOD)`

TI has chips ([`BQ34Z100-R2`](https://www.ti.com/lit/gpn/BQ34Z100-R2)) implemting
their [Impedance Track algorithm](https://www.ti.com/lit/an/slua450a/slua450a.pdf)
[ (learning https://www.tij.co.jp/jp/lit/an/slua903/slua903.pdf) ]
[[2](https://www.ti.com/lit/wp/slpy002/slpy002.pdf)]
[[yt](https://www.youtube.com/watch?v=_8MzGy_tkEQ)]
[[fine tuning](https://www.ti.com/lit/an/slyt402/slyt402.pdf?ts=1695924597934)].
It computes the DC resistance.

The algo differentiates three states: charging, discharging and relaxation.

It constructs a lookup table OCV(DOD,T) which stores relaxed open-circuit voltage depending on DoD and temperature (pg
5).

It then computes DC cell resistance `R(DOD) = dV/I` and update the `R(DOD)` table also for higher DoD.

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

# Useful Statistical Formulas

* stddev(x) = pct_change(x)**2
* stddev(x) = sqrt(variance(x))
* variance(x) = E(xx) - E(x)E(x)
* covariance(x,y) = E(xy) - E(x)*E(y)
* corr(x,y) = cov(x,y) / sqrt(var(x)*var(y))
* TODO book about bazesian statistics

Now lets apply this to our cell resistance algorithm. R=dU/dI .
So we use the corr(), as it is normalized, having the same dimensionality as the inputs.

* cross correlation of dU and dI to find sample time offset
* corr(dU,1/dI) gives us the estimated cell resistance.
* it eliminates any uncorellated noise that is not present in both signals dU,dI
*

see [Reducing the Noise Floor and Improving the SNR with Cross-Correlation Techniques](https://www.zhinst.com/europe/en/blogs/how-reduce-noise-floor-and-improve-snr-employing-cross-correlation-techniques#Basic%20Principle)

corr(x,y) = avg(x*y) - avg(x)*avg(y)
corr(x,y) = ( E(xy)-E(x)E(y) ) / sqrt( E(xx)E(yy) - E(xx)E(y)E(y) - E(yy)E(x)E(x) + E(x)E(x)E(y)E(y) )

Addionally, we can use cross-correlation, wich basically computes a table of corr(x[t],y[t+n]), for a range of offsets
n.
This is computanaly intense and probably best solved by a FFT convolution.

# Current Impl State

* use `imp2.py` to visualize
* block_compute can process large time ranges of years
* cell resistance appears to increase with cell index,
    * how to the bms measure single cell voltage?
    * do higher cells ge tmore noisy?
* ("2022-01-05", "2022-05-05")
    * cell0: using data from daly_bms and jbd_bms both result a median R of 2.8/2.9mOhm.
    * dependencies: Temp, SoC
* Relaxation mask
* u0?

# Input filtering

We want to remove noise from U and I readings.
The BMS has a current sensors, that samples the current at discrete time points (usuall by measuring a small voltage
drop across a burden/shunt resistors built into the BMS).
If we have a 50 Hz inverter running, it consumes an DC+AC current with the AC part ay 100 Hz.
Some BMS have no or poor aliasing filter (Daly), so we need to average current over time.
A suitable window is ... TODO
This smoothing filter limits our AC bandwith, i.e. the maximum frequency we can sample.
Some induction coockers pulse with a period of 2.5s, which can provide us with useful AC impedance.
However, due to smoothing, this component is eliminated.
For proper AC impedance measurement we need proper alias filtering of the current sensor.

TODO: a possible fix is to add a capacitor across the differential input of the current sensor on the PCB of the BMS.
The analog filter should have a cut-off frequency of x1 to x10 of the sampling freq.
You need to try until you find a suitable value of C.

ANT bms appears to have a good alias filtering, even with a strong distorted (non-harmonic, non-sinusoidal)
AC component. JK bms will display an error and set measured current to 0 if it detects a large AC component (althoug
its sensor readouts do not suffer from aliasing). As already mentioned, Daly has aliasing issues (and a quite low
sampling period of ~1.3s).
Still, I experienced the Daly has the best SoC estimate, even with the awful AC current (aliasing noise cancels out
here)

# TODO, issues

* examine the way the BMSes measure cell voltage. with increasing cell index, cell resistance appears to increase.
  measurement of higher cells might contain more noise (ant24_23_11_12_fry)