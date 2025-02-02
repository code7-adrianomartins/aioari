#
# Copyright (c) 2013, Digium, Inc.
#

"""Async ARI client library.
"""

import json
import urllib
import aiohttp
import aioswagger11.client
import asyncio

from aioari.model import Repository
from aioari.model import Channel, Bridge, Playback, LiveRecording, StoredRecording, Endpoint, DeviceState, Sound

import logging
log = logging.getLogger(__name__)


class Client(object):
    """Async ARI Client object.

    :param base_url: Base URL for accessing Asterisk.
    :param http_client: HTTP client interface.
    """

    def __init__(self, base_url, http_client):
        self.base_url = base_url
        self.http_client = http_client
        self.app = None
        self.websockets = None
        url = urllib.parse.urljoin(base_url, "ari/api-docs/resources.json")
        self.swagger = aioswagger11.client.SwaggerClient(
            http_client=http_client, url=url)

    async def init(self, RepositoryFactory=Repository):
        await self.swagger.init()
        # Extract models out of the events resource
        events = [api['api_declaration']
                  for api in self.swagger.api_docs['apis']
                  if api['name'] == 'events']
        if events:
            self.event_models = events[0]['models']
        else:
            self.event_models = {}

        self.repositories = {
            name: Repository(self, name, api)
            for (name, api) in self.swagger.resources.items()}
        self.websockets = set()
        self.event_listeners = {}
        self.exception_handler = \
            lambda ex: log.exception("Event listener threw exception")

    def __getattr__(self, item):
        """Exposes repositories as fields of the client.

        :param item: Field name
        """
        repo = self.get_repo(item)
        if not repo:
            raise AttributeError(
                "'%r' object has no attribute '%s'" % (self, item))
        return repo

    async def close(self):
        """Close this ARI client.

        This method will close any currently open WebSockets, and close the
        underlying Swaggerclient.
        """
        unsubscribe = {
            'channel': '__AST_CHANNEL_ALL_TOPIC',
            'bridge': '__AST_BRIDGE_ALL_TOPIC',
            'endpoint': '__AST_ENDPOINT_ALL_TOPIC',
            'deviceState': '__AST_DEVICE_STATE_ALL_TOPIC'
        }
        unsubscribe_str = ','.join([('%s:%s' % (key, value)) for (key, value) in unsubscribe.items()])

        try:
            full_url = '%sari/applications/%s/subscription?eventSource=%s' % (self.base_url, self.app, unsubscribe_str)
            await self.http_client.request('delete', full_url)
        except Exception as ex:
            pass

        for ws in list(self.websockets):  # changes during processing
            try:
                host, port = self.get_peer_info(ws)
            except TypeError:
                # host, port = 'unknown', 'unknown'
                self.websockets.remove(ws)
                await ws.close()
                continue

            log.info('Successfully disconnected from ws://%s:%s, app: %s' % (host, port, self.app))
            self.websockets.remove(ws)
            await ws.close()

        await self.swagger.close()

    def get_peer_info(self, ws):
        """Get information about a connected peer from a websocket.

        :param ws: Websocket to get peer information from.
        :return: A two-tuple (host,port) describing the connected peer. 
        """
        # info will either be a two-tuple (host, port) for an IPV4 address, 
        # or a four-tuple (host, port, flowinfo, scope_id) for an IPV6 address.
        # see https://docs.python.org/3/library/asyncio-protocol.html#asyncio.BaseTransport.get_extra_info
        # we're only interested in host and port anyway
        info = ws.get_extra_info('peername')
        return tuple(info[:2])

    def get_repo(self, name):
        """Get a specific repo by name.

        :param name: Name of the repo to get
        :return: Repository, or None if not found.
        :rtype:  aioari.model.Repository
        """
        return self.repositories.get(name)

    async def run_operation(self, oper, **kwargs):
        """Trigger an operation.
        Overrideable for Trio.
        """
        return await oper(**kwargs)

    async def get_resp_text(self, resp):
        """Get the text from a response.
        Overrideable for Trio.
        """
        return await resp.text()

    async def __run(self, ws):
        """Drains all messages from a WebSocket, sending them to the client's
        listeners.

        :param ws: WebSocket to drain.
        """
        # TypeChecker false positive on iter(callable, sentinel) -> iterator
        # Fixed in plugin v3.0.1
        # noinspection PyTypeChecker
        while True:
            msg = await ws.receive()
            if msg is None:
                return ## EOF
            elif msg.type in {aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING}:
                break
            elif msg.type != aiohttp.WSMsgType.TEXT:
                log.warning("Unknown JSON message type: %s", repr(msg))
                continue # ignore
            msg_json = json.loads(msg.data)
            if not isinstance(msg_json, dict) or 'type' not in msg_json:
                log.error("Invalid event: %s" % msg)
                continue
            await self.process_ws(msg_json)

    async def process_ws(self, msg):
        """Process one incoming websocket message"""

        listeners = list(self.event_listeners.get(msg['type'], [])) \
                    + list(self.event_listeners.get('*', []))
        for listener in listeners:
            # noinspection PyBroadException
            try:
                callback, event_obj, args, kwargs, as_task = listener
                log.debug("cb_type=%s" % type(callback))
                args = args or ()
                kwargs = kwargs or {}
                cb = callback(msg, *args, **kwargs)
                # The callback may or may not be an async function
                if hasattr(cb,'__await__'):
                    if as_task:
                        asyncio.create_task(cb)
                    else:
                        await cb

            except Exception as e:
                self.exception_handler(e)

        if msg['type'] == "ChannelDestroyed":
            for i in tuple(self.event_listeners.keys()):
                for o in [item for item in self.event_listeners[i] if msg.get('channel').get('id') in item]:
                    self.event_listeners[i].remove(o)

    async def run(self, apps, subscribe_all=False, *, _test_msgs=[]):
        """Connect to the WebSocket and begin processing messages.

        This method will block until all messages have been received from the
        WebSocket, or until this client has been closed.

        :param apps: Application (or list of applications) to connect for
        :type  apps: str or list of str
        """
        self.app = apps.split('&')[0]
        while True:
            if isinstance(apps, list):
                apps = ','.join(apps)
            try:
                ws = await self.swagger.events.eventWebsocket(app=apps, subscribeAll=subscribe_all)
            except (OSError, aiohttp.ClientConnectionError, aiohttp.WSServerHandshakeError) as ex:
                log.error(ex)
                await asyncio.sleep(1)
                continue
            host, port = self.get_peer_info(ws)
            log.info('Successfully connected to ws://%s:%s, app: %s' % (host, port, self.app))
            self.websockets.add(ws)

            # For tests
            for m in _test_msgs:
                ws.push(m)

            await self.__run(ws)


    def on_event(self, event_type, event_cb, event_obj=None, as_task=False, *args, **kwargs):
        """Register callback for events with given type.

        :param event_type: String name of the event to register for.
        :param event_cb: Callback function
        :type  event_cb: (dict) -> None
        :param args: Arguments to pass to event_cb
        :param kwargs: Keyword arguments to pass to event_cb
        """
        listeners = self.event_listeners.setdefault(event_type, list())
        for cb in listeners:
            if event_cb == cb[0]:
                listeners.remove(cb)
        callback_obj = (event_cb, event_obj, args, kwargs, as_task)
        log.debug("event_cb=%s" % event_cb)
        listeners.append(callback_obj)
        client = self

        class EventUnsubscriber(object):
            """Class to allow events to be unsubscribed.
            """

            def close(self):
                """Unsubscribe the associated event callback.
                """
                if callback_obj in client.event_listeners[event_type]:
                    client.event_listeners[event_type].remove(callback_obj)

        return EventUnsubscriber()

    def on_object_event(self, event_type, event_cb, factory_fn, model_id, as_task=False,
                        *args, **kwargs):
        """Register callback for events with the given type. Event fields of
        the given model_id type are passed along to event_cb.

        If multiple fields of the event have the type model_id, a dict is
        passed mapping the field name to the model object.

        :param event_type: String name of the event to register for.
        :param event_cb: Callback function
        :type  event_cb: (Obj, dict) -> None or (dict[str, Obj], dict) ->
        :param factory_fn: Function for creating Obj from JSON
        :param model_id: String id for Obj from Swagger models.
        :param args: Arguments to pass to event_cb
        :param kwargs: Keyword arguments to pass to event_cb
        """
        # Find the associated model from the Swagger declaration
        log.debug("On object event %s %s %s %s"%(event_type, event_cb, factory_fn, model_id))
        event_model = self.event_models.get(event_type)
        if not event_model:
            raise ValueError("Cannot find event model '%s'" % event_type)

        # Extract the fields that are of the expected type
        obj_fields = [k for (k, v) in event_model['properties'].items()
                      if v['type'] == model_id]
        if not obj_fields:
            raise ValueError("Event model '%s' has no fields of type %s"
                             % (event_type, model_id))

        def extract_objects(event, *args, **kwargs):
            """Extract objects of a given type from an event.

            :param event: Event
            :param args: Arguments to pass to the event callback
            :param kwargs: Keyword arguments to pass to the event
                                      callback
            """
            # Extract the fields which are of the expected type
            obj = {obj_field: factory_fn(self, event[obj_field])
                   for obj_field in obj_fields
                   if event.get(obj_field)}
            # If there's only one field in the schema, just pass that along
            if len(obj_fields) == 1:
                if obj:
                    vals = list(obj.values())
                    obj = vals[0]
                else:
                    obj = None
            return event_cb(obj, event, *args, **kwargs)

        return self.on_event(event_type, extract_objects, as_task=as_task,
                             *args,
                             **kwargs)

    def on_channel_event(self, event_type, fn, as_task=False, *args, **kwargs):
        """Register callback for Channel related events

        :param event_type: String name of the event to register for.
        :param fn: Callback function
        :type  fn: (Channel, dict) -> None or (list[Channel], dict) -> None
        :param args: Arguments to pass to fn
        :param kwargs: Keyword arguments to pass to fn
        """
        return self.on_object_event(event_type, fn, Channel, 'Channel',
                                    as_task=as_task, *args, **kwargs)

    def on_bridge_event(self, event_type, fn, *args, **kwargs):
        """Register callback for Bridge related events

        :param event_type: String name of the event to register for.
        :param fn: Callback function
        :type  fn: (Bridge, dict) -> None or (list[Bridge], dict) -> None
        :param args: Arguments to pass to fn
        :param kwargs: Keyword arguments to pass to fn
        """
        return self.on_object_event(event_type, fn, Bridge, 'Bridge',
                                    *args, **kwargs)

    def on_playback_event(self, event_type, fn, *args, **kwargs):
        """Register callback for Playback related events

        :param event_type: String name of the event to register for.
        :param fn: Callback function
        :type  fn: (Playback, dict) -> None or (list[Playback], dict) -> None
        :param args: Arguments to pass to fn
        :param kwargs: Keyword arguments to pass to fn
        """
        return self.on_object_event(event_type, fn, Playback, 'Playback',
                                    *args, **kwargs)

    def on_live_recording_event(self, event_type, fn, *args, **kwargs):
        """Register callback for LiveRecording related events

        :param event_type: String name of the event to register for.
        :param fn: Callback function
        :type  fn: (LiveRecording, dict) -> None or (list[LiveRecording], dict) -> None
        :param args: Arguments to pass to fn
        :param kwargs: Keyword arguments to pass to fn
        """
        return self.on_object_event(event_type, fn, LiveRecording,
                                    'LiveRecording', *args, **kwargs)

    def on_stored_recording_event(self, event_type, fn, *args, **kwargs):
        """Register callback for StoredRecording related events

        :param event_type: String name of the event to register for.
        :param fn: Callback function
        :type  fn: (StoredRecording, dict) -> None or (list[StoredRecording], dict) -> None
        :param args: Arguments to pass to fn
        :param kwargs: Keyword arguments to pass to fn
        """
        return self.on_object_event(event_type, fn, StoredRecording,
                                    'StoredRecording', *args, **kwargs)

    def on_endpoint_event(self, event_type, fn, *args, **kwargs):
        """Register callback for Endpoint related events

        :param event_type: String name of the event to register for.
        :param fn: Callback function
        :type  fn: (Endpoint, dict) -> None or (list[Endpoint], dict) -> None
        :param args: Arguments to pass to fn
        :param kwargs: Keyword arguments to pass to fn
        """
        return self.on_object_event(event_type, fn, Endpoint, 'Endpoint',
                                    *args, **kwargs)

    def on_device_state_event(self, event_type, fn, *args, **kwargs):
        """Register callback for DeviceState related events

        :param event_type: String name of the event to register for.
        :param fn: Callback function
        :type  fn: (DeviceState, dict) -> None or (list[DeviceState], dict) -> None
        :param args: Arguments to pass to fn
        :param kwargs: Keyword arguments to pass to fn
        """
        return self.on_object_event(event_type, fn, DeviceState, 'DeviceState',
                                    *args, **kwargs)

    def on_sound_event(self, event_type, fn, *args, **kwargs):
        """Register callback for Sound related events

        :param event_type: String name of the event to register for.
        :param fn: Sound function
        :type  fn: (Sound, dict) -> None or (list[Sound], dict) -> None
        :param args: Arguments to pass to fn
        :param kwargs: Keyword arguments to pass to fn
        """
        return self.on_object_event(event_type, fn, Sound, 'Sound',
                                    *args, **kwargs)

