# Copyright 2018, Erlang Solutions Ltd, and S2HC Sweden AB
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import asyncio
import logging
from typing import Set, Dict, List, Tuple, Any

from pyrlang import node_db
from pyrlang.match import Match
from pyrlang import errors
from term.atom import Atom
from term.pid import Pid
from term.reference import Reference

LOG = logging.getLogger(__name__)


class Process:
    """ Implements Erlang process semantic and lifetime.
        Registers itself in the process registry, can receive and send messages.
        To optionally register self with a name, call
        ``node.register_name(self, term.Atom('fgsfds'))``

        Subclass the Process to run your logic in its ``_loop() -> bool``
        function or to handle incoming messages via
        ``handle_one_inbox_message(self, msg)``.

        .. note::
            Only a ``Process`` can serve as a target for sending messages, for
            linking and monitoring. You do not need to create a Process for simple
            one-way interactions with remote Erlang nodes.
    """

    # if we want receive to always match and route in a specific way,
    # we can put it here (should be list of `pyrlang.match.Match` object
    _match = None

    def __init__(self, passive: bool = False) -> None:
        """ Create a process and register itself. Pid is generated by the node
            object.
            :arg passive: Passive process has to handle their ``self.inbox_``
                from the user code. Active process will run the poll loop on the
                inbox and call ``self.handle_one_inbox_message`` for every
                incoming message.
        """
        self.state='init'
        self.passive_ = passive  # type: bool
        """ Having ``passive=True`` will only wake up this
            ``Process`` when a message arrives, to handle it, otherwise it will
            not get any CPU time for any empty polling loops. Having
            ``passive=False`` will run
            :py:func:`~Pyrlang.process.Process.process_loop``
            polling inbox.
        """

        node_obj = node_db.get()

        self.node_name_ = node_obj.node_name_  # type: str
        """ Convenience field to see the Node  """

        self.inbox_ = asyncio.Queue()
        """ Message queue. Messages are detected by the ``_run``
            loop and handled one by one in ``handle_one_inbox_message()``. 
        """
        self.__tmp_inbox = asyncio.Queue() # used for selective receives

        self.pid_ = node_obj.register_new_process(self)
        """ Process identifier for this object. Remember that when creating a 
            process, it registers itself in the node, and this creates a
            reference. 
            References prevent an object from being garbage collected.
            To destroy a process, get rid of this extra reference by calling
            ``exit()`` and telling it the cause of its death.
        """

        self.is_exiting_ = False

        self._monitored_by = dict()  # type: Dict[Reference, Pid]
        """ Who monitors us. Either local or remote processes. """

        self._monitors = dict()  # type: Dict[Reference, Pid]
        """ Who we monitor. NOTE: For simplicity multiple monitors of same 
            target are not implemented. """

        self._links = set()  # type: Set[Pid]
        """ Bi-directional linked process pids. Each linked pid pair is unique
            hence using a set to store them. """

        self._signals = asyncio.Queue()
        """ Exit (and maybe later other) signals are placed here and handled
            at safe moments of time between handling messages. """

        if not self._match:
            self._match = Match()
        LOG.debug("Spawned process %s", self.pid_)
        if not self.passive_:
            event_loop = asyncio.get_event_loop()
            self._run_task = event_loop.create_task(self.process_loop())
            #self._signal_task = event_loop.create_task(self.handle_signals())
            event_loop.create_task(self.run_wrapper())

    def __repr__(self):
        return "pyrlang.Process: {}".format(self.pid_)

    def __etf__(self):
        """allow process objects to be put into messages to erlang"""
        return self.pid_

    async def run_wrapper(self):
        """
        The outer task that awaits and supervise the run and signal tasks
        :return:
        """
        try:
            # await asyncio.gather(self._run_task, self._signal_task)
            await self._run_task
        except asyncio.CancelledError as e:
            if self.is_exiting_:
                # this is expected when exiting
                return
            else:
                raise e
        except Exception as e:
            LOG.exception("%s got an exception in runtime", self)
            print("caught exception {}".format(e))
        else:
            LOG.debug("%s finished without any issues", self)
            return

        # understand what happened and cleanup as good as possible
        self._run_task.cancel()
        # self._signal_task.cancel()
        self._on_exit_signal(Atom("unhandledexception"))

    async def process_loop(self):
        """ Polls inbox in an endless loop.
            .. note::
                This will not be executed if the process was constructed with
                ``passive=True`` (the default). Passive processes should read
                their inbox directly from ``self.inbox_``.
        """
        while not self.is_exiting_:
            # If any messages have been handled recently, do not sleep
            # Else if no messages, sleep for some short time
            msg = await self.receive()
            if msg:
                self.handle_one_inbox_message(msg)

        LOG.debug("Process %s process_loop stopped", self.pid_)

    async def receive(self, match=None, timeout=None, timeout_callback=None):
        if not timeout:
            return await self._receive(match)

        # timeout functionality
        try:
            return await asyncio.wait_for(self._receive(match), timeout)
        except asyncio.TimeoutError:
            if not callable(timeout_callback):
                emsg = "receive in {} timed out".format(self)
                raise errors.ProcessTimeoutError(emsg)
            return timeout_callback()

    async def _receive(self, match=None):
        LOG.debug("Starting receive")
        # if no override use default
        if not match:
            match = self._match
        if not self.__tmp_inbox.empty():
            raise ValueError("temporary inbox not empty")
        while True:
            msg = await self.inbox_.get()
            LOG.debug("\n\ngot inbox {}, {}, {}".format(self, match, msg))
            matched_pattern = match(msg)
            if not matched_pattern:
                self.__tmp_inbox.put_nowait(msg)
                self.inbox_.task_done()  # not sure we can say done this early
                continue
            self._cleanup_inbox()
            return matched_pattern.run(msg)

    def _cleanup_inbox(self):
        """
        move data around in inbox and tmp inbox
        :return:
        """
        while not self.inbox_.empty():
            self.__tmp_inbox.put_nowait(self.inbox_.get_nowait())
            self.inbox_.task_done()
        while not self.__tmp_inbox.empty():
            self.inbox_.put_nowait(self.__tmp_inbox.get_nowait())
            self.__tmp_inbox.task_done()

    async def handle_signals(self):
        """ Called from Node if the Node knows that there's a signal waiting
            to be handled. """
        # Signals defer exiting a process while doing something important
        (_exit, reason) = await self._signals.get()
        self._on_exit_signal(reason)


    def handle_inbox(self) -> int:
        """ Do not override `handle_inbox`, instead go for
            `handle_one_inbox_message`
            :returns: How many messages have been handled
        """
        n_handled = 0
        try:
            while True:
                msg = self.inbox_.get_nowait()

                n_handled += 1
                self.handle_one_inbox_message(msg)
        except asyncio.QueueEmpty:
            return n_handled

    def handle_one_inbox_message(self, msg):
        """ Override this method to handle new incoming messages. """
        LOG.error("%s: Unhandled msg %s" % (self.pid_, msg))
        pass

    def deliver_message(self, msg):
        """ Places message into the inbox, or delivers it immediately to a
            handler (if process is ``passive``). """
        if self.passive_:
            self.handle_one_inbox_message(msg)
        else:
            self.inbox_.put_nowait(msg)

    def add_link(self, pid):
        """ Links pid to this process.
            Please use Node method :py:meth:`~pyrlang.node.Node.link` for proper
            linking.
        """
        self._links.add(pid)

    def remove_link(self, pid):
        """ Unlinks pid from this process.
            Please use Node method :py:meth:`~pyrlang.node.Node.unlink` for
            proper unlinking.
        """
        self._links.remove(pid)

    def exit(self, reason=None):
        """ Marks the object as exiting with the reason, informs links and
            monitors and unregisters the object from the node process
            dictionary.
        """
        LOG.info("%s got exit call, sending signal", self)
        self._signals.put_nowait(('exit', reason))
        self.get_node().signal_wake_up(self.pid_)

    def destroy(self):
        """
        kills tasks running
        :return:
        """
        LOG.warning("destroying %s", self)
        self._run_task.cancel()
        # self._signal_task.cancel()

    def _on_exit_signal(self, reason):
        """ Internal function triggered between message handling. """
        if reason is None:
            reason = Atom('normal')

        self._run_task.cancel()
        self.is_exiting_ = True
        self._trigger_monitors(reason)
        self._trigger_links(reason)

        n = node_db.get(self.node_name_)
        n.on_exit_process(self.pid_, reason)

    def get_node(self):
        """ Finds current node from global nodes dict by ``self.node_name_``.
            A convenient way to access the node which holds the current process.
            :rtype: pyrlang2.node.Node
        """
        return node_db.get(self.node_name_)

    def _trigger_monitors(self, reason):
        """ On process exit inform all monitor owners that monitor us about the
            exit reason.
        """
        node = self.get_node()
        for (monitor_ref, monitor_owner) in self._monitored_by.items():
            down_msg = (Atom("DOWN"),
                        monitor_ref,
                        Atom("process"),
                        self.pid_,
                        reason)
            node.send_nowait(sender=self.pid_,
                             receiver=monitor_owner,
                             message=down_msg)

    def _trigger_links(self, reason):
        """ Pass any exit reason other than 'normal' to linked processes.
            If Reason is 'kill' it will be converted to 'killed'.
        """
        if not isinstance(reason, Atom):
            msg = "reason must be Atom, got {}".format(type(reason))
            raise AttributeError(msg)

        if reason == 'normal':
            return
        elif reason == 'kill':
            reason = Atom('killed')

        node = self.get_node()
        for link in self._links:
            # For local pids, just forward them the exit signal
            node.send_link_exit_notification(sender=self.pid_,
                                             receiver=link,
                                             reason=reason)

    def add_monitor(self, pid: Pid, ref: Reference):
        """ Helper function. To monitor a process please use Node's
            :py:meth:`~pyrlang.node.Node.monitor_process`.
        """
        self._monitors[ref] = pid

    def add_monitored_by(self, pid: Pid, ref: Reference):
        """ Helper function. To monitor a process please use Node's
            :py:meth:`~pyrlang.node.Node.monitor_process`.
        """
        self._monitored_by[ref] = pid

    def remove_monitor(self, pid: Pid, ref: Reference):
        """ Helper function. To demonitor a process please use Node's
            :py:meth:`~pyrlang.node.Node.demonitor_process`.
        """
        existing = self._monitors.get(ref, None)
        if existing == pid:
            del self._monitors[ref]

    def remove_monitored_by(self, pid: Pid, ref: Reference):
        """ Helper function. To demonitor a process please use Node's
            :py:meth:`~pyrlang.node.Node.demonitor_process`.
        """
        existing = self._monitored_by.get(ref, None)
        if existing == pid:
            del self._monitored_by[ref]
