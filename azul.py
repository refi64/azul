#!/usr/bin/env python

# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import gevent.monkey
gevent.monkey.patch_all(thread=False)

import sys
sys.modules['gi.overrides.Gdk'] = None

import gi
gi.require_version('GLib', '2.0')
gi.require_version('Gdk', '3.0')
gi.require_version('Gtk', '3.0')
gi.require_foreign('cairo')

from gi.repository import GLib, GObject, Gio, Gdk, GdkPixbuf, Gtk
import cairo

import attr
import ctypes
import functools
import gevent.os
import gevent.pool
import greenlet
import inspect
import io
import keyring
import math
import mistune
import os
import pygments.formatter
import pygments.lexers
import pygments.util
import queue
import requests
import signal
import sys
import syslog
import threading
import traceback
import urllib.parse
import zulip


APP_ID = 'com.refi64.Azul'


def ignore_first(func):
    return lambda first, *args, **kwargs: func(*args, **kwargs)


def construct_with_mapped_args(ty, data, **kw):
    for key, value in data.items():
        if key not in ty.mapping:
            continue

        target_key = ty.mapping[key]
        kw[target_key or key] = value

    return ty(**kw)


class ValidationError(Exception):
    pass


class GLog:
    _SYSLOG_PRIORITIES = {
        'debug': syslog.LOG_DEBUG,
        'info': syslog.LOG_INFO,
        'message': syslog.LOG_NOTICE,
        'warning': syslog.LOG_WARNING,
        'critical': syslog.LOG_WARNING,
        'error': syslog.LOG_ERR,
    }

    PRIORITIES = {level: ctypes.create_string_buffer(str(n).encode('utf-8'))
                  for level, n in _SYSLOG_PRIORITIES.items()}

    @classmethod
    def log(cls, level, message, frame=2):
        glevel = getattr(GLib.LogLevelFlags, f'LEVEL_{level.upper()}')
        preserve = []

        def make_field(key, value):
            if not isinstance(value, ctypes.Array):
                value = ctypes.create_string_buffer(value.encode('utf-8'))
                preserve.append(value)

            field = GLib.LogField()
            field.key = key
            field.value = ctypes.addressof(value)
            field.length = len(value)
            return field

        frame_info = inspect.stack()[frame]

        fields = [
            make_field('PRIORITY', cls.PRIORITIES[level]),
            make_field('CODE_FILE', frame_info.filename),
            make_field('CODE_LINE', str(frame_info.lineno)),
            make_field('CODE_FUNC', frame_info.function),
            make_field('MESSAGE', message),
        ]
        GLib.log_structured_array(glevel, fields)


def g_log(level, message): GLog.log(level, message)
def g_debug(message): GLog.log('debug', message)
def g_info(message): GLog.log('info', message)
def g_message(message): GLog.log('message', message)
def g_warning(message): GLog.log('warning', message)
def g_error(message): GLog.log('error', message)
def g_critical(message): GLog.log('critical', message)


@attr.s
class AccountInfo:
    name = attr.ib()
    description = attr.ib()
    icon_url = attr.ib()

    mapping = {
        'realm_name': 'name',
        'realm_description': 'description',
        'realm_icon': 'icon_url',
    }

    @staticmethod
    def from_data(data):
        return construct_with_mapped_args(AccountInfo, data)


@attr.s
class AccountQueueModel:
    id = attr.ib()
    last_event_id = attr.ib()

    mapping = {
        'queue_id': 'id',
        'last_event_id': None,
    }

    @staticmethod
    def from_data(data):
        return construct_with_mapped_args(AccountQueueModel, data)


@attr.s
class AccountModel:
    index = attr.ib()
    server = attr.ib()
    email = attr.ib()
    apikey = attr.ib()
    _client = attr.ib(default=None)

    info = attr.ib(default=None)
    queue = attr.ib(default=None)

    @property
    def client(self):
        if self._client is None:
            self._client = zulip.Client(site=self.server, email=self.email,
                                        api_key=self.apikey)
        return self._client

    def get_absolute_url(self, url):
        server = self.server
        if url.startswith('/'):
            url = url[1:]
        if not server.startswith('http'):
            server = f'https://{server}'
        return urllib.parse.urljoin(server, url)


@attr.s
class EventModel:
    id = attr.ib()

    mapping = {
        'id': None,
    }

    @staticmethod
    def from_data(data):
        if data['type'] == 'message':
            return MessageEventModel.from_data(data)
        else:
            if data['type'] != 'heartbeat':
                g_warning(f'Unknown event: {data}')
            return construct_with_mapped_args(EventModel, data)


@attr.s
class MessageEventModel(EventModel):
    message = attr.ib()

    mapping = {
        **EventModel.mapping,
    }

    @staticmethod
    def from_data(data):
        message = MessageModel.from_data(data['message'])
        return construct_with_mapped_args(MessageEventModel, data, message=message)


@attr.s
class StreamModel:
    id = attr.ib()
    name = attr.ib()
    description = attr.ib()
    invite_only = attr.ib()

    mapping = {
        'stream_id': 'id',
        'name': None,
        'description': None,
        'invite_only': None,
    }

    @staticmethod
    def from_data(data):
        return construct_with_mapped_args(StreamModel, data)


@attr.s
class SubscribedStreamModel(StreamModel):
    color = attr.ib()
    desktop_notifications = attr.ib()
    subscribers = attr.ib()

    is_muted = attr.ib(default=False)
    pinned = attr.ib(default=False)

    mapping = {
        'color': None,
        'desktop_notifications': None,
        'is_muted': None,
        'pinned': None,
        'subscribers': None,
        **StreamModel.mapping,
    }

    @staticmethod
    def from_data(data):
        return construct_with_mapped_args(SubscribedStreamModel, data)


@attr.s
class TopicModel:
    name = attr.ib()
    max_id = attr.ib(default=None)

    mapping = {
        'max_id': None,
        'name': None,
    }

    @staticmethod
    def from_data(data):
        return construct_with_mapped_args(TopicModel, data)


@attr.s
class ReactionModel:
    code = attr.ib()
    type = attr.ib()
    name = attr.ib()

    mapping = {
        'emoji_code': 'code',
        'emoji_name': 'name',
        'reaction_type': 'type',
    }

    @staticmethod
    def from_data(data):
        return construct_with_mapped_args(ReactionModel, data)


@attr.s
class MessageModel:
    id = attr.ib()
    type = attr.ib()
    sender_name = attr.ib()
    sender_avatar = attr.ib()
    sender_id = attr.ib()
    topic_name = attr.ib()
    reactions = attr.ib()
    content = attr.ib(repr=False)
    stream_id = attr.ib(default=None)

    mapping = {
        'id': None,
        'type': None,
        'sender_full_name': 'sender_name',
        'avatar_url': 'sender_avatar',
        'sender_id': None,
        'stream_id': None,
        'subject': 'topic_name',
        'content': None,
    }

    @staticmethod
    def from_data(data):
        reactions = list(map(ReactionModel.from_data, data['reactions']))
        return construct_with_mapped_args(MessageModel, data, reactions=reactions)


@attr.s
class MessagesModel:
    found_oldest = attr.ib()
    found_newest = attr.ib()
    messages = attr.ib()

    mapping = {
        'found_oldest': None,
        'found_newest': None,
    }

    @staticmethod
    def from_data(data):
        messages = list(map(MessageModel.from_data, data['messages']))
        return construct_with_mapped_args(MessagesModel, data,
                                          messages=messages)


@attr.s
class SearchNarrow:
    stream = attr.ib(default=None)
    topic = attr.ib(default=None)
    query = attr.ib(default=attr.Factory(dict))

    def to_data(self):
        narrow = []
        narrow.extend({'operator': k, 'operand': v} for k, v in self.query.items())

        if self.stream is not None and 'stream' not in self.query:
            narrow.append({'operator': 'stream', 'operand': self.stream.name})
        if self.topic is not None and 'topic' not in self.query:
            narrow.append({'operator': 'topic', 'operand': self.topic.name})

        return narrow


class Task:
    pass


@attr.s
class GetApiKeyTask(Task):
    account = attr.ib()
    password = attr.ib()

    def process(self, bus):
        client = zulip.Client(site=self.account.server, email='', api_key='')
        try:
            result = client.call_endpoint(url='fetch_api_key', method='POST',
                                          request={'username': self.account.email,
                                                   'password': self.password})
        except zulip.ZulipError as ex:
            bus.emit_from_main_thread('login-failed', self.account, str(ex))
            return

        if result['result'] == 'error':
            bus.emit_from_main_thread('login-failed', self.account, result['msg'])
        else:
            self.account.apikey = result['api_key']
            bus.emit_from_main_thread('api-key-retrieved', self.account)


@attr.s
class LoadDataTask(Task):
    account = attr.ib()
    url = attr.ib()

    def process(self, bus):
        data = requests.get(self.account.get_absolute_url(self.url)).content
        bus.emit_from_main_thread('data-loaded', self.url, data)


@attr.s
class LoadAccountTask(Task):
    account = attr.ib()

    def process(self, bus):
        info = self.account.client.call_endpoint(url='server_settings', method='GET')

        self.account.info = AccountInfo.from_data(info)
        self.account.info.icon_url = self.account.get_absolute_url(
            self.account.info.icon_url)
        self.account.info.icon = requests.get(self.account.info.icon_url).content

        queue = self.account.client.register(event_types=['message', 'typing'])
        self.account.queue = AccountQueueModel.from_data(queue)

        bus.emit_from_main_thread('account-loaded', self.account)


@attr.s
class MonitorAccountEventsTask(Task):
    account = attr.ib()

    def process(self, bus):
        while True:
            try:
                result = self.account.client.get_events(
                    queue_id=self.account.queue.id,
                    last_event_id=self.account.queue.last_event_id)
                events = list(map(EventModel.from_data, result['events']))

                message_events = [event for event in events
                                  if isinstance(event, MessageEventModel)]
                if message_events:
                    bus.emit_from_main_thread('message-events', self.account,
                                              message_events)

                self.account.queue.last_event_id = events[-1].id
            except greenlet.GreenletExit:
                raise
            except:
                traceback.print_exc()


@attr.s
class LoadStreamsTask(Task):
    account = attr.ib()
    subscribed_only = attr.ib(default=True)

    def process(self, bus):
        if self.subscribed_only:
            ty = SubscribedStreamModel
            stream_data = self.account.client.list_subscriptions()['subscriptions']
        else:
            ty = StreamModel
            stream_data = self.account.client.get_streams()['streams']

        streams = {}
        for stream in map(ty.from_data, stream_data):
            streams[stream.name] = stream

        bus.emit_from_main_thread('account-streams-loaded', self.account, streams)


@attr.s
class LoadTopicsTask(Task):
    account = attr.ib()
    stream = attr.ib()

    def process(self, bus):
        result = self.account.client.call_endpoint(
                    url=f'users/me/{self.stream.id}/topics', method='GET')
        topics = list(map(TopicModel.from_data, result['topics']))

        bus.emit_from_main_thread('stream-topics-loaded', self.account, self.stream,
                                  topics)


@attr.s
class LoadMessagesTask(Task):
    account = attr.ib()
    anchor = attr.ib(default=10000000000000000)
    narrow = attr.ib(default=attr.Factory(SearchNarrow))

    def process(self, bus):
        narrow = self.narrow.to_data()
        result = self.account.client.call_endpoint(url='messages', method='GET',
                                                   request={'anchor': self.anchor,
                                                            'num_before': 40,
                                                            'num_after': 0,
                                                            'narrow': narrow,
                                                            'apply_markdown': False})
        if result.get('code') == 'BAD_NARROW':
            bus.emit_from_main_thread('message-narrow-failed', self.account,
                                      self.narrow, result['desc'])
        else:
            messages = MessagesModel.from_data(result)
            bus.emit_from_main_thread('messages-loaded', self.account, self.narrow,
                                      self.anchor, messages)


@attr.s
class SendMessageTask(Task):
    account = attr.ib()
    stream = attr.ib()
    topic = attr.ib()
    content = attr.ib()

    def process(self, bus):
        request = {
            'type': 'stream',
            'to': self.stream.name,
            'subject': self.topic.name,
            'content': self.content,
        }
        self.account.client.send_message(request)


class TaskThread(threading.Thread):
    def __init__(self, bus):
        super(TaskThread, self).__init__()
        self.bus = bus

        self._signal_reader, self._signal_writer = os.pipe()
        self._tasks = queue.Queue()

    def add_task(self, task):
        self._tasks.put(task, block=False)
        os.write(self._signal_writer, bytes([1]))

    def quit(self):
        os.write(self._signal_writer, bytes([0]))

    def run(self):
        pool = gevent.pool.Pool()

        while True:
            signal = gevent.os.tp_read(self._signal_reader, 1)
            if not signal[0]:
                break

            task = self._tasks.get()
            pool.spawn(task.process, self.bus)
            self._tasks.task_done()

        pool.kill()


class EventBus(GObject.Object):
    def __init__(self):
        super(EventBus, self).__init__()

        self.settings = Gio.Settings(APP_ID)

        self.connect('api-key-retrieved', ignore_first(self.on_api_key_retrieved))
        self.connect('account-loaded', ignore_first(self.on_account_loaded))
        self.connect('data-loaded', ignore_first(self.on_data_loaded))

        self._thread = TaskThread(self)
        self._thread.start()

        self._accounts = {}
        self._load_accounts()

        self._cache = {}
        self._cache_loading = object()
        self._cache_lock = threading.Lock()

    def quit(self):
        self._thread.quit()

    def emit_from_main_thread(self, signal, *args):
        def emitter():
            self.emit(signal, *args)
            return False

        GLib.idle_add(emitter)

    def _add_task(self, task):
        self._thread.add_task(task)

    def sync_sizes(self, name, axis, widget):
        def on_size_sync(sync_name, requested_size):
            if sync_name != name:
                return

            current = widget.get_allocation()
            current_request = getattr(widget.get_size_request(), axis)
            current_allocated = getattr(current, axis)
            current_size = max([current_allocated, current_request])

            if current_size > requested_size:
                self.emit('size-sync', name, current_size)
            elif current_size < requested_size:
                setattr(current, axis, requested_size)
                widget.set_size_request(current.width, current.height)

        def on_size_allocate(allocation):
            self.emit('size-sync', name, getattr(allocation, axis))

        self.connect('size-sync', ignore_first(on_size_sync))
        widget.connect('size-allocate', ignore_first(on_size_allocate))

    def _load_accounts(self):
        saved_accounts = self.settings.get_value('accounts')

        for i in range(saved_accounts.n_children()):
            saved_account = saved_accounts.get_child_value(i)

            account = AccountModel(index=i,
                                   server=saved_account.get_child_value(0).get_string(),
                                   email=saved_account.get_child_value(1).get_string(),
                                   apikey=saved_account.get_child_value(2).get_string())
            self._accounts[account.server] = account
            self._load_account(account)

    @property
    def accounts(self):
        return list(self._accounts.values())

    def get_account_for_server(self, server):
        return self._accounts[server]

    def _save_account_data(self):
        account_data = [(account.server, account.email, account.apikey or '')
                        for account in self._accounts.values()]
        self.settings.set_value('accounts', GLib.Variant('a(sss)', account_data))

    def set_account(self, account, password):
        if not account.server:
            raise ValidationError('Invalid server.')
        if '@' not in account.email:
            raise ValidationError('Invalid email.')
        if not password:
            raise ValidationError('Invalid password.')

        if account.server in self._accounts and account.index == -1:
            raise ValidationError('An account for this server already exists.')

        if account.index == -1:
            account.index = len(self._accounts)
        self._accounts[account.server] = account
        self._save_account_data()

        keyring.set_password(account.server, account.email, password)

        self._load_account(account)

    def _load_account(self, account, password=None):
        if not account.apikey:
            if password is None:
                password = keyring.get_password(account.server, account.email)
            self._add_task(GetApiKeyTask(account, password))
        else:
            self._add_task(LoadAccountTask(account))

    def load_uncached_data_from_url(self, account, url):
        self._add_task(LoadDataTask(account, url))

    def load_data_from_url(self, account, url):
        with self._cache_lock:
            if url in self._cache:
                data = self._cache[url]
                if data is self._cache_loading:
                    return

                self.emit('data-loaded', url, self._cache[url])
            else:
                self._cache[url] = self._cache_loading
                self.load_uncached_data_from_url(account, url)

    def load_streams_for_account(self, account):
        self._add_task(LoadStreamsTask(account))

    def load_messages(self, account, **kwargs):
        self._add_task(LoadMessagesTask(account, **kwargs))

    def send_message(self, account, stream, topic, content):
        self._add_task(SendMessageTask(account, stream, topic, content))

    def load_topics_in_stream(self, account, stream):
        self._add_task(LoadTopicsTask(account, stream))

    def on_api_key_retrieved(self, account):
        self._save_account_data()
        self._load_account(account)

    def on_account_loaded(self, account):
        self._add_task(MonitorAccountEventsTask(account))

    def on_data_loaded(self, url, data):
        with self._cache_lock:
            if self._cache.get(url) is self._cache_loading:
                self._cache[url] = data

    @GObject.Signal(name='size-sync', arg_types=(object, object))
    def size_sync(self, name, size): pass

    @GObject.Signal(name='api-key-retrieved', arg_types=(object,))
    def api_key_retrieved(self, account): pass

    @GObject.Signal(name='login-failed', arg_types=(object, object))
    def login_failed(self, account, error): pass

    @GObject.Signal(name='data-loaded', arg_types=(str, object))
    def data_loaded(self, url, data): pass

    @GObject.Signal(name='account-loaded', arg_types=(object,))
    def account_loaded(self, account): pass

    @GObject.Signal(name='account-streams-loading')
    def account_streams_loading(self): pass

    @GObject.Signal(name='account-streams-loaded', arg_types=(object, object))
    def account_streams_loaded(self, account, streams): pass

    @GObject.Signal(name='stream-topics-loaded', arg_types=(object, object, object))
    def stream_topics_loaded(self, account, stream, topics): pass

    @GObject.Signal(name='messages-loaded', arg_types=(object, object, object, object))
    def messages_loaded(self, account, narrow, anchor, messages): pass

    @GObject.Signal(name='message-narrow-failed', arg_types=(object, object, object))
    def message_narrow_failed(self, account, narrow, error): pass

    @GObject.Signal(name='message-events', arg_types=(object, object))
    def message_events(self, account, events): pass

    @GObject.Signal(name='ui-account-selected', arg_types=(object,))
    def ui_account_selected(self, account): pass

    @GObject.Signal(name='ui-stream-selected', arg_types=(object, object))
    def ui_stream_selected(self, account, stream): pass

    @GObject.Signal(name='ui-topic-view-selected', arg_types=(object, object, object))
    def ui_topic_view_selected(self, account, stream_id, topic_name): pass

    @GObject.Signal(name='ui-add-account', arg_types=(object,))
    def ui_add_account(self, account): pass


class AccountDialog(Gtk.Dialog):
    def __init__(self, parent, account=None):
        title = 'Add Account' if account is None else 'Edit Account'
        super(AccountDialog, self).__init__(title, parent, 0,
            ('_Save', Gtk.ResponseType.APPLY, '_Cancel', Gtk.ResponseType.CANCEL))
        self.account = account
        self.set_default_size(500, 0)

        self.get_action_area().set_property('margin', 5)

        content = self.get_content_area()
        grid = Gtk.Grid(column_spacing=10, row_spacing=10, margin=10)

        self.error_bar = Gtk.InfoBar(show_close_button=True)
        self.error_label = Gtk.Label(label='')
        self.error_bar.get_content_area().add(self.error_label)
        self.error_bar.connect('response', lambda *_: self.error_bar.hide())
        self.error_bar.set_message_type(Gtk.MessageType.ERROR)
        grid.attach(self.error_bar, 0, 0, 2, 1)

        self.server = None
        self.email = None
        self.password = None
        labels = ['server', 'email', 'password']
        for i, name in enumerate(labels):
            label = Gtk.Label(label=f'{name.capitalize()}:', halign=Gtk.Align.END)
            entry = Gtk.Entry(hexpand=True)
            if account is not None:
                if name == 'password':
                    entry.set_text(keyring.get_password(account.server, account.email))
                else:
                    entry.set_text(getattr(account, name))

            grid.attach(label, 0, i + 1, 1, 1)
            grid.attach(entry, 1, i + 1, 1, 1)

            setattr(self, name, entry)

        self.server.set_input_purpose(Gtk.InputPurpose.URL)
        self.email.set_input_purpose(Gtk.InputPurpose.EMAIL)
        self.password.set_input_purpose(Gtk.InputPurpose.PASSWORD)
        self.password.set_visibility(False)

        content.add(grid)

        self.show_all()
        self.error_bar.hide()

    def get_account(self):
        if self.account is not None:
            account = self.account
            account.email = self.email.get_text()
            account.server = self.server.get_text()
        else:
            account = AccountModel(index=-1, email=self.email.get_text(),
                                   server=self.server.get_text(), apikey=None)

        return (account, self.password.get_text())

    def show_failure(self, message):
        self.error_label.set_label(message)
        self.error_bar.show_all()

    @staticmethod
    def get_account_info(parent, bus, account=None):
        dialog = AccountDialog(parent, account)
        account = None

        while True:
            response = dialog.run()
            if response != Gtk.ResponseType.APPLY:
                break

            account, password = dialog.get_account()
            try:
                bus.set_account(account, password)
            except ValidationError as ex:
                dialog.show_failure(str(ex))
            else:
                break

        dialog.destroy()
        return account


class DataImage(Gtk.Image):
    def __init__(self, size, data):
        super(DataImage, self).__init__()

        if isinstance(data, GdkPixbuf.Pixbuf):
            pixbuf = data
        else:
            loader = GdkPixbuf.PixbufLoader()
            loader.write(data)
            loader.close()

            pixbuf = loader.get_pixbuf()

        pixbuf = pixbuf.scale_simple(size, size, GdkPixbuf.InterpType.BILINEAR)
        self.set_from_pixbuf(self._process(pixbuf, size))

    def _process(self, pixbuf, size):
        return pixbuf


class CircularImage(DataImage):
    def _process(self, pixbuf, size):
        surface = Gdk.cairo_surface_create_from_pixbuf(pixbuf, 1, None)
        target = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)

        cr = cairo.Context(target)
        cr.set_source_surface(surface, 0, 0)
        cr.arc(size / 2, size / 2, size / 2, 0, 2 * math.pi)
        cr.clip()
        cr.paint()

        return Gdk.pixbuf_get_from_surface(target, 0, 0, size, size)


class AccountsView(Gtk.ListBox):
    REALM_ICON_SIZE = 40

    def __init__(self, bus, parent, **kwargs):
        super(AccountsView, self).__init__(**kwargs)
        self.parent = parent
        self.bus = bus
        self.widgets = {}

        for i, account in enumerate(self.bus.accounts):
            self.add_account(account)

        self.bus.connect('login-failed', ignore_first(self.on_login_failure))
        self.bus.connect('account-loaded', ignore_first(self.update_account))
        self.bus.connect('ui-add-account', ignore_first(self.add_account))
        self.connect('row-activated', ignore_first(self.on_account_selected))

        self.bus.sync_sizes('add-server', 'width', self)

    def _insert_account_widget(self, widget, account, index=None):
        index = account.index
        if index is None:
            index = len(self.bus.accounts) - 1

        if account.server in self.widgets:
            original = self.widgets[account.server]
            self.remove(original.get_parent().get_parent())
        self.widgets[account.server] = widget

        event_box = Gtk.EventBox()
        event_box.add(widget)

        event_box.set_events(Gdk.EventMask.BUTTON_RELEASE_MASK)
        event_box.connect('button-release-event',
                          ignore_first(functools.partial(self.show_context_menu,
                                                         account)))

        event_box.show_all()
        self.insert(event_box, index)

    def add_account(self, account):
        spinner = Gtk.Spinner()
        spinner.set_size_request(self.REALM_ICON_SIZE, self.REALM_ICON_SIZE)
        spinner.start()

        self._insert_account_widget(spinner, account)
        self.show_all()

    def on_login_failure(self, account, error):
        icon = Gtk.IconTheme.get_default().lookup_icon('dialog-error',
                                                       self.REALM_ICON_SIZE, 0)
        image = DataImage(self.REALM_ICON_SIZE, icon.load_icon())
        self._insert_account_widget(image, account)

        row = image.get_parent().get_parent()
        row.set_selectable(False)
        row.set_tooltip_text(error)

    def update_account(self, account):
        image = CircularImage(self.REALM_ICON_SIZE, account.info.icon)
        image.get_style_context().add_class('circular')
        image.set_tooltip_text(account.info.name)
        self._insert_account_widget(image, account)

        self.show_all()

    def show_context_menu(self, account, event):
        if event.type != Gdk.EventType.BUTTON_RELEASE or event.button != 3:
            return False

        def on_edit():
            nonlocal account
            account = AccountDialog.get_account_info(self.parent, self.bus, account)
            if account is not None:
                self.add_account(account)

        menu = Gtk.Menu()
        edit = Gtk.MenuItem.new_with_label('Edit Account')
        edit.connect('activate', ignore_first(on_edit))
        menu.append(edit)

        menu.show_all()
        menu.popup_at_pointer(event)

    def on_account_selected(self, row):
        index = row.get_index()
        account = self.bus.accounts[index]
        assert account.index == index

        if account.info is None:
            return

        self.bus.emit('ui-account-selected', account)

        self.bus.emit('account-streams-loading')
        self.bus.load_streams_for_account(account)


class EmptyListView(Gtk.ListBox):
    def __init__(self, bus, **kwargs):
        super(EmptyListView, self).__init__(**kwargs)
        self.bus = bus
        self.loading = False

        placeholder = Gtk.Label(label='Click a server on the left.', margin=10,
                                sensitive=False)
        placeholder.show()
        self.set_placeholder(placeholder)

        self.bus.sync_sizes('stream-view', 'width', self)

    def set_loading(self):
        if self.loading:
            return

        spinner = Gtk.Spinner(margin=120)
        spinner.start()
        spinner.show()
        self.set_placeholder(spinner)

    def clear_selection(self):
        pass


class StreamsView(Gtk.ScrolledWindow):
    def __init__(self, bus, account, streams, **kwargs):
        super(StreamsView, self).__init__(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                          **kwargs)
        self.bus = bus
        self.account = account
        self.streams = streams

        self.stream_topics = {}
        self.stream_iters = {}
        self.store = Gtk.TreeStore(str)
        self.tree = Gtk.TreeView(self.store, headers_visible=False)

        self.add(self.tree)

        for stream in streams.values():
            it = self.store.append(None, [f'#{stream.name}'])
            self.stream_topics[stream.name] = {}
            self.stream_iters[stream.name] = it
            self.bus.load_topics_in_stream(self.account, stream)

        stream_renderer = Gtk.CellRendererText()
        stream_column = Gtk.TreeViewColumn('stream', stream_renderer, text=0)
        self.tree.append_column(stream_column)

        self.tree.get_selection().connect('changed',
                                          ignore_first(self.on_stream_selected))
        self.bus.connect('stream-topics-loaded',
                          ignore_first(self.on_stream_topics_loaded))

        self.bus.sync_sizes('stream-view', 'width', self)

    def on_stream_selected(self):
        _, it = self.tree.get_selection().get_selected()
        if it is None:
            self.bus.emit('ui-stream-selected', self.account, None)
            self.bus.load_messages(self.account)
            return

        path = self.store.get_path(it).get_indices()
        stream = self.streams[self.store[path[0]][0][1:]]
        topic = self.stream_topics[stream.name][self.store[path][0]] if len(path) == 2 \
                                                                     else None
        narrow = SearchNarrow(stream=stream, topic=topic)

        self.bus.emit('ui-stream-selected', self.account, stream)
        self.bus.load_messages(self.account, narrow=narrow)

    def on_stream_topics_loaded(self, account, stream, topics):
        if account is not self.account:
            return

        for topic in topics:
            self.stream_topics[stream.name][topic.name] = topic
            self.store.append(self.stream_iters[stream.name], [topic.name])

    def clear_selection(self):
        self.tree.get_selection().unselect_all()


class AvatarView(Gtk.Bin):
    _AVATAR_SIZE = 48
    _cache = {}

    def __init__(self, bus, account, url, **kwargs):
        super(AvatarView, self).__init__(**kwargs)
        self.set_size_request(self._AVATAR_SIZE, self._AVATAR_SIZE)
        self.bus = bus
        self.account = account
        self.url = url
        self.image = None
        self.id = None

        missing = object()
        cached = self._cache.get(url, missing)
        if cached is not None and cached is not missing:
            self.on_data_loaded(url, cached)

        self.id = self.bus.connect('data-loaded', ignore_first(self.on_data_loaded))
        if cached is missing:
            self._cache[url] = None
            self.bus.load_data_from_url(self.account, url)

    def on_data_loaded(self, url, data):
        if url != self.url:
            return

        if self.id is not None:
            self.bus.disconnect(self.id)

        self._cache[url] = data

        self.image = DataImage(self._AVATAR_SIZE, data)
        self.image.show()
        self.add(self.image)


class MarkdownView(Gtk.Label):
    class PangoFormatter(pygments.formatter.Formatter):
        def __init__(self, **kw):
            super(MarkdownView.PangoFormatter, self).__init__(**kw)

            self.styles = {}

            for token, style in self.style:
                attrs = {}
                if style['color']:
                    attrs['color'] = f'#{style["color"]}'
                if style['bold']:
                    attrs['font_weight'] = 'bold'
                if style['italic']:
                    attrs['style'] = 'italic'
                if style['underline']:
                    attrs['underline'] = 'single'

                self.styles[token] = ' '.join(f'{k}="{v}"' for k, v in attrs.items())

        def format(self, tokens, out):
            out.write('<span face="monospace">')

            for token, value in tokens:
                while token not in self.styles:
                    token = token.parent

                escaped = GLib.markup_escape_text(value)
                out.write(f'<span {self.styles[token]}>{escaped}</span>')

            out.write('</span>')

    class PangoRenderer(mistune.Renderer):
        # Block level.

        def block_code(self, code, language=None):
            if language == 'quote':
                markdown = mistune.Markdown(renderer=self)
                return self.block_quote(markdown(code))

            highlighted = None

            if language is not None:
                try:
                    lexer = pygments.lexers.get_lexer_by_name(language)
                except pygments.util.ClassNotFound:
                    pass
                else:
                    highlighted = pygments.highlight(code, lexer,
                                                     MarkdownView.PangoFormatter())

            if highlighted is not None:
                return highlighted
            else:
                escaped = GLib.markup_escape_text(code)
                return f'<span face="monospace">{escaped}</span>'

        def block_quote(self, text):
            return f'<span color="#616161">{text}</span>'

        def block_html(self, html):
            return GLib.markup_escape_text(html)

        def header(self, text, level, raw=None):
            return self.paragraph('#' * level + text)

        def hrule(self):
            return ''

        def image(self, src, title, alt_text):
            return self.link(src, title, alt_text)

        def list(self, body, ordered=True):
            if ordered:
                count = 1
                buf = io.StringIO()

                assert body[0] == '\0'
                for chunk in body.split('\0')[1:]:
                    buf.write(f'{count}. ')
                    buf.write(chunk)
                    count += 1

                result = buf.getvalue()
            else:
                result = body.replace('\0', '•')

            return f'\n{result}\n'

        def list_item(self, text):
            return f'\0{text}\n'

        def paragraph(self, text):
            return f'{text}\n'

        # Inline level.

        def autolink(self, link, is_email=False):
            return self.link(link, None, GLib.markup_escape_text(link))

        def codespan(self, text):
            return f'<tt>{GLib.markup_escape_text(text)}</tt>'

        def double_emphasis(self, text):
            return f'<b>{text}</b>'

        def emphasis(self, text):
            return f'<i>{text}</i>'

        def linebreak(self):
            return '\n'

        def link(self, link, title, content):
            escaped = link.replace('\\', '\\\\').replace('"', '\"')
            return f'<a href="{GLib.markup_escape_text(link)}">{content}</a>'

        def strikethrough(self, text):
            return f'<s>{text}</s>'

        def text(self, text):
            return GLib.markup_escape_text(text)

        def inline_html(self, text):
            return GLib.markup_escape_text(text)

    renderer = PangoRenderer()
    markdown = mistune.Markdown(renderer=renderer)

    def __init__(self, account, content, **kwargs):
        super(MarkdownView, self).__init__(xalign=0,
                                           track_visited_links=False, **kwargs)
        self.account = account

        markup = self.markdown(content)
        if self.get_single_line_mode():
            markup = markup.replace('\n', '')
        self.set_markup(markup)

        self.connect('activate-link', ignore_first(self.activate_link))

        self.show_all()

    def activate_link(self, url):
        url = self.account.get_absolute_url(url)
        scheme = urllib.parse.urlparse(url).scheme
        if not scheme:
            appinfo = Gio.AppInfo.get_default_for_type('x-scheme-handler/http', True)
        else:
            appinfo = Gio.AppInfo.get_default_for_uri_scheme(scheme)

        if appinfo is not None:
            appinfo.launch_uris([url], None)
        return True


class MessagesFromSenderView(Gtk.Grid):
    def __init__(self, bus, account, first, **kwargs):
        super(MessagesFromSenderView, self).__init__(**kwargs)
        self.bus = bus
        self.account = account
        self.first = first
        self.messages = []

        self.set_column_spacing(10)

        avatar = AvatarView(self.bus, self.account, self.first.sender_avatar,
                            valign=Gtk.Align.START)
        self.attach(avatar, 0, 0, 1, 5)

        name = Gtk.Label(halign=Gtk.Align.START)
        name.set_markup(f'<b>{GLib.markup_escape_text(self.first.sender_name)}</b>')
        self.attach(name, 1, 0, 1, 1)

        self.add_message(first)

    def add_message(self, message):
        previous_view = self.get_child_at(1, 1)
        if previous_view is not None:
            previous_view.destroy()

        self.messages.append(message)
        view = MarkdownView(self.account,
                            '\n'.join(message.content for message in self.messages),
                            selectable=True, wrap=True)
        self.attach(view, 1, 1, 1, 1)


class TopicView(Gtk.Grid):
    def __init__(self, bus, account, stream_id, name, **kwargs):
        super(TopicView, self).__init__(**kwargs)
        self.bus = bus
        self.account = account
        self.stream_id = stream_id
        self.name = name
        self.messages = []

        label = Gtk.Label(hexpand=True)
        label.set_markup(f'<b>{GLib.markup_escape_text(name)}</b>')
        self.attach(label, 0, 0, 1, 1)

        self.bottom = label

    def add_message(self, message):
        self.messages.append(message)

        if isinstance(self.bottom, MessagesFromSenderView):
            previous_sender_id = self.bottom.first.sender_id
            if previous_sender_id == message.sender_id:
                self.bottom.add_message(message)
                return

        sender_messages_view = MessagesFromSenderView(self.bus, self.account, message)
        self.attach_next_to(sender_messages_view, self.bottom, Gtk.PositionType.BOTTOM,
                            1, 1)
        self.bottom = sender_messages_view


class MessagesView(Gtk.ScrolledWindow):
    def __init__(self, bus, account, **kwargs):
        super(MessagesView, self).__init__(**kwargs)
        self.bus = bus
        self.account = account
        self.narrow = None
        self.listbox = None
        self.last_messages = None
        self.topic_views = []
        self.requested_more_messages = False
        self.brace_for_scrollbar_reset = False

        self.bus.connect('messages-loaded', ignore_first(self.on_messages_loaded))
        self.bus.connect('message-events', ignore_first(self.on_message_events))

        adjustment = self.get_vadjustment()
        adjustment.connect('changed', ignore_first(self.on_adjustment_changed))
        adjustment.connect('value-changed',
                           ignore_first(self.on_adjustment_value_changed))
        self.update_previous_adjustment()

    def update_previous_adjustment(self):
        adjustment = self.get_vadjustment()
        self.previous_adjustment_upper = adjustment.get_upper()
        self.previous_adjustment_top = (adjustment.get_upper() -
                                        adjustment.get_page_size())
        self.previous_adjustment_value = adjustment.get_value()

    def on_messages_loaded(self, account, narrow, anchor, messages):
        if account is not self.account:
            return

        if narrow != self.narrow or self.listbox is None:
            if self.listbox is not None:
                self.remove(self.listbox)
            self.listbox = Gtk.ListBox(expand=True)
            self.listbox.connect('row-activated', ignore_first(self.on_row_activated))
            self.listbox.set_selection_mode(Gtk.SelectionMode.NONE)
            self.add(self.listbox)

            self.topic_views = []

        self.narrow = narrow
        self.last_messages = messages
        self.requested_more_messages = False

        topic_view = None
        message_backlog = []

        if messages.found_newest:
            topic_view_insert_position = len(self.topic_views)
        else:
            topic_view_insert_position = 0

            if self.topic_views[0].name == messages.messages[-1].topic_name:
                top = self.topic_views.pop(0)
                message_backlog = top.messages[1:]
                top.get_parent().destroy()

        original_first = self.topic_views[0] if self.topic_views else None

        for message in messages.messages:
            if topic_view is None or topic_view.name != message.topic_name:
                topic_view = TopicView(self.bus, account, message.stream_id,
                                       message.topic_name)
                self.listbox.insert(topic_view, topic_view_insert_position)
                self.topic_views.insert(topic_view_insert_position, topic_view)

                topic_view_insert_position += 1

            topic_view.add_message(message)

        for message in message_backlog:
            topic_view.add_message(message)

        if original_first is not None and not messages.found_newest:
            original_position = original_first.get_allocation().y
            handler = ignore_first(
                lambda _: self.on_listbox_size_allocate(signal_id, original_first,
                                                        original_position))
            signal_id = self.listbox.connect('size-allocate', handler)
        else:
            self.requested_more_messages = False

        self.show_all()

    def on_row_activated(self, row):
        topic_view = row.get_child()
        self.bus.emit('ui-topic-view-selected', self.account, topic_view.stream_id,
                      topic_view.name)

    def on_listbox_size_allocate(self, signal_id, original_first, original_position):
        new_position = original_first.get_allocation().y
        amount_moved = new_position - original_position

        adjustment = self.get_vadjustment()
        adjustment.set_value(adjustment.get_value() + amount_moved)

        self.listbox.disconnect(signal_id)
        self.requested_more_messages = False
        self.brace_for_scrollbar_reset = True

    def on_message_events(self, account, events):
        if account is not self.account:
            return

        if self.narrow is None or self.narrow.query:
            return

        adjustment = self.get_vadjustment()
        top = adjustment.get_upper() - adjustment.get_page_size()
        current = adjustment.get_value()

        stream = self.narrow.stream
        topic = self.narrow.topic

        for event in events:
            if stream is not None and event.message.stream_id != stream.id:
                continue
            if topic is not None and event.message.topic_name != topic.name:
                continue
            self.topic_views[-1].add_message(event.message)

        self.show_all()

    def on_adjustment_changed(self):
        adjustment = self.get_vadjustment()

        if self.previous_adjustment_value == self.previous_adjustment_top:
            adjustment.set_value(adjustment.get_upper() - adjustment.get_page_size())

        self.update_previous_adjustment()

    def on_adjustment_value_changed(self):
        adjustment = self.get_vadjustment()
        value = adjustment.get_value()
        page_size = adjustment.get_page_size()

        if self.brace_for_scrollbar_reset:
            if not value:
                adjustment.set_value(self.previous_adjustment_value)
                return
            elif value != self.previous_adjustment_value:
                self.brace_for_scrollbar_reset = False
        elif page_size and value < page_size and \
           not self.last_messages.found_oldest and not self.requested_more_messages:
            self.bus.load_messages(self.account,
                                   anchor=self.last_messages.messages[0].id,
                                   narrow=self.narrow)
            self.requested_more_messages = True
        elif value > page_size and self.requested_more_messages:
            self.requested_more_messages = False

        self.update_previous_adjustment()


class HeaderBar(Gtk.Grid):
    def __init__(self, bus, parent):
        super(HeaderBar, self).__init__()
        self.bus = bus
        self.parent = parent

        self.bus.connect('messages-loaded', ignore_first(self.on_messages_loaded))
        self.bus.connect('message-narrow-failed', ignore_first(self.on_narrow_failure))
        self.bus.connect('ui-account-selected', ignore_first(self.on_account_selected))
        self.bus.connect('ui-stream-selected', ignore_first(self.on_stream_selected))

        self.left_header = Gtk.HeaderBar()
        self.stream_header = Gtk.HeaderBar()
        self.main_header = Gtk.HeaderBar(hexpand=True, show_close_button=True)

        add_button = Gtk.Button(image=Gtk.Image.new_from_icon_name('list-add-symbolic',
                                                                   Gtk.IconSize.BUTTON))
        add_button.connect('clicked', ignore_first(self.on_add_button_click))
        self.left_header.set_custom_title(add_button)

        self.search_field = Gtk.SearchEntry(sensitive=False)
        self.search_field.connect('search-changed', ignore_first(self.update_search))
        self.main_header.pack_end(self.search_field)

        self.search_popover = Gtk.Popover(modal=False, relative_to=self.search_field)
        self.search_error = Gtk.Label(margin=10)
        self.search_error.show()
        self.search_popover.add(self.search_error)

        self.attach(self.left_header, 0, 0, 1, 1)
        self.attach(Gtk.Separator(), 1, 0, 1, 1)
        self.attach(self.stream_header, 2, 0, 1, 1)
        self.attach(Gtk.Separator(), 3, 0, 1, 1)
        self.attach(self.main_header, 4, 0, 1, 1)

        self.bus.sync_sizes('add-server', 'width', self.left_header)
        self.bus.sync_sizes('stream-view', 'width', self.stream_header)

        self.account = None
        self.stream = None

    def reset_error(self):
        self.search_field.get_style_context().remove_class('error')
        self.search_popover.popdown()

    def on_messages_loaded(self, account, narrow, anchor, messages):
        self.reset_error()

    def on_narrow_failure(self, account, narrow, error):
        self.search_field.get_style_context().add_class('error')
        self.search_error.set_text(f'Error: {error}')
        self.search_popover.popup()

    def on_account_selected(self, account):
        self.account = account
        self.stream = None
        self.search_field.set_sensitive(True)
        self.reset_error()

        self.stream_header.set_title(account.info.name)
        self.main_header.set_title('')
        self.main_header.set_custom_title(None)

        if self.search_field.get_text():
            self.update_search()

    def on_stream_selected(self, account, stream):
        self.stream = stream

        if stream is None:
            self.on_account_selected(self.account)
            return

        if stream.description:
            titles = Gtk.Grid()

            title = Gtk.Label(label=f'#{stream.name}')
            title.get_style_context().add_class('title')
            titles.attach(title, 0, 0, 1, 1)

            if stream.description:
                subtitle = MarkdownView(account, stream.description,
                                        single_line_mode=True)
                subtitle.get_style_context().add_class('subtitle')
                titles.attach(subtitle, 0, 1, 1, 1)

            self.main_header.set_custom_title(titles)
        else:
            self.main_header.set_custom_title(None)
            self.main_header.set_title(f'#{stream.name}')
        self.main_header.show_all()

        if self.search_field.get_text():
            self.update_search()

    def update_search(self):
        text = self.search_field.get_text()
        if not text:
            if self.stream is not None:
                self.bus.load_messages(self.account,
                                       narrow=SearchNarrow(stream=self.stream))
                self.on_stream_selected(self.account, self.stream)
            else:
                self.bus.load_messages(self.account)
                self.on_account_selected(self.account)

            return

        self.main_header.set_title('Search')

        operators = {'has', 'in', 'is', 'stream', 'topic', 'sender', 'near', 'id'}
        query = {}
        search = []

        for term in text.split():
            if ':' in term:
                operator, operand = term.split(':', 1)
                if operator in operators:
                    query[operator] = operand
                    continue

            search.append(term)

        if search:
            query['search'] = ' '.join(search)

        narrow = SearchNarrow(stream=self.stream, query=query)
        self.bus.load_messages(self.account, narrow=narrow)

    def on_add_button_click(self):
        account = AccountDialog.get_account_info(self.parent, self.bus)
        if account is not None:
            self.bus.emit('ui-add-account', account)


class MessageEditorInput(Gtk.TextView):
    def __init__(self, bus, **kwargs):
        super(MessageEditorInput, self).__init__(editable=True, hexpand=True,
                                                 **kwargs)

    @GObject.Signal(name='submit', arg_types=(), flags=GObject.SIGNAL_ACTION)
    def submit(self): pass


class MessageEditor(Gtk.Overlay):
    def __init__(self, bus, **kwargs):
        super(MessageEditor, self).__init__(sensitive=False, **kwargs)
        self.bus = bus
        self.stream_topics = {}

        self.bus.connect('account-streams-loaded',
                         ignore_first(self.on_account_streams_loaded))
        self.bus.connect('stream-topics-loaded',
                         ignore_first(self.on_stream_topics_loaded))
        self.bus.connect('messages-loaded', ignore_first(self.on_messages_loaded))
        self.bus.connect('ui-stream-selected', ignore_first(self.on_stream_selected))
        self.bus.connect('ui-topic-view-selected',
                         ignore_first(self.on_topic_view_selected))

        self.input = MessageEditorInput(self.bus)
        self.add(self.input)

        self.input.connect('submit', ignore_first(self.on_message_submit))

        selector_grid = Gtk.Grid(halign=Gtk.Align.START, valign=Gtk.Align.START)

        self.stream_selector = Gtk.ComboBoxText()
        self.stream_selector.connect('changed', ignore_first(self.on_stream_changed))

        selector_grid.attach(self.stream_selector, 0, 0, 1, 1)

        self.topic_selector = Gtk.ComboBoxText.new_with_entry()
        self.topic_selector.connect('changed', ignore_first(self.on_topic_changed))

        self.topic_popover = Gtk.Popover(modal=False, relative_to=self.topic_selector)
        topic_popover_message = Gtk.Label(label='A topic is required.', margin=10)
        topic_popover_message.show()
        self.topic_popover.add(topic_popover_message)

        topic_selector_overlay = Gtk.Overlay()
        topic_selector_overlay.add(self.topic_selector)

        self.topic_selector_label = Gtk.Label(label='Topic', halign=Gtk.Align.CENTER,
                                              sensitive=False)
        topic_selector_overlay.add_overlay(self.topic_selector_label)
        topic_selector_overlay.set_overlay_pass_through(self.topic_selector_label,
                                                        True)

        selector_grid.attach(topic_selector_overlay, 1, 0, 1, 1)

        self.add_overlay(selector_grid)

        action_grid = Gtk.Grid(halign=Gtk.Align.END, valign=Gtk.Align.CENTER)

        emoji = Gtk.Button(relief=Gtk.ReliefStyle.NONE,
                           image=Gtk.Image.new_from_icon_name('face-smile-symbolic',
                                                              Gtk.IconSize.BUTTON))
        action_grid.attach(emoji, 0, 0, 1, 1)

        submit = Gtk.Button(label='Submit', relief=Gtk.ReliefStyle.NONE)
        submit.connect('clicked', ignore_first(self.on_message_submit))
        action_grid.attach(submit, 1, 0, 1, 1)

        self.add_overlay(action_grid)

    def on_account_streams_loaded(self, account, streams):
        self.account = account
        self.streams = streams
        self.streams_by_id = {stream.id: stream for stream in streams.values()}
        self.stream_topics = {}
        self.set_sensitive(True)

        self.stream_selector.remove_all()
        for stream in streams:
            self.stream_selector.append(stream, f'#{stream}')

    def on_stream_topics_loaded(self, account, stream, topics):
        self.stream_topics[stream.name] = {topic.name: topic for topic in topics}
        if self.stream_selector.get_active_id() == stream.name:
            self.on_stream_changed()

    def on_stream_selected(self, account, stream):
        if stream is not None:
            self.stream_selector.set_active_id(stream.name)

    def on_topic_view_selected(self, account, stream_id, topic_name):
        self.stream_selector.set_active_id(self.streams_by_id[stream_id].name)
        self.on_stream_changed()
        self.topic_selector.set_active_id(topic_name)

    def on_messages_loaded(self, account, narrow, anchor, messages):
        latest_message = messages.messages[-1]
        stream = self.streams_by_id[latest_message.stream_id]

        self.stream_selector.set_active_id(stream.name)
        self.on_stream_changed()
        self.topic_selector.set_active_id(latest_message.topic_name)

    def on_stream_changed(self):
        stream_name = self.stream_selector.get_active_id()
        if stream_name not in self.stream_topics:
            return
        topics = self.stream_topics[stream_name]

        self.topic_selector.remove_all()
        for topic_name in topics:
            self.topic_selector.append(topic_name, topic_name)

        if self.topic_selector.get_active_text() not in topics:
            self.topic_selector.set_active_id(None)
            self.topic_selector.get_child().set_text('')

    def on_topic_changed(self):
        empty_topic = not self.topic_selector.get_active_text()
        self.topic_selector_label.set_visible(empty_topic)
        self.topic_popover.popdown()

    def on_message_submit(self):
        stream_name = self.stream_selector.get_active_id()
        topic_name = self.topic_selector.get_active_text()

        if not topic_name:
            self.topic_popover.popup()
            return

        stream = self.streams[stream_name]
        topic = self.stream_topics[stream_name].get(topic_name,
                                                    TopicModel(name=topic_name))

        buf = self.input.get_buffer()
        content = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)
        self.bus.send_message(self.account, stream, topic, content)

        self.input.set_buffer(Gtk.TextBuffer())


class Window(Gtk.ApplicationWindow):
    LOADING = object()

    def __init__(self, bus, **kw):
        super(Window, self).__init__(title='Azul', **kw)
        self.bus = bus

        self.set_default_size(1400, 800)

        self.bus.connect('account-streams-loading',
                         ignore_first(self.on_account_streams_loading))
        self.bus.connect('account-streams-loaded',
                          ignore_first(self.on_account_streams_loaded))

        self.set_titlebar(HeaderBar(self.bus, self))

        self.grid = Gtk.Grid()
        self.add(self.grid)

        self.accounts = AccountsView(self.bus, self, vexpand=True)
        self.account_streams = {}
        self.account_streams_empty = EmptyListView(self.bus, vexpand=True)
        self.account_messages = {}
        self.account_messages_empty = Gtk.ListBox(expand=True)

        self.grid.attach(self.accounts, 0, 0, 1, 3)
        self.grid.attach(Gtk.Separator(), 1, 0, 1, 3)
        self.grid.attach(Gtk.Separator(), 3, 0, 1, 3)
        self.grid.attach(Gtk.Separator(), 4, 1, 1, 1)
        self.grid.attach(MessageEditor(self.bus), 4, 2, 1, 1)
        self._set_account()

        self.show_all()

    def quit(self, window):
        self.bus.quit()

    def _set_account(self, server=None):
        if server is None or server is self.LOADING:
            current_stream = self.account_streams_empty
            current_messages = self.account_messages_empty

            if server is self.LOADING:
                current_stream.set_loading()
        else:
            current_stream = self.account_streams[server]
            current_messages = self.account_messages[server]

        previous_stream = self.grid.get_child_at(2, 0)
        if previous_stream is not None and previous_stream is not current_stream:
            self.grid.remove(previous_stream)
        previous_messages = self.grid.get_child_at(4, 0)
        if previous_messages is not None and previous_messages is not current_messages:
            self.grid.remove(previous_messages)

        current_stream.set_size_request(300, 0)
        if previous_stream is not current_stream:
            self.grid.attach(current_stream, 2, 0, 1, 3)
            current_stream.clear_selection()
        if previous_messages is not current_messages:
            self.grid.attach(current_messages, 4, 0, 1, 1)

        current_stream.show_all()
        current_messages.show_all()

    def on_account_streams_loading(self):
        self._set_account(self.LOADING)

    def on_account_streams_loaded(self, account, streams):
        if account.server not in self.account_streams:
            self.account_streams[account.server] = StreamsView(self.bus, account,
                                                               streams, vexpand=True)
            self.account_messages[account.server] = MessagesView(self.bus, account,
                                                                 expand=True)
        self.bus.load_messages(account)
        self._set_account(account.server)


class Application(Gtk.Application):
    CSS = b'''
    @binding-set Submit {
      bind "<Control>Return" { "submit" () };
    }

    textview {
      -gtk-key-bindings: Submit;
      padding: 40px 20px 20px 20px;
    }

    combobox {
        border: none;
    }

    button.combo, entry.combo {
        background: none;
        border: none;
        box-shadow: none;
        outline: none;
    }
    '''

    MENU_XML = '''
    <?xml version="1.0" encoding="UTF-8"?>

    <interface>
        <menu id="app-menu">
            <section>
                <item>
                    <attribute name="action">app.about</attribute>
                    <attribute name="label" translatable="yes">_About</attribute>
                </item>
                <item>
                    <attribute name="action">app.quit</attribute>
                    <attribute name="label" translatable="yes">_Quit</attribute>
                    <attribute name="accel">&lt;Primary&gt;q</attribute>
                </item>
            </section>
        </menu>
    </interface>
    '''

    def __init__(self, bus, **kw):
        super(Application, self).__init__(
            application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE,
            **kw)
        self.bus = bus
        self.window = None

        self.bus.connect('account-streams-loaded',
                         ignore_first(self.on_account_streams_loaded))
        self.bus.connect('message-events', ignore_first(self.on_message_events))

    def on_account_streams_loaded(self, account, streams):
        pass

    def on_message_events(self, account, events):
        suffix = '(s)' if len(events) > 1 else ''
        body = []

        for event in events:
            message = event.message
            body.append(f'<b>{message.sender_name} in {message.topic_name}</b>: {message.content}')

        notification = Gio.Notification.new(f'New message{suffix} in {account.info.name}')
        notification.set_body('\n'.join(body))
        notification.set_priority(Gio.NotificationPriority.HIGH)
        self.send_notification('id', notification)

    def do_startup(self):
        Gtk.Application.do_startup(self)

        style_provider = Gtk.CssProvider()
        style_provider.load_from_data(self.CSS)

        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), style_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        about_action = Gio.SimpleAction.new('about', None)
        about_action.connect('activate', self.on_about)
        self.add_action(about_action)

        quit_action = Gio.SimpleAction.new('quit', None)
        quit_action.connect('activate', self.on_quit)
        self.add_action(quit_action)

        builder = Gtk.Builder.new_from_string(self.MENU_XML, -1)
        self.set_app_menu(builder.get_object('app-menu'))

    def do_activate(self):
        if self.window is None:
            self.window = Window(self.bus, application=self)

        self.window.present()

    def do_command_line(self, cmdline):
        self.activate()
        return 0

    def on_about(self, action, data):
        about_dialog = Gtk.AboutDialog(transient_for=self.window, modal=True)
        about_dialog.present()

    def on_quit(self, action, data):
        self.quit()


def main():
    bus = EventBus()
    app = Application(bus)
    app.run(sys.argv)
    bus.quit()


if __name__ == '__main__':
    main()
