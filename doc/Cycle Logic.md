*these are my personal notes, maybe you'll find something useful*

# SoC range
Avoid high SoC cycling, e.g. when only using little energy overnight, the battery would cycle between 100%-95%.
- Keep SoC within lower and upper range. Lower SoC might be normally 0%

# Threshold switch
Consume excess solar energy e.g. water heater, absorber fridge
Solar power turns on switch. The threshold can depend on SoC
- Higher SoC, lower threshold (SoC 100% -> 0 W threshold)
- Avoid using battery energy

# Conditional Loads
- If EV is charging, disable other loads


# Literature
According to https://www.sciencedirect.com/science/article/abs/pii/S037877531730143X
cycling LiFePo4 between 55 and 45 shows stronger aging. 50% DoD (SoC 75-25) appears to be better.

https://www.mdpi.com/1996-1073/14/6/1732
