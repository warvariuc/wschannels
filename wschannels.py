import asyncio
import json
import logging.config
import time

import aiohttp.web
import aiohttp.errors
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


def render_template(template_name, context=None):
    template = jinja_env.get_template(template_name)
    text = template.render(context or {})
    response = aiohttp.web.Response(text=text, content_type='text/html')
    return response


def home_handler(request):
    response = render_template('home.html')
    return response


def channel_handler(request):
    response = render_template('channel.html', {'channel': request.match_info['channel']})
    return response


class Channels():
    """Channels and web-sockets registered on them.
    """
    def __init__(self, name):
        self.name = name
        self.sockets = set()
        self.subchannels = {}

    def __getitem__(self, name):
        """Get a sub-channel.
        """
        channel = self.subchannels.get(name)
        if channel is None:
            channel = self.__class__(self.name + '/' + name)
            self.subchannels[name] = channel
        return channel

    def add_socket(self, socket, channel):
        sockets = self
        for channel in channel.split('/'):
            if channel:
                sockets = sockets[channel]
        sockets.sockets.add(socket)

    def remove_socket(self, socket):
        # discard invalid connection
        # logger.info('Discarding invalid web-socket connection')
        self._discard(socket)

    def _discard(self, socket):
        self.sockets.discard(socket)
        for channel in tuple(self.subchannels.values()):
            channel._discard(socket)

    def get_sockets(self, channel='', subchannels=False):
        """Get all web-sockets from the given channel.
        Args:
            channel (str): channel path
            subchannels (bool): whether to include sockets from subchannels
        Returns:
            iterable: of web-sockets
        """
        channel_sockets = self
        for channel in channel.split('/'):
            if channel:
                channel_sockets = channel_sockets[channel]
        yield from tuple(channel_sockets.sockets)
        if subchannels:
            for subchannel in channel_sockets.subchannels.values():
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

        self.channels = Channels('')
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

jinja_env = jinja2.Environment(loader=jinja2.FileSystemLoader('templates'))
event_loop = asyncio.get_event_loop()
ws_server = WSChannelServer(mongo_client['wschannels'], event_loop)
app = aiohttp.web.Application()

app.router.add_route('GET', '/', home_handler)
app.router.add_route('GET', r'/channel/{channel:.+}', channel_handler)
app.router.add_route('GET', r'/ws/{channel:.+}', ws_server.ws_handler)
app.router.add_route('POST', r'/publish', ws_server.publish_message_handler)


if __name__ == '__main__':
    handler = app.make_handler()
    f = event_loop.create_server(handler, '127.0.0.1', 5000)
    srv = event_loop.run_until_complete(f)

    print('Serving on', srv.sockets[0].getsockname())

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
