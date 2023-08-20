

Query1:
```
    
SELECT value as A FROM "home_assistant"."autogen"."A"
    WHERE time >= 1691587662145ms and time <= 1691588646345ms and "entity_id" =~ /.+_soc_current/ group by entity_id
    
SELECT value as V FROM "home_assistant"."autogen"."V"
    WHERE time >= 1691587662145ms and time <= 1691588646345ms and "entity_id" =~ /bat_caravan_cell_voltages_[0-9]+/) AND t GROUP BY  "entity_id" 
```