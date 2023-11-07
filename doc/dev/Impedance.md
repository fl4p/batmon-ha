*this is a draft and personal notes*

* see tools/impedance for code
* [tools/impedance/README](../../tools/impedance/README.md)


* there is AC and DC cell resistance
* DC resistance is in relaxed state (TI bq chips)
* Lifepo4 can have relexation periods of 6 hours !
* AC resistance is frequency dependend
* resistance depends on SoC and temperature
* temperature dependence should not be ignored! need to collect temp data

TI Algorithm Impedance Track

* [Gauging: Achieving The Successful Learning Cycle](https://www.tij.co.jp/jp/lit/an/slua903/slua903.pdf)

* [Theory and Implementation of Impedance Track™ Battery
Fuel-Gauging Algorithm in bq20zxx Product Family](https://www.ti.com/lit/an/slua364b/slua364b.pdf?ts=1691485130796)

  

# Simplified DC Algorithm
* Start Conditions:
  * Voltage in range
  * Current above threshold 
  * Conditions met for 500 sec
* wait for current change
* then wait for a couple of seconds for relaxation


We try to implement a DC algorithm that considers relaxation.
We can try to model cell relaxation. This will give us even more insights into cell SoH.
Here we try to be keep it as simple as possible and its a first approach.

For minimu memory footprint (so we possbily can run this on an MCU), we can
use EWMA instead of a look back window buffer of recent values. 

### OLS (ordinary least squared, regression)


### stddev
Compute Standard deviation of recent U and I readings.
Has poor noise rejection. If U has more noise than I, cell resistance is over-estimated.
Need good filtering to remove individual noise. Common mode (? TODO) noise
can be useful for AC resistance measurement, so might want to use a multi-variate
filter (TODO name? ref?) 


### range 
