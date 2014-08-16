import asyncio
import enum
import logging

logger = logging.getLogger("cloudbot")


@enum.unique
class EventType(enum.Enum):
    message = 0
    action = 1
    join = 2
    part = 3
    kick = 4
    topic = 5
    nick = 6
    quit = 7
    other = 8


class Event:
    """
    :type bot: cloudbot.bot.CloudBot
    :type conn: cloudbot.connection.Connection
    :type hook: cloudbot.plugin.Hook
    :type type: EventType
    :type content: str
    :type target: str
    :type chan_name: str
    :type channel: cloudbot.connection.Channel
    :type channels: list(cloudbot.connection.Channel)
    :type nick: str
    :type user: str
    :type host: str
    :type mask: str
    :type irc_raw: str
    :type irc_command: str
    :type irc_command_params: str
    :type irc_ctcp_text: str
    :param: channels: A list of channels which the event affects. Only used for events which affect multiple channels,
            such as NICK or QUIT events.
    """

    def __init__(self, *, bot=None, hook=None, conn=None, base_event=None, event_type=EventType.other, content=None,
                 target=None, channel_name=None, nick=None, user=None, host=None, mask=None, irc_raw=None,
                 irc_command=None, irc_command_params=None, irc_ctcp_text=None):
        """
        All of these parameters except for `bot` and `hook` are optional.
        The irc_* parameters should only be specified for IRC events.

        Note that the `bot` argument may be left out if you specify a `base_event`.

        :param bot: The CloudBot instance this event was triggered from
        :param conn: The Connection instance this event was triggered from
        :param hook: The hook this event will be passed to
        :param base_event: The base event that this event is based on. If this parameter is not None, then nick, user,
                            host, mask, and irc_* arguments are ignored
        :param event_type: The type of the event
        :param content: The content of the message, or the reason for an join or part
        :param target: The target of the action, for example the user being kicked, or invited
        :param channel_name: The channel that this action took place in
        :param nick: The nickname of the sender that triggered this event
        :param user: The user of the sender that triggered this event
        :param host: The host of the sender that triggered this event
        :param mask: The mask of the sender that triggered this event (nick!user@host)
        :param irc_raw: The raw IRC line
        :param irc_command: The IRC command
        :param irc_command_params: The list of params for the IRC command. If the last param is a content param, the ':'
                                should be removed from the front.
        :param irc_ctcp_text: CTCP text if this message is a CTCP command
        :type bot: cloudbot.bot.CloudBot
        :type conn: cloudbot.connection.Connection
        :type hook: cloudbot.plugin.Hook
        :type base_event: cloudbot.event.Event
        :type content: str
        :type target: str
        :type event_type: EventType
        :type nick: str
        :type user: str
        :type host: str
        :type mask: str
        :type irc_raw: str
        :type irc_command: str
        :type irc_command_params: list[str]
        :type irc_ctcp_text: str
        """
        self.bot = bot
        self.conn = conn
        self.hook = hook
        if base_event is not None:
            # We're copying an event, so inherit values
            if self.bot is None and base_event.bot is not None:
                self.bot = base_event.bot
            if self.conn is None and base_event.conn is not None:
                self.conn = base_event.conn
            if self.hook is None and base_event.hook is not None:
                self.hook = base_event.hook

            # If base_event is provided, don't check these parameters, just inherit
            self.type = base_event.type
            self.content = base_event.content
            self.target = base_event.target
            self.chan_name = base_event.chan_name
            self.nick = base_event.nick
            self.user = base_event.user
            self.host = base_event.host
            self.mask = base_event.mask
            self.channel = base_event.channel
            self.channels = base_event.channel
            # irc-specific parameters
            self.irc_raw = base_event.irc_raw
            self.irc_command = base_event.irc_command
            self.irc_command_params = base_event.irc_command_params
            self.irc_ctcp_text = base_event.irc_ctcp_text
        else:
            # Since base_event wasn't provided, we can take these parameters
            self.type = event_type
            self.content = content
            self.target = target
            self.chan_name = channel_name
            self.nick = nick
            self.user = user
            self.host = host
            self.mask = mask
            # channel and channels are assigned in Connection.pre_process_event
            self.channel = None
            self.channels = []
            # irc-specific parameters
            self.irc_raw = irc_raw
            self.irc_command = irc_command
            self.irc_command_params = irc_command_params
            self.irc_ctcp_text = irc_ctcp_text

    @property
    def event(self):
        """
        :rtype: Event
        """
        return self

    @property
    def loop(self):
        """
        :rtype: asyncio.events.AbstractEventLoop
        """
        return self.bot.loop

    @property
    def db(self):
        return self.bot.db

    def message(self, *messages, target=None):
        """sends a message to a specific or current channel/user
        :type message: list[str]
        :type target: str
        """
        if target is None:
            if self.chan_name is None:
                raise ValueError("Target must be specified when chan is not assigned")
            target = self.chan_name
        self.conn.message(target, *messages)

    def reply(self, *messages, target=None):
        """sends a message to the current channel/user with a prefix
        :type messages: str
        :type target: str
        """
        if target is None:
            if self.chan_name is None:
                raise ValueError("Target must be specified when chan is not assigned")
            target = self.chan_name

        if not messages:  # if there are no messages specified, don't do anything
            return

        if target == self.nick:
            self.conn.message(target, *messages)
        else:
            self.conn.message(target, "({}) {}".format(self.nick, messages[0]), *messages[1:])

    def action(self, message, target=None):
        """sends an action to the current channel/user or a specific channel/user
        :type message: str
        :type target: str
        """
        if target is None:
            if self.chan_name is None:
                raise ValueError("Target must be specified when chan is not assigned")
            target = self.chan_name

        self.conn.action(target, message)

    def ctcp(self, message, ctcp_type, target=None):
        """sends an ctcp to the current channel/user or a specific channel/user
        :type message: str
        :type ctcp_type: str
        :type target: str
        """
        if target is None:
            if self.chan_name is None:
                raise ValueError("Target must be specified when chan is not assigned")
            target = self.chan_name
        if not hasattr(self.conn, "ctcp"):
            raise ValueError("CTCP can only be used on IRC connections")
        # noinspection PyUnresolvedReferences
        self.conn.ctcp(target, ctcp_type, message)

    def notice(self, message, target=None):
        """sends a notice to the current channel/user or a specific channel/user
        :type message: str
        :type target: str
        """
        if target is None:
            if self.nick is None:
                raise ValueError("Target must be specified when nick is not assigned")
            target = self.nick

        self.conn.notice(target, message)

    def has_permission(self, permission, notice=True):
        """ returns whether or not the current user has a given permission
        :type permission: str
        :rtype: bool
        """
        if not self.mask:
            raise ValueError("has_permission requires mask to be assigned")
        return self.conn.permissions.has_perm_mask(self.mask, permission, notice=notice)

    @asyncio.coroutine
    def async(self, function, *args, **kwargs):
        return (yield from self.loop.run_in_executor(None, lambda: function(*args, **kwargs)))


class CommandEvent(Event):
    """
    :type hook: cloudbot.plugin.CommandHook
    :type text: str
    :type triggered_command: str
    """

    def __init__(self, *, bot=None, hook, text, triggered_command, conn=None, base_event=None, event_type=None,
                 content=None, target=None, channel_name=None, nick=None, user=None, host=None, mask=None, irc_raw=None,
                 irc_command=None, irc_command_params=None):
        """
        :param text: The arguments for the command
        :param triggered_command: The command that was triggered
        :type text: str
        :type triggered_command: str
        """
        super().__init__(bot=bot, hook=hook, conn=conn, base_event=base_event, event_type=event_type, content=content,
                         target=target, channel_name=channel_name, nick=nick, user=user, host=host, mask=mask,
                         irc_raw=irc_raw,
                         irc_command=irc_command, irc_command_params=irc_command_params)
        self.hook = hook
        self.text = text
        self.triggered_command = triggered_command

    def notice_doc(self, target=None):
        """sends a notice containing this command's docstring to the current channel/user or a specific channel/user
        :type target: str
        """
        if self.triggered_command is None:
            raise ValueError("Triggered command not set on this event")
        if self.hook.doc is None:
            message = "{}{} requires additional arguments.".format(self.conn.config["command_prefix"],
                                                                   self.triggered_command)
        else:
            # this is using the new format of `<args> - doc`
            message = "{}{} {}".format(self.conn.config["command_prefix"], self.triggered_command, self.hook.doc)

        self.notice(message, target=target)


class RegexEvent(Event):
    """
    :type hook: cloudbot.plugin.RegexHook
    :type match: re.__Match
    """

    def __init__(self, *, bot=None, hook, match, conn=None, base_event=None, event_type=None, content=None, target=None,
                 channel_name=None, nick=None, user=None, host=None, mask=None, irc_raw=None, irc_command=None,
                 irc_command_params=None):
        """
        :param: match: The match objected returned by the regex search method
        :type match: re.__Match
        """
        super().__init__(bot=bot, conn=conn, hook=hook, base_event=base_event, event_type=event_type, content=content,
                         target=target, channel_name=channel_name, nick=nick, user=user, host=host, mask=mask,
                         irc_raw=irc_raw,
                         irc_command=irc_command, irc_command_params=irc_command_params)
        self.match = match