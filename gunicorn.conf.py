import os


HERE = os.path.dirname(os.path.abspath(__file__))
RUN = os.path.join(HERE, "run")

bind = "127.0.0.1:5000"
worker_class = 'aiohttp.worker.GunicornWebWorker'
workers = 1
preload_app = False
chdir = HERE
pythonpath = HERE
daemon = False  # supervisord requires that the child process runs in foreground
pidfile = os.path.join(RUN, "wschannels.pid")
accesslog = os.path.join(RUN, "wschannels.access.log")
errorlog = os.path.join(RUN, "wschannels.error.log")
proc_name = 'wschannels (%s)' % HERE
