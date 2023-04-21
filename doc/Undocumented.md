*This feature is experimental*

# Groups

With groups, you can merge readings and switches of multiple BMS.

## Parallel Batteries

If you have two Batteries in parallel you can use a group to combine SoC & power readings.
Also state changes for charge & discharge switches are jointly applied to all BMS in the group.

## BMS Swapping

You can create a group with a single BMS to ease the process of BMS swapping.

Imagine you have all your Battery Dashboards and automations set up.
At some point in the future you might want to replace the BMS with a different one, with a different name/alias.
The work of changing all the entity names in Home Assistant can be tedious.
Use a group as a "virtual proxy BMS" to easily map entities to another physical BMS. 
