import asyncio
import argparse

import aiohttp


stats = {}
# TODO: check that other clients from the same channel received the message


class WebSocketClient():

    def __init__(self, client_id, url):
        self.client_id = client_id
        self.url = url
        self.stats = stats.setdefault(self.client_id, {})
        self.errors = self.stats.setdefault('errors', {})
        self.message_count = 0
        self.messages = set()

    @asyncio.coroutine
    def run(self):
        try:
            yield from self._run()
        except Exception as exc:
            self.errors[str(exc)] = 1

    def _run(self):
        # print(self.client_id, 'Connecting')
        self.stats['handshake_started_at'] = event_loop.time()
        ws = yield from aiohttp.ws_connect(self.url)
        self.stats['handshake_done_at'] = event_loop.time()  # asyncio.get_event_loop().time()
        # print(self.client_id, 'Connected')

        # print(self.client_id, 'Sending the message')
        self.stats['message_sent_at'] = event_loop.time()
        ws.send_str(self.make_new_message())

        while not ws.closed:
            msg = yield from ws.receive()

            if msg.tp == aiohttp.MsgType.text:
                if msg.data in self.messages:
                    self.stats['echo_message_received_at'] = event_loop.time()
                    # print(self.client_id, 'Received the echo')
                    yield from ws.close()
                    break

            elif msg.tp == aiohttp.MsgType.closed:
                break
            elif msg.tp == aiohttp.MsgType.error:
                break
        # print(self.client_id, 'Closed')

    def make_new_message(self):
        self.message_count += 1
        message = 'Message #{:<3} from client #{:<5}'.format(self.message_count, self.client_id)
        self.messages.add(message)
        return message


def calculate_stats(array):
    """Return min, max, avg in the given array.
    """
    _count = 0
    _max = _sum = 0.0
    _min = float('inf')
    for number in array:
        _count += 1
        if _min is None or number < _min:
            _min = number
        if number > _max:
            _max = number
        _sum += number
    return {'min': _min, 'max': _max, 'avg': _sum / (_count or 1)}


class FutureSet:

    def __init__(self, maxsize, *, loop=None):
        self._set = set()
        self._loop = loop
        self._maxsize = maxsize
        self._waiters = []

    @asyncio.coroutine
    def add(self, item):
        assert asyncio.iscoroutine(item) or isinstance(item, asyncio.Future)
        if item in self._set:
            return
        while len(self._set) >= self._maxsize:
            waiter = asyncio.Future(loop=self._loop)
            self._waiters.append(waiter)
            # this will block until `waiter`s result is not set in `self._remove`
            yield from waiter
        item = asyncio.async(item, loop=self._loop)
        self._set.add(item)
        item.add_done_callback(self._remove)

    def _remove(self, item):
        assert item.done()
        self._set.remove(item)
        if len(self._set) < self._maxsize:
            while self._waiters:  # release all waiters
                self._waiters.pop().set_result(None)

    @asyncio.coroutine
    def wait(self):
        return asyncio.wait(self._set)


# TODO: try to use asyncio.BoundedSemaphore (http://stackoverflow.com/a/20722204/248296)

if __name__ == '__main__':

    arg_parser = argparse.ArgumentParser(description='WebSocket Channels test')
    arg_parser.add_argument('--clients', default=2000, type=int, help='Client count')
    arg_parser.add_argument('--wsserver', default='ws://127.0.0.1:5000/ws/{channel}',
                            help='WebSocket channels URL')
    args = arg_parser.parse_args()

    event_loop = asyncio.get_event_loop()
    event_loop.set_debug(True)
    start_time = event_loop.time()

    @asyncio.coroutine
    def make_clients(client_count, limit=0):
        futures = FutureSet(maxsize=limit)

        def print_clients(client_count):
            print('{} clients created. {} clients are active'.format(
                client_count, len(futures._set)))

            # print("%d clients were created\n" % (client_id + 1))

        for client_id in range(client_count):
            url = args.wsserver.format(
                channel='client/{client_id}'.format(client_id=client_id))
            client = WebSocketClient(client_id, url)
            if client_id % 100 == 0:
                print_clients(client_id)
            print_clients(client_id)
            yield from futures.add(client.run())
        print_clients(client_id)
        yield from futures.wait()

    try:
        event_loop.run_until_complete(make_clients(args.clients, 100))
    except KeyboardInterrupt:
        pass

    event_loop.close()

    print("Clients finished in {:.2f} sec.\n".format(event_loop.time() - start_time))

    print("""Handshake times (msec):
  Average: {avg:10.2f}    Min: {min:10.2f}    Max: {max:10.2f}
""".format_map(calculate_stats(
        (_stats['handshake_done_at'] - _stats['handshake_started_at']) * 1000
        for _stats in stats.values() if 'handshake_done_at' in _stats)))

    print("""Echo response times (msec):
  Average: {avg:10.2f}    Min: {min:10.2f}    Max: {max:10.2f}
""".format_map(calculate_stats(
        (_stats['echo_message_received_at'] - _stats['message_sent_at']) * 1000
        for _stats in stats.values() if 'echo_message_received_at' in _stats)))

    errors = {}
    for _stats in stats.values():
        for error_code, error_count in _stats['errors'].items():
            errors[error_code] = errors.get(error_code, 0) + error_count
    if errors:
        print("Errors:")
        for error_code, error_count in errors.items():
            print("  {}: {}".format(error_code, error_count))
