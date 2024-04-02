import random
import string


def random_str(n=12):
    return ''.join(random.choice(string.ascii_letters + string.digits) for _ in range(n))


def is_hashable(obj):
    # noinspection PyBroadException
    try:
        hash(obj)
        return True
    except Exception:
        return False



def to_hashable(obj, id_types=()):
    if is_hashable(obj):
        return obj  # , type(obj)

    if isinstance(obj, set):
        obj = sorted(obj)
    elif isinstance(obj, dict):
        obj = sorted(obj.items())

    if isinstance(obj, (list, tuple)):
        return tuple(map(to_hashable, obj))

    if id_types and isinstance(obj, id_types):
        return type(obj), id(obj)

    raise ValueError(
        "%r can not be hashed. Try providing a custom key function."
        % obj)