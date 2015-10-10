WebSocket channels
==================

The server is written for Python3 using AIOHTTP.

It's like a chat where rooms are called 'channels'.

For example your web-page listens for messages for the loggged in user on channel `/users/{user_id}`
You subscribe to (listen on) a channel and receive messages when someone posts messages to it.
For example the server has some notifications or updates to data.
You can send messages to subchannels. For example posting a message to channel `/users/` will
send the messages to clients listening on channel `/users` as well to clients listing on any
subchannel `/users/**/*`.

The client can also send messages to server and the message will be sent to all clients listening
on the same channel (including himself).

Start the server under Gunicorn:

    gunicorn wschannels:app --bind=localhost:5000 --worker-class=aiohttp.worker.GunicornWebWorker --workers=2

You can publish a message to a channel(s) using HTTP API:

    curl http://127.0.0.1:5000/publish --data '[{"channel": "test", "message": "111"}, {"channel": "test2", "message": "222"}]'

Or you can post the message directly into the MongoDB:

    # ensure that _id is generated on the server
    database.eval('db.messages.insert({})'.format(json.dumps(
        {'data': message, 'channel': channel}, ensure_ascii=False)))


There is simple demo web-interface to send-receive messages on a channel: http://127.0.0.1:5000

Sample Nginx config:

    upstream wschannels {
        server 127.0.0.1:5000;
    }
    
    server {
        ...
    
        location ~ /ws/(?<channel>.+) {
            proxy_pass http://wschannels/$channel;
            proxy_http_version 1.1;
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection "Upgrade";
        }
    }
