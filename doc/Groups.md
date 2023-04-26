*This feature is experimental*

# Groups

With battery groups, you can tell batmon that multiple BMSs are connected together. 
Batmon will merge readings and switches of the BMSs.

To create a group, add another device entry in the options.

```
- address: "jk_bms1,jk_bms2"
  type: group_parallel
  alias: battery_group1
```

Refer to the member BMSs in the `address` property, name/alias separated by `,`.

Set `type` to `group_parallel`. Serial battery strings are not implemented yet.

## Parallel Batteries

If you have two Batteries in parallel you can use a group to combine SoC & power readings.
Also state changes for charge & discharge switches are jointly applied to all BMS in the group.

Example:

```
- address: C8:47:8C:E4:55:E5
  type: jk
  alias: jk_bms1
- address: E8:57:8C:E4:45:34
  type: jk
  alias: jk_bms2
- address: "jk_bms1,jk_bms2"
  type: group_parallel
  alias: battery_group1
```

## Serial Batteries

Not yet implemented.

## BMS Swapping

You can create a group with a single BMS to ease the process of BMS swapping.

Imagine you have all your Battery Dashboards and automations set up.
At some point in the future you might want to replace the BMS with a different one, with a different name/alias.
The work of changing all the entity names in Home Assistant can be tedious.
Use a group as a *virtual proxy* BMS to map entities to another *physical* BMS. 
