

Query1:
```
    
SELECT value as A FROM "home_assistant"."autogen"."A"
    WHERE time >= 1691587662145ms and time <= 1691588646345ms and "entity_id" =~ /.+_soc_current/ group by entity_id
    
SELECT value as V FROM "home_assistant"."autogen"."V"
    WHERE time >= 1691587662145ms and time <= 1691588646345ms and "entity_id" =~ /bat_caravan_cell_voltages_[0-9]+/) AND t GROUP BY  "entity_id" 
```


q2
* with tracking & 50hz inverter noise
* /batmon?orgId=1&from=1694257914567&to=1694258109389



# Local InfluxDB under mac
```
brew install influxdb@1
/usr/local/opt/influxdb@1/bin/influxd

curl -G 'http://localhost:8086/query' --data-urlencode "db=home_assistant" --data-urlencode "q=SELECT * FROM \"V\" " -H "Accept: application/csv" >  V.csv

```