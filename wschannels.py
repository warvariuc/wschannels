import asyncio
import json
import logging.config
import time

import aiohttp.web
import aiohttp.errors
import aiohttp_jinja2
import jinja2
import pymongo.errors


logging.config.dictConfig({
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'detailed': {
            'format': '[%(asctime)s] %(levelname)s %(name)s:%(lineno)s %(message)s',
            'datefmt': "%d/%b/%Y %H:%M:%S",
        },
    },
    'handlers': {
        'stderr': {
            'class': 'logging.StreamHandler',
            'formatter': 'detailed',
        },
    },
    'loggers': {
        '': {
            'level': 'INFO',
            'handlers': ['stderr'],
        },
    },
})
logger = logging.getLogger(__name__)


@aiohttp_jinja2.template('home.html')
def home_handler(request):
    return {}


@aiohttp_jinja2.template('channel.html')
def channel_handler(request):
    return {'channel': request.match_info['channel']}


class Channels(dict):
    """Channels and web-sockets registered on them.
    {channel_name: ({subchannel_name: {...}}, set([socket, ...])), ...}
    """
    _sockets = {}  # {socket: channel_path, ...}

    def __init__(self, *args, name='', parent=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.name = name
        self.parent = parent
        self.sockets = set()

    def __repr__(self):
        return 'Channel {} with {} sockets'.format(self.name, len(self.sockets))

    def get_subchannels(self, channel_path):
        channels = self
        for channel in channel_path.split('/'):
            if channel:
                try:
                    channels = channels[channel]
                except KeyError:
                    channels = self.setdefault(channel, self.__class__(name=channel, parent=self))
        return channels

    def add_socket(self, socket, channel):
        assert socket not in self._sockets
        channels = self.get_subchannels(channel)
        channels.sockets.add(socket)
        self._sockets[socket] = channels

    def _gc(self):
        if not self.sockets and self.parent:
            del self.parent[self.name]
            # logger.info('Removed channel `%s`', self.name)
            self.parent._gc()

    def remove_socket(self, socket):
        # discard invalid connection
        # logger.info('Discarding invalid web-socket connection')
        channels = self._sockets.pop(socket)
        channels.sockets.discard(socket)
        channels._gc()

    def get_sockets(self, channel='', subchannels=False):
        """Get all web-sockets from the given channel.
        Args:
            channel (str): channel path
            subchannels (bool): whether to include sockets from subchannels
        Returns:
            iterable: of web-sockets
        """
        channels = self.get_subchannels(channel)
        yield from tuple(channels.sockets)
        if subchannels:
            for subchannel in channels.values():
                yield from subchannel.get_sockets(subchannels=True)


class WSChannelServer():
    """Web-socket server.
    """
    def __init__(self, db, event_loop):
        # db.drop_collection('messages')
        try:
            # http://docs.mongodb.org/manual/core/capped-collections/
            db.create_collection(
                'messages', capped=True, size=2**20, max=1000, autoIndexId=False)
        except pymongo.errors.CollectionInvalid:
            pass
        self.messages = db['messages']

        self.channels = Channels()
        self.event_loop = event_loop
        self.event_loop.call_soon(
            lambda loop: loop.run_in_executor(None, self.get_messages), event_loop)

    @asyncio.coroutine
    def ws_handler(self, request):
        channel = request.match_info['channel']
        ws = aiohttp.web.WebSocketResponse()
        ws.start(request)
        self.channels.add_socket(ws, channel)

        while not ws.closed:
            msg = yield from ws.receive()

            if msg.tp == aiohttp.MsgType.text:
                # logger.info('Received a message on channel `%s`: %s', channel, msg.data)
                self.publish_message(channel, msg.data)
            # elif msg.tp == aiohttp.MsgType.close:
            #     logger.info('websocket connection closed')
            # elif msg.tp == aiohttp.MsgType.error:
            #     logger.info('ws connection closed with exception %s', ws.exception())

        self.channels.remove_socket(ws)

        return ws

    def publish_message(self, channel, message):
        """Publish a new message to the queue, so that other servers can see it and notify their
        clients.
        """
        # XXX: ObjectId might not be sequential.
        # ensure that _id is generated on the server
        self.messages.database.eval('db.messages.insert({})'.format(json.dumps(
            {'data': message, 'channel': channel}, ensure_ascii=False)))

    def publish_message_handler(self, request):
        """Publish given messages on the given channels. The POST body should be in form of
        [{'channel': channel, 'message': message}, ...]
        """
        data = yield from request.json()
        for _data in data:
            self.publish_message(_data['channel'], _data['message'])
            yield  # give control to the loop
        return aiohttp.web.Response(text='ok')

    def get_messages(self):
        """Get new messages from the shared queue.
        """
        # get the most recent message id
        last_id = None
        for messsage in self.messages.find().sort([('$natural', -1)]):
            last_id = messsage['_id']
            break

        while not self.event_loop.is_closed():
            query = {} if last_id is None else {'_id': {'$gt': last_id}}
            cursor = self.messages.find(query)
            cursor.add_option(pymongo.CursorType.TAILABLE_AWAIT)

            for message in cursor:
                last_id = message['_id']
                # print(message)
                self.send_message(message['channel'], message['data'])
            time.sleep(0.1)

    def send_message(self, channel, message):
        """Asynchronously send a message to web-socket clients handled by this worker on the given
        channel. If channel path ends with '/' the message is sent to all subchannels too.
        """
        for ws in self.channels.get_sockets(channel, subchannels=channel.endswith('/')):
            try:
                ws.send_str(message)
            except aiohttp.errors.DisconnectedError:
                self.channels.remove_socket(ws)


mongo_client = pymongo.MongoClient('mongodb://localhost:27017/')

event_loop = asyncio.get_event_loop()
ws_server = WSChannelServer(mongo_client['wschannels'], event_loop)
app = aiohttp.web.Application()
aiohttp_jinja2.setup(app, loader=jinja2.FileSystemLoader('templates'))

app.router.add_route('GET', '/', home_handler)
app.router.add_route('GET', r'/channel/{channel:.+}', channel_handler)
app.router.add_route('GET', r'/ws/{channel:.+}', ws_server.ws_handler)
app.router.add_route('POST', r'/publish', ws_server.publish_message_handler)


if __name__ == '__main__':
    handler = app.make_handler()
    f = event_loop.create_server(handler, '127.0.0.1', 5000)
    srv = event_loop.run_until_complete(f)

    print('Serving on', srv.sockets[0].getsockname())

    import pympler.tracker
    import pympler.process
    pmi = pympler.process.ProcessMemoryInfo()
    print("\nVirtual size (bytes): {:,}".format(pmi.vsz))
    print("Resident set size (bytes): {:,}".format(pmi.rss))
    tr = pympler.tracker.SummaryTracker()

    try:
        event_loop.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        event_loop.run_until_complete(handler.finish_connections(1.0))
        srv.close()
        event_loop.run_until_complete(srv.wait_closed())
        event_loop.run_until_complete(app.finish())
    event_loop.close()

    pmi = pympler.process.ProcessMemoryInfo()
    print("\nVirtual size (bytes): {:,}".format(pmi.vsz))
    print("Resident set size (bytes): {:,}".format(pmi.rss))
    tr.print_diff()
