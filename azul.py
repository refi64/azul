#!/usr/bin/env python

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
import io
import keyring
import math
import mistune
import queue
import requests
import sys
import threading
import webbrowser
import zulip


APP_ID = 'com.refi64.azul'


def ignore_first(func):
    return lambda first, *args, **kwargs: func(*args, **kwargs)


def construct_with_mapped_args(ty, data):
    args = {}

    for key, value in data.items():
        if key not in ty.mapping:
            continue

        target_key = ty.mapping[key]
        args[target_key or key] = value

    return ty(**args)


# Monkey-patch to allow getting the server icon.
old_json = requests.Response.json

def new_json(resp):
    if 'realm/icon' in resp.url or 'gravatar' in resp.url:
        return {'result': 'success', 'icon': resp.content}
    return old_json(resp)

requests.Response.json = new_json


class ValidationError(Exception):
    pass


@attr.s
class AccountInfo:
    icon = attr.ib(default=None, repr=False)


@attr.s
class AccountModel:
    index = attr.ib()
    server = attr.ib()
    email = attr.ib()
    apikey = attr.ib()
    _client = attr.ib(default=None)

    info = attr.ib(default=attr.Factory(AccountInfo))

    @property
    def client(self):
        if self._client is None:
            self._client = zulip.Client(site=self.server, email=self.email,
                                        api_key=self.apikey)
        return self._client


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
    max_id = attr.ib()
    name = attr.ib()

    mapping = {
        'max_id': None,
        'name': None,
    }

    @staticmethod
    def from_data(data):
        return construct_with_mapped_args(TopicModel, data)


@attr.s
class MessageModel:
    id = attr.ib()
    sender_name = attr.ib()
    sender_avatar = attr.ib()
    sender_id = attr.ib()
    topic_name = attr.ib()
    content = attr.ib(repr=False)

    mapping = {
        'id': None,
        'sender_full_name': 'sender_name',
        'avatar_url': 'sender_avatar',
        'sender_id': None,
        'subject': 'topic_name',
        'content': None,
    }

    @staticmethod
    def from_data(data):
        return construct_with_mapped_args(MessageModel, data)


@attr.s
class SearchNarrow:
    stream = attr.ib(default=None)
    topic = attr.ib(default=None)

    def to_data(self):
        result = []
        if self.stream is not None:
            result.append({'operator': 'stream', 'operand': self.stream.name})
        if self.topic is not None:
            result.append({'operator': 'topic', 'operand': self.topic.name})
        return result


class Task: pass


@attr.s
class GetApiKeyTask(Task):
    account = attr.ib()
    password = attr.ib()

    def process(self, bus):
        client = zulip.Client(site=self.account.server, email='', api_key='')
        result = client.call_endpoint(url='fetch_api_key', method='POST',
                                      request={'username': self.account.email,
                                               'password': self.password})
        self.account.apikey = result['api_key']
        bus.emit_from_main_thread('api-key-retrieved', self.account)


@attr.s
class LoadDataTask(Task):
    url = attr.ib()

    def process(self, bus):
        data = requests.get(self.url).content
        bus.emit_from_main_thread('data-loaded', self.url, data)


@attr.s
class LoadInfoTask(Task):
    account = attr.ib()

    def process(self, bus):
        result = self.account.client.call_endpoint(url='realm/icon', method='GET')
        assert 'icon' in result, result
        self.account.info.icon = result['icon']
        bus.emit_from_main_thread('account-info-loaded', self.account)


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
    narrow = attr.ib()
    anchor = attr.ib(default=0)

    def process(self, bus):
        narrow = self.narrow.to_data()
        result = self.account.client.call_endpoint(url='messages', method='GET',
                                                   request={'anchor': self.anchor,
                                                            'num_before': 0,
                                                            'num_after': 100,
                                                            'narrow': narrow,
                                                            'apply_markdown': False})
        messages = list(map(MessageModel.from_data, result['messages']))

        bus.emit_from_main_thread('messages-loaded', self.account, self.narrow,
                                  self.anchor, messages)


class TaskThread(threading.Thread):
    def __init__(self, bus):
        super(TaskThread, self).__init__()
        self.bus = bus
        self._tasks = queue.Queue()

    def add_task(self, task):
        self._tasks.put(task, block=False)

    def run(self):
        while True:
            task = self._tasks.get()
            if task is None:
                break

            try:
                task.process(self.bus)
            finally:
                self._tasks.task_done()


class EventBus(GObject.Object):
    def __init__(self, window):
        super(EventBus, self).__init__()

        self.window = window

        self.settings = Gio.Settings(APP_ID)

        self.connect('api-key-retrieved', ignore_first(self.on_api_key_retrieved))

        self._thread = TaskThread(self)
        self._thread.start()

        self._accounts = {}
        self._load_accounts()

    def quit(self):
        self._thread.add_task(None)

    def emit_from_main_thread(self, signal, *args):
        GLib.idle_add(lambda: self.emit(signal, *args))

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

    def _save_account_data(self, accounts=None):
        accounts = accounts or self._accounts.values()
        account_data = [(account.server, account.email, account.apikey or '')
                        for account in accounts]
        self.settings.set_value('accounts', GLib.Variant('a(sss)', account_data))

    def add_account(self, account, password):
        if not account.server:
            raise ValidationError('Invalid server.')
        if '@' not in account.email:
            raise ValidationError('Invalid email.')
        if not password:
            raise ValidationError('Invalid password.')

        if account.server in self._accounts:
            raise ValidationError('An account for this server already exists.')

        self._save_account_data([*self._accounts.values(), account])
        account.index = len(self._accounts)
        self._accounts[account.server] = account

        keyring.set_password(account.server, account.email, password)

        self._load_account(account)

    def _load_account(self, account, password=None):
        if not account.apikey:
            if password is None:
                password = keyring.get_password(account.server, account.email)
            self._thread.add_task(GetApiKeyTask(account, password))
        else:
            self._thread.add_task(LoadInfoTask(account))

    def load_data_from_url(self, url):
        self._thread.add_task(LoadDataTask(url))

    def load_streams_for_account(self, account):
        self._thread.add_task(LoadStreamsTask(account))

    def load_messages(self, account, narrow=None):
        self._thread.add_task(LoadMessagesTask(account, narrow or SearchNarrow()))

    def load_topics_in_stream(self, account, stream):
        self._thread.add_task(LoadTopicsTask(account, stream))

    def on_api_key_retrieved(self, account):
        self._save_account_data()
        self._load_account(account)

    @GObject.Signal(name='api-key-retrieved', arg_types=(object,))
    def api_key_retrieved(self, account): pass

    @GObject.Signal(name='data-loaded', arg_types=(str, object))
    def data_loaded(self, url, data): pass

    @GObject.Signal(name='account-info-loaded', arg_types=(object,))
    def account_info_loaded(self, account): pass

    @GObject.Signal(name='account-streams-loading')
    def account_streams_loading(self): pass

    @GObject.Signal(name='account-streams-loaded', arg_types=(object, object))
    def account_streams_loaded(self, account, streams): pass

    @GObject.Signal(name='stream-topics-loaded', arg_types=(object, object, object))
    def account_topics_loaded(self, account, stream, topics): pass

    @GObject.Signal(name='messages-loaded', arg_types=(object, object, object, object))
    def messages_loaded(self, account, narrow, anchor, messages): pass


class AddServerDialog(Gtk.Dialog):
    def __init__(self, parent):
        super(AddServerDialog, self).__init__('Add Server', parent, 0,
            ('_Save', Gtk.ResponseType.APPLY, '_Cancel', Gtk.ResponseType.CANCEL))
        self.set_default_size(500, 0)

        self.get_action_area().set_property('margin', 5)

        content = self.get_content_area()
        grid = Gtk.Grid(column_spacing=10, row_spacing=10, margin=10)

        self.error_bar = Gtk.InfoBar()
        self.error_label = Gtk.Label(label='')
        self.error_bar.get_content_area().add(self.error_label)
        self.error_bar.add_button('_Close', Gtk.ResponseType.OK)
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
        return (AccountModel(index=-1, email=self.email.get_text(),
                             server=self.server.get_text(), apikey=None),
                self.password.get_text())

    def show_failure(self, message):
        self.error_label.set_label(message)
        self.error_bar.show_all()


class DataImage(Gtk.Image):
    def __init__(self, size, data):
        super(DataImage, self).__init__()

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

        add_button = Gtk.Button(image=Gtk.Image.new_from_icon_name('list-add-symbolic',
                                                                   Gtk.IconSize.BUTTON))
        add_button.set_size_request(self.REALM_ICON_SIZE, self.REALM_ICON_SIZE)
        add_button.get_style_context().add_class('circular')
        add_button.connect('clicked', ignore_first(self.on_add_button_click))
        self.add(add_button)

        for i, account in enumerate(self.bus.accounts):
            self.add_account(account, i)

        self.bus.connect('account-info-loaded', ignore_first(self.update_account))
        self.connect('row-activated', ignore_first(self.on_account_selected))

    def add_account(self, account, index=None):
        spinner = Gtk.Spinner()
        spinner.set_size_request(self.REALM_ICON_SIZE, self.REALM_ICON_SIZE)
        spinner.start()

        self.widgets[account.server] = spinner
        self.insert(spinner, index if index is not None
                                   else len(self.bus.accounts) - 1)

    def update_account(self, account):
        widget = self.widgets[account.server]
        self.remove(widget.get_parent())

        image = CircularImage(self.REALM_ICON_SIZE, account.info.icon)
        self.insert(image, account.index)

        self.show_all()

    def on_add_button_click(self):
        dialog = AddServerDialog(self.parent)

        while True:
            response = dialog.run()
            if response != Gtk.ResponseType.APPLY:
                break

            account, password = dialog.get_account()
            try:
                self.bus.add_account(account, password)
            except ValidationError as ex:
                dialog.show_failure(str(ex))
            else:
                self.add_account(account)
                break

        dialog.destroy()

    def on_account_selected(self, row):
        self.bus.emit('account-streams-loading')

        index = row.get_index()
        account = self.bus.accounts[index]
        assert account.index == index

        self.bus.load_streams_for_account(account)


class EmptyListView(Gtk.ListBox):
    def __init__(self, **kwargs):
        super(EmptyListView, self).__init__(**kwargs)

        placeholder = Gtk.Label(label='Click a server on the left.', margin=10,
                                sensitive=False)
        placeholder.show()
        self.set_placeholder(placeholder)

        self.loading = False

    def set_loading(self):
        if self.loading:
            return

        spinner = Gtk.Spinner(margin=120)
        spinner.start()
        spinner.show()
        self.set_placeholder(spinner)


class StreamsView(Gtk.Bin):
    def __init__(self, bus, account, streams, **kwargs):
        super(StreamsView, self).__init__(**kwargs)
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

    def on_stream_selected(self):
        _, it = self.tree.get_selection().get_selected()
        if it is None:
            return

        path = self.store.get_path(it).get_indices()
        stream = self.streams[self.store[path[0]][0][1:]]
        topic = self.stream_topics[stream.name][self.store[path][0]] if len(path) == 2 \
                                                                     else None

        self.bus.load_messages(self.account, SearchNarrow(stream=stream, topic=topic))

    def on_stream_topics_loaded(self, account, stream, topics):
        if account is not self.account:
            return

        for topic in topics:
            self.stream_topics[stream.name][topic.name] = topic
            self.store.append(self.stream_iters[stream.name], [topic.name])


class AvatarView(Gtk.Bin):
    _AVATAR_SIZE = 48
    _cache = {}

    def __init__(self, bus, url, **kwargs):
        super(AvatarView, self).__init__(**kwargs)
        self.set_size_request(self._AVATAR_SIZE, self._AVATAR_SIZE)
        self.bus = bus
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
            self.bus.load_data_from_url(url)

    def on_data_loaded(self, url, data):
        if url != self.url:
            return

        if self.id is not None:
            self.bus.disconnect(self.id)

        self._cache[url] = data

        self.image = DataImage(self._AVATAR_SIZE, data)
        self.image.show()
        self.add(self.image)


class MarkdownView(Gtk.Grid):
    class PangoRenderer:
        def __init__(self):
            pass

    def __init__(self, content, **kwargs):
        super(MarkdownView, self).__init__(**kwargs)
        self.attach(Gtk.Label(label=content, wrap=True), 0, 0, 1, 1)

        self.show_all()

    def activate_link(self, uri):
        print(uri)
        return True


class MessagesFromSenderView(Gtk.Grid):
    def __init__(self, bus, account, messages, **kwargs):
        super(MessagesFromSenderView, self).__init__(**kwargs)
        self.bus = bus
        self.account = account
        self.messages = messages

        self.set_column_spacing(10)

        assert messages
        first = messages[0]

        avatar = AvatarView(self.bus, first.sender_avatar, valign=Gtk.Align.START)
        self.attach(avatar, 0, 0, 1, 5)

        name = Gtk.Label(halign=Gtk.Align.START)
        name.set_markup(f'<b>{GLib.markup_escape_text(first.sender_name)}</b>')
        self.attach(name, 1, 0, 1, 1)

        view = MarkdownView(''.join(message.content for message in messages))
        self.attach(view, 1, 1, 1, 1)


class TopicView(Gtk.Grid):
    def __init__(self, bus, account, name, **kwargs):
        super(TopicView, self).__init__(**kwargs)
        self.bus = bus
        self.account = account
        self.name = name

        label = Gtk.Label(hexpand=True)
        label.set_markup(f'<b>{GLib.markup_escape_text(name)}</b>')
        self.attach(label, 0, 0, 1, 1)

        self.bottom = label

    def add_messages_from_sender(self, messages):
        sender_messages_view = MessagesFromSenderView(self.bus, self.account, messages)
        self.attach_next_to(sender_messages_view, self.bottom, Gtk.PositionType.BOTTOM,
                            1, 1)
        self.bottom = sender_messages_view

        self.show_all()


class MessagesView(Gtk.ScrolledWindow):
    def __init__(self, bus, account, **kwargs):
        super(MessagesView, self).__init__(**kwargs)
        self.bus = bus
        self.account = account

        self.listbox = Gtk.ListBox(expand=True)
        self.add(self.listbox)

        self.active_messages = set()
        self.message_views = {}

        self.bus.connect('messages-loaded', ignore_first(self.on_messages_loaded))

    def on_messages_loaded(self, account, narrow, anchor, messages):
        if account is not self.account:
            return

        self.remove(self.listbox)
        self.listbox = Gtk.ListBox(expand=True)
        self.listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self.add(self.listbox)

        topic_view = None
        messages_from_sender = []

        for index, (previous, message) in enumerate(zip([None, *messages], messages)):
            if messages_from_sender and (previous.sender_id != message.sender_id or
                                         previous.topic_name != message.topic_name):
                topic_view.add_messages_from_sender(messages_from_sender)
                messages_from_sender = []

            if previous is None or previous.topic_name != message.topic_name:
                messages_from_sender = []
                topic_view = TopicView(self.bus, account, message.topic_name)
                self.listbox.add(topic_view)

            messages_from_sender.append(message)

        if messages_from_sender:
            topic_view.add_messages_from_sender(messages_from_sender)

        self.show_all()


class Window(Gtk.ApplicationWindow):
    LOADING = object()

    def __init__(self):
        super(Window, self).__init__(title='Azul')
        self.bus = EventBus(self)

        self.set_default_size(1000, 600)

        self.bus.connect('account-streams-loading',
                         ignore_first(self.on_account_streams_loading))
        self.bus.connect('account-streams-loaded',
                          ignore_first(self.on_account_streams_loaded))

        self.grid = Gtk.Grid()
        self.add(self.grid)

        self.accounts = AccountsView(self.bus, self, vexpand=True)
        self.account_streams = {}
        self.account_streams_empty = EmptyListView(vexpand=True)
        self.account_messages = {}
        self.account_messages_empty = Gtk.ListBox(expand=True)

        self.grid.attach(self.accounts, 0, 0, 1, 1)
        self._set_account()

    def quit(self, window):
        self.bus.quit()
        Gtk.main_quit()

    def _set_account(self, server=None):
        previous_stream = self.grid.get_child_at(1, 0)
        if previous_stream is not None:
            self.grid.remove(previous_stream)
        previous_messages = self.grid.get_child_at(2, 0)
        if previous_messages is not None:
            self.grid.remove(previous_messages)

        if server is None or server is self.LOADING:
            current_stream = self.account_streams_empty
            current_messages = self.account_messages_empty

            if server is self.LOADING:
                current_stream.set_loading()
        else:
            current_stream = self.account_streams[server]
            current_messages = self.account_messages[server]

        current_stream.set_size_request(400, 0)
        self.grid.attach(current_stream, 1, 0, 1, 1)
        self.grid.attach(current_messages, 2, 0, 1, 1)

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


if __name__ == '__main__':
    win = Window()
    win.connect('destroy', win.quit)
    win.show_all()
    Gtk.main()
