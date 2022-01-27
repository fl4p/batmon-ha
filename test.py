import asyncio

from bmslib.jbd import JbdBt


async def main():
    mac_address = 'A3161184-6D54-4B9E-8849-E755F10CEE12'
    mac_address = 'A4:C1:38:44:48:E7'
    # serial_service = '0000ff00-0000-1000-8000-00805f9b34fb'

    bms = JbdBt(mac_address, name='jbd')

    async with bms:
        voltages = await bms.fetch_voltages()
        print(voltages)
        # sample = await bms.fetch()
        # print(sample)



if __name__ == '__main__':
    asyncio.run(main())