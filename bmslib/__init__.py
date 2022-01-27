
class FuturesPool:

    def __init__(self):
        self.futures = {}

    def add(self, name):
        self.futures[name] = None