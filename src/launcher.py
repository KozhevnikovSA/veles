"""
Created on Feb 10, 2014

Workflow launcher (server/client/standalone).

@author: Kazantsev Alexey <a.kazantsev@samsung.com>
"""
import os
import sys
import argparse
import paramiko
import socket
from twisted.internet import reactor, threads

import client
import config
import graphics_server
import server
import logger
import units


class Launcher(logger.Logger):
    """Workflow launcher.

    Parameters:
    mode                  The operation mode ("slave" or "master").
    addr                  The partner's address (host:port).
                          These two parameters are used in case of empty
                          command line args.
    skip_web_status       If set to True, do not launch the status server.
                          It will not start if already running, anyway.
    slaves                The list of slaves to launch remotely.
    """
    def __init__(self, **kwargs):
        super(Launcher, self).__init__()
        parser = kwargs.get("parser")
        if parser is None:
            parser = argparse.ArgumentParser()
        parser.add_argument("-s", "--server_address", type=str, default="",
            help="Workflow will be launched in client mode "
            "and connected to the server at the specified address.")
        parser.add_argument("-l", "--listen_address", type=str, default="",
            help="Workflow will be launched in server mode "
            "and will accept client connections at the specified address.")
        parser.add_argument("-p", "--matplotlib_backend", type=str,
                            default="Qt4Agg",
            help="Matplotlib drawing backend.")
        self.args = parser.parse_args()

        self.args.server_address = self.args.server_address.strip()
        self.args.listen_address = self.args.listen_address.strip()
        config.matplotlib_backend = self.args.matplotlib_backend

        if (not self.args.server_address and
           "mode" in kwargs and kwargs["mode"] == "slave"):
            self.args.server_address = kwargs["addr"]
        if (not self.args.listen_address and
           "mode" in kwargs and kwargs["mode"] == "master"):
            self.args.listen_address = kwargs["addr"]

        self.args.skip_web_status = kwargs.get("skip_web_status", False)
        self.args.slaves = kwargs.get("slaves")

        if self.args.server_address:
            config.is_slave = True
            config.plotters_disabled = True

    @property
    def is_master(self):
        return True if self.args.listen_address else False

    @property
    def is_slave(self):
        return True if self.args.server_address else False

    @property
    def is_standalone(self):
        return not self.is_master and not self.is_slave

    def on_shutdown(self):
        self.stop()

    def initialize(self, workflow):
        workflow.thread_pool().register_on_shutdown(self.on_shutdown)
        if self.is_master:
            self.agent = client.Client(self.args.server_address, workflow)
        elif self.is_slave:
            self.agent = server.Server(self.args.listen_address, workflow)
            # Launch the status server if it's not been running yet
            if not self.args.skip_web_status:
                # Launch the status server if it's not been running yet
                self.launch_status()
            # Launch the nodes described in the configuration file/string
            nodes = self.args.slaves
            if nodes is not None:
                self.launch_nodes(nodes)
        else:
            self.agent = workflow
            self.graphics_server, self.graphics_client = \
                graphics_server.GraphicsServer.launch_pair()

    def run(self, daemonize=False):
        self.running = True
        try:
            if not self.is_standalone:
                return self.agent.run(daemonize=daemonize)
            else:
                darun = threads.deferToThreadPool(reactor,
                                                  self.agent.thread_pool(),
                                                  self.agent.run)
                darun.addCallback(self.stop)
                reactor.run()
        finally:
            self.running = False

    def stop(self, *args):
        if not self.running:
            return
        self.running = False
        if not self.is_standalone:
            self.agent.stop()
        elif not config.plotters_disabled:
            self.graphics_client.wait()
            reactor.stop()

    def launch_status(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            result = sock.connect_ex((config.web_status_host,
                                      config.web_status_port))
        if result != 0:
            self.info("Launching the web status server")
            self.launch_remote_program(
                config.web_status_host,
                os.path.abspath(os.path.join(config.this_dir,
                                             "web_status.py")))
        else:
            self.info("Discovered an already running web status server")

    def launch_nodes(self, nodes):
        if not "nodes" in self.options:
            return
        filtered_argv = []
        skip = False
        for i in range(1, len(sys.argv)):
            if filtered_argv[i] == "-l" or \
               filtered_argv[i] == "--listen_address":
                skip = True
            elif not skip:
                filtered_argv.append(sys.argv[i])
            else:
                skip = False
        filtered_argv.append("-s")
        host = self.args.listen_address[0:self.args.listen_address.index(':')]
        port = self.args.listen_address[len(host) + 1:]
        # No way we can send 'localhost' or empty host name to a slave.
        if not host or host == "0.0.0.0" or host == "localhost" or \
           host == "127.0.0.1":
            host = socket.gethostname()
        filtered_argv.append("%s:%s", (host, port))
        slave_args = " ".join(filtered_argv)
        self.debug("Slave args: %s", slave_args)
        for node in nodes:
            self.launch_remote_program(node,
                                       "%s %s" % (os.path.abspath(sys.argv[0]),
                                                  slave_args))

    def launch_remote_program(self, host, prog):
        self.debug("Launching \"%s\" on %s", prog, host)
        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(host, look_for_keys=True, timeout=0.1)
            client.exec_command(prog)
            client.close()
        except:
            self.exception()
