
```

HA caravan rpi influx


daly22: ("2022-01-05", "2023-11-29")
jbd22: (2022-01-23 - 2023-04-17)
jk22 (2022-08-05 -- -08-14) & (2023-04-12 -- 2023-10-25)


http://localhost:3000/api/hassio_ingress/YlvS1K32yJnjFjPC5JM5H4BdrxqM6EqNaABWSoAVECU/goto/MmoZsqVSR?orgId=1

SELECT mean("value") AS "mean_value"
    FROM "home_assistant"."autogen"."A"
        WHERE time > :dashboardTime: AND time < :upperDashboardTime:
            AND "entity_id"='daly_bms_soc_current'
    GROUP BY time(:interval:) FILL(null)
    
  # same  jbd_bms_soc_current}
  
  
SELECT mean("value") AS "mean_value" FROM "home_assistant"."autogen"."°C" WHERE time > :dashboardTime: AND time < :upperDashboardTime:
AND ("entity_id"='daly_bms_temperatures_1' 
    OR "entity_id"='jbd_bms_temperatures_1'
     OR "entity_id"='jbd_bms_temperatures_2') GROUP BY time(:interval:) FILL(null)

SELECT mean("value") AS "mean_value" FROM "home_assistant"."autogen"."%" WHERE time > :dashboardTime: AND time < :upperDashboardTime: AND "entity_id"='daly_bms_soc_soc_percent' GROUP BY time(:interval:) FILL(null)
SELECT mean("value") AS "mean_value" FROM "home_assistant"."autogen"."V" WHERE time > :dashboardTime: AND time < :upperDashboardTime: AND "entity_id"='daly_bms_cell_voltages_1' GROUP BY time(:interval:) FILL(null)
```