this is all vip and some notes

# Battery Impedance Measurement
With the current and cell voltages from the BMS we can estimate cell resistance.
Impedance is depends on temperature, decreases about 1.5x per 10°C increase. ([src](https://www.youtube.com/watch?v=_8MzGy_tkEQ&t=69))

Theres DC and AC impedance https://batteryuniversity.com/article/bu-902-how-to-measure-internal-resistance
AC is easier to implement than DC, DC requires relaxation considerations.

`R(f,T,DOD)`

TI has chips ([`BQ34Z100-R2`](https://www.ti.com/lit/gpn/BQ34Z100-R2)) implemting their [Impedance Track algorithm](https://www.ti.com/lit/an/slua450a/slua450a.pdf)
[ (learning https://www.tij.co.jp/jp/lit/an/slua903/slua903.pdf) ]
[[2](https://www.ti.com/lit/wp/slpy002/slpy002.pdf)]
[[yt](https://www.youtube.com/watch?v=_8MzGy_tkEQ)]
[[fine tuning](https://www.ti.com/lit/an/slyt402/slyt402.pdf?ts=1695924597934)].
It computes the DC resistance.

The algo differentiates three states: charging, discharging and relaxation.

It constructs a lookup table OCV(DOD,T) which stores relaxed open-circuit voltage depending on DoD and temperature (pg 5).

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
* see [Reducing the Noise Floor and Improving the SNR with Cross-Correlation Techniques](https://www.zhinst.com/europe/en/blogs/how-reduce-noise-floor-and-improve-snr-employing-cross-correlation-techniques#Basic%20Principle) 

corr(x,y) = avg(x*y) - avg(x)*avg(y)
corr(x,y) = ( E(xy)-E(x)E(y) ) / sqrt( E(xx)E(yy) - E(xx)E(y)E(y) - E(yy)E(x)E(x) + E(x)E(x)E(y)E(y) )

Addionally, we can use cross-correlation, wich basically computes a table of corr(x[t],y[t+n]), for a range of offsets n.
This is computanaly intense and probably best solved by a FFT convolution.
