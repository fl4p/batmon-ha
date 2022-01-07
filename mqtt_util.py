import json




def build_mqtt_hass_config_discovery(base, topic):
    # Instead of daly_bms should be here added a proper name (unique), like serial or something
    # At this point it can be used only one daly_bms system with hass discovery

    hass_config_topic = f'homeassistant/sensor/daly_bms/{base.replace("/", "_")}/config'
    hass_config_data = {}

    hass_config_data["unique_id"] = f'daly_bms_{base.replace("/", "_")}'
    hass_config_data["name"] = f'Daly BMS {base.replace("/", " ")}'

    if 'soc_percent' in base:
        hass_config_data["device_class"] = 'battery'
        hass_config_data["unit_of_measurement"] = '%'
    elif 'voltage' in base:
        hass_config_data["device_class"] = 'voltage'
        hass_config_data["unit_of_measurement"] = 'V'
    elif 'current' in base:
        hass_config_data["device_class"] = 'current'
        hass_config_data["unit_of_measurement"] = 'A'
    elif 'temperatures' in base:
        hass_config_data["device_class"] = 'temperature'
        hass_config_data["unit_of_measurement"] = '°C'
    else:
        pass

    hass_config_data["json_attributes_topic"] = f'{topic}{base}'
    hass_config_data["state_topic"] = f'{topic}{base}'

    hass_device = {
        "identifiers": ['daly_bms'],
        "manufacturer": 'Daly',
        "model": 'Currently not available',
        "name": 'Daly BMS',
        "sw_version": 'Currently not available'
    }
    hass_config_data["device"] = hass_device

    return hass_config_topic, json.dumps(hass_config_data)


def mqtt_single_out(client, topic, data, retain=False):
    # logger.debug(f'Send data: {data} on topic: {topic}, retain flag: {retain}')
    # print('mqtt: ' + topic, data)
    client.publish(topic, data, retain=retain)


def mqtt_iterator(client, result, topic, base='', hass=True):
    for key in result.keys():
        if type(result[key]) == dict:
            mqtt_iterator(client, result[key], topic, f'{base}/{key}', hass)
        else:
            if hass:
                # logger.debug('Sending out hass discovery message')
                topic_, output = build_mqtt_hass_config_discovery(f'{base}/{key}', topic=topic)
                mqtt_single_out(client, topic_, output, retain=True)

            if type(result[key]) == list:
                val = json.dumps(result[key])
            else:
                val = result[key]

            mqtt_single_out(client, f'{topic}{base}/{key}', val)
