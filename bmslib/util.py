import logging
import os
import random
import string
import time


class dotdict(dict):
    def __getattr__(self, attr):
        try:
            return self[attr]
        except KeyError as e:
            raise AttributeError(e)

    __setattr__ = dict.__setitem__
    __delattr__ = dict.__delitem__
    # __hasattr__ = dict.__contains__


def get_logger(verbose=False):
    # log_format = '%(asctime)s %(levelname)-6s [%(filename)s:%(lineno)d] %(message)s'
    log_format = '%(asctime)s %(levelname)s [%(module)s] %(message)s'
    if verbose:
        level = logging.DEBUG
    else:
        level = logging.INFO

    logging.basicConfig(level=level, format=log_format, datefmt='%H:%M:%S')
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)

    return logger


def dict_to_short_string(d: dict):
    return '(' + ','.join(f'{k}={v}' for k, v in d.items() if v is not None) + ')'


def to_hex_str(data):
    return " ".join(map(lambda b: hex(b)[2:], data))


def exit_process(is_error=True, delayed=False):
    from threading import Thread
    import _thread
    status = 1 if is_error else 0
    Thread(target=lambda: (time.sleep(3), _thread.interrupt_main()), daemon=True).start()
    Thread(target=lambda: (time.sleep(6), os._exit(status)), daemon=True).start()
    if not delayed:
        import sys
        sys.exit(status)


def _id_generator(size=6, chars=string.ascii_uppercase + string.ascii_lowercase + string.digits):
    return ''.join(random.choice(chars) for _ in range(size))


def sid_generator(n=2):
    assert n >= 2
    return _id_generator(n-1, string.ascii_lowercase + string.ascii_uppercase) + _id_generator(1, string.digits)

