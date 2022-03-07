import asyncio
from functools import partial

shutdown = False

async def fetch_loop(fn, max_errors=4, period=.1):
    num_errors_row =0
    while not shutdown:
        try:
            await fn()
            num_errors_row = 0
        except Exception as e:
            num_errors_row += 1
            #logger.error('Error (num %d) reading BMS: %s', num_errors_row, e)
            # logger.error('Stack: %s', traceback.format_exc())
            if num_errors_row > max_errors:
                # logger.warn('too many errors, abort')
                break
        await asyncio.sleep(period)



async def main():
    def ap(s):
        print(s)
    async def t0():
        print('t0')
    async def t1():
        print('t1')
    tasks = [t0,t1, partial(ap, 'ap')]
    loops = [fetch_loop( fn) for fn in tasks]
    await asyncio.wait(loops, return_when='FIRST_COMPLETED')


asyncio.run(main())