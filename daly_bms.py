import struct
import time
import math
import logging

from error_codes import ERROR_CODES


class DalyBMS:
    def __init__(self, request_retries=3, address=4, logger=None):
        """

        :param request_retries: How often read requests should get repeated in case that they fail (Default: 3).
        :param address: Source address for commands sent to the BMS (4 for RS485, 8 for UART/Bluetooth)
        :param logger: Python Logger object for output (Default: None)
        """
        self.status = None
        if logger:
            self.logger = logger
        else:
            self.logger = logging.getLogger(__name__)
        self.request_retries = request_retries
        self.address = address  # 4 = USB, 8 = Bluetooth

    def connect(self, device):
        """
        Connect to a serial device

        :param device: Serial device, e.g. /dev/ttyUSB0
        """

        import serial

        self.serial = serial.Serial(
            port=device,
            baudrate=9600,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.5,
            xonxoff=False,
            writeTimeout=0.5
        )
        self.get_status()

    @staticmethod
    def _calc_crc(message_bytes):
        """
        Calculate the checksum of a message

        :param message_bytes: Bytes for which the checksum should get calculated
        :return: Checksum as bytes
        """
        return bytes([sum(message_bytes) & 0xFF])

    def _format_message(self, command, extra=""):
        """
        Takes the command ID and formats a request message

        :param command: Command ID ("90" - "98")
        :return: Request message as bytes
        """
        # 95 -> a58095080000000000000000c2
        message = "a5%i0%s08%s" % (self.address, command, extra)
        message = message.ljust(24, "0")
        message_bytes = bytearray.fromhex(message)
        message_bytes += self._calc_crc(message_bytes)
        self.logger.debug("w %s" % message_bytes.hex())
        return message_bytes

    def _read_request(self, command, extra="", max_responses=1, return_list=False):
        """
        Sends a read request to the BMS and reads the response. In case it fails, it retries 'max_responses' times.

        :param command: Command ID ("90" - "98")
        :param max_responses: For how many response packages it should wait (Default: 1).
        :return: Request message as bytes or False
        """
        response_data = None
        x = None
        for x in range(0, self.request_retries):
            response_data = self._read(
                command=command,
                extra=extra,
                max_responses=max_responses,
                return_list=return_list)
            if not response_data:
                self.logger.debug("%x. try failed, retrying..." % (x + 1))
                time.sleep(0.2)
            else:
                break
        if not response_data:
            self.logger.error('%s failed after %s tries' % (command, x + 1))
            return False
        return response_data

    def _read(self, command, extra="", max_responses=1, return_list=False):
        self.logger.debug("-- %s ------------------------" % command)
        if not self.serial.isOpen():
            self.serial.open()
        message_bytes = self._format_message(command, extra=extra)

        # clear all buffers, in case something is left from a previous command that failed
        self.serial.reset_input_buffer()
        self.serial.reset_output_buffer()

        if not self.serial.write(message_bytes):
            self.logger.error("serial write failed for command" % command)
            return False
        x = 0
        response_data = []
        while True:
            b = self.serial.read(13)
            if len(b) == 0:
                self.logger.debug("%i empty response for command %s" % (x, command))
                break
            self.logger.debug("%i %s %s" % (x, b.hex(), len(b)))
            x += 1
            response_crc = self._calc_crc(b[:-1])
            if response_crc != b[-1:]:
                self.logger.debug("response crc mismatch: %s != %s" % (response_crc.hex(), b[-1:].hex()))
            header = b[0:4].hex()
            # todo: verify  more header fields
            if header[4:6] != command:
                self.logger.debug("invalid header %s: wrong command (%s != %s)" % (header, header[4:6], command))
                continue
            data = b[4:-1]
            response_data.append(data)
            if x == max_responses:
                break

        if return_list or len(response_data) > 1:
            return response_data
        elif len(response_data) == 1:
            return response_data[0]
        else:
            return False

    def get_soc(self, response_data=None):
        # SOC of Total Voltage Current
        if not response_data:
            response_data = self._read_request("90")
        if not response_data:
            return False

        parts = struct.unpack('>h h h h', response_data)
        voltage = parts[0] / 10
        current = (parts[2] - 30000) / 10  # negative=charging, positive=discharging
        data = {
            "total_voltage": voltage,
            # "x_voltage": parts[1] / 10, # always 0
            "current": current,
            "soc_percent": parts[3] / 10,
            "power": round(voltage * current, 1),
        }
        return data

    def get_cell_voltage_range(self, response_data=None):
        # Cells with the maximum and minimum voltage
        if not response_data:
            response_data = self._read_request("91")
        if not response_data:
            return False

        parts = struct.unpack('>h b h b 2x', response_data)
        data = {
            "highest_voltage": parts[0] / 1000,
            "highest_cell": parts[1],
            "lowest_voltage": parts[2] / 1000,
            "lowest_cell": parts[3],
        }
        return data

    def get_temperature_range(self, response_data=None):
        # Temperature in degrees celsius
        if not response_data:
            response_data = self._read_request("92")
        if not response_data:
            return False
        parts = struct.unpack('>b b b b 4x', response_data)
        data = {
            "highest_temperature": parts[0] - 40,
            "highest_sensor": parts[1],
            "lowest_temperature": parts[2] - 40,
            "lowest_sensor": parts[3],
        }
        return data

    def get_mosfet_status(self, response_data=None):
        # Charge/discharge, MOS status
        if not response_data:
            response_data = self._read_request("93")
        if not response_data:
            return False
        # todo: implement
        self.logger.debug(response_data.hex())

        parts = struct.unpack('>b ? ? B l', response_data)

        if parts[0] == 0:
            mode = "stationary"
        elif parts[0] == 1:
            mode = "charging"
        else:
            mode = "discharging"

        data = {
            "mode": mode,
            "charging_mosfet": parts[1],
            "discharging_mosfet": parts[2],
            # "bms_cycles": parts[3], unstable result
            "capacity_ah": parts[4] / 1000,
        }

        return data

    def get_status(self, response_data=None):
        if not response_data:
            response_data = self._read_request("94")
        if not response_data:
            return False

        parts = struct.unpack('>b b ? ? b h x', response_data)
        state_bits = bin(parts[4])[2:]
        state_names = ["DI1", "DI2", "DI3", "DI4", "DO1", "DO2", "DO3", "DO4"]
        states = {}
        state_index = 0
        for bit in reversed(state_bits):
            if len(state_bits) == state_index:
                break
            states[state_names[state_index]] = bool(int(bit))
            state_index += 1
        data = {
            "cells": parts[0],  # number of cells
            "temperature_sensors": parts[1],  # number of sensors
            "charger_running": parts[2],
            "load_running": parts[3],
            # "state_bits": state_bits,
            "states": states,
            "cycles": parts[5],  # number of charge/discharge cycles
        }
        self.status = data
        return data


    def _calc_num_responses(self, status_field, num_per_frame):
        if not self.status:
            self.logger.error("get_status has to be called at least once before calling get_cell_voltages")
            return False

        # each response message includes 3 cell voltages
        if self.address == 8:
            # via Bluetooth the BMS returns all frames, even when they don't have data
            if status_field == 'cell_voltages':
                max_responses = 16
            elif status_field == 'temperatures':
                max_responses = 3
            else:
                self.logger.error("unkonwn status_field %s" % status_field)
                return False
        else:
            # via UART/USB the BMS returns only frames that have data
            max_responses = math.ceil(self.status[status_field] / num_per_frame)
        return max_responses

    def _split_frames(self, response_data, status_field, structure):
        values = {}
        x = 1
        for response_bytes in response_data:
            parts = struct.unpack(structure, response_bytes)
            if parts[0] != x:
                self.logger.warning("frame out of order, expected %i, got %i" % (x, response_bytes[0]))
                continue
            for value in parts[1:]:
                values[len(values) + 1] = value
                if len(values) == self.status[status_field]:
                    return values
            x += 1

    def get_cell_voltages(self, response_data=None):
        if not response_data:
            max_responses = self._calc_num_responses(status_field="cells", num_per_frame=3)
            if not max_responses:
                return
            response_data = self._read_request("95", max_responses=max_responses, return_list=True)
        if not response_data:
            return False

        cell_voltages = self._split_frames(response_data=response_data, status_field="cells", structure=">b 3h x")
        for id in cell_voltages:
            cell_voltages[id] = cell_voltages[id] / 1000
        return cell_voltages

    def get_temperatures(self, response_data=None):
        # Sensor temperatures
        if not response_data:
            max_responses = self._calc_num_responses(status_field="temperature_sensors", num_per_frame=7)
            if not max_responses:
                return
            response_data = self._read_request("96", max_responses=max_responses, return_list=True)
        if not response_data:
            return False

        temperatures = self._split_frames(response_data=response_data, status_field="temperature_sensors",
                                          structure=">b 7b")
        for id in temperatures:
            temperatures[id] = temperatures[id] - 40
        return temperatures

    def get_balancing_status(self, response_data=None):
        # Cell balancing status
        if not response_data:
            response_data = self._read_request("97")
        if not response_data:
            return False
        self.logger.info(response_data.hex())
        bits = bin(int(response_data.hex(), base=16))[2:].zfill(48)
        self.logger.info(bits)
        cells = {}
        for cell in range(1, self.status["cells"] + 1):
            cells[cell] = bool(int(bits[cell * -1]))
        self.logger.info(cells)
        # todo: get sample data and verify result
        return {"error": "not implemented"}

    def get_errors(self, response_data=None):
        # Battery failure status
        if not response_data:
            response_data = self._read_request("98")
        if int.from_bytes(response_data, byteorder='big') == 0:
            return []

        byte_index = 0
        errors = []
        for b in response_data:
            if b == 0:
                byte_index += 1
                continue
            bits = bin(b)[2:]
            bit_index = 0
            for bit in reversed(bits):
                if bit == "1":
                    errors.append(ERROR_CODES[byte_index][bit_index])

                bit_index += 1

            self.logger.debug("%s %s %s" % (byte_index, b, bits))
            byte_index += 1
        return errors

    def get_all(self):
        return {
            "soc": self.get_soc(),
            "cell_voltage_range": self.get_cell_voltage_range(),
            # "temperature_range": self.get_temperature_range(), # broken? TODO
            "mosfet_status": self.get_mosfet_status(),
            "status": self.get_status(),
            "cell_voltages": self.get_cell_voltages(),
            "temperatures": self.get_temperatures(),
            "balancing_status": self.get_balancing_status(),
            "errors": self.get_errors()
        }

    def set_discharge_mosfet(self, on=True, response_data=None):
        if on:
            extra = "01"
        else:
            extra = "00"
        if not response_data:
            response_data = self._read_request("d9", extra=extra)
        if not response_data:
            return False
        self.logger.info(response_data.hex())
        # on response
        # 0101000002006cbe
        # off response
        # 0001000002006c44
