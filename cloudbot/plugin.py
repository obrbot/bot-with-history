import asyncio
import enum
import glob
import importlib
import inspect
import logging
import os
import re
import itertools

from cloudbot.event import Event

logger = logging.getLogger("cloudbot")


class HookType(enum.Enum):
    on_start = 1,
    on_stop = 2,
    sieve = 3,
    event = 4,
    regex = 5,
    command = 6,
    irc_raw = 7,


def find_hooks(title, module):
    """
    :type title: str
    :type module: object
    :rtype: dict[HookType, list[Hook]
    """
    # set the loaded flag
    module._plugins_loaded = True
    hooks_dict = dict()
    for hook_type in HookType:
        hooks_dict[hook_type] = list()

    for name, func in module.__dict__.items():
        if hasattr(func, "plugin_hook"):
            # if it has cloudbot hook
            func_hooks = func.plugin_hook

            for hook_type, func_hook in func_hooks.items():
                hooks_dict[hook_type].append(_hook_type_to_plugin[hook_type](title, func_hook))

            # delete the hook to free memory
            del func.plugin_hook

    return hooks_dict


def _prepare_parameters(hook, event):
    """
    Prepares arguments for the given hook

    :type hook: cloudbot.plugin.Hook
    :type event: cloudbot.event.Event
    :rtype: list
    """
    parameters = []
    for required_arg in hook.required_args:
        if hasattr(event, required_arg):
            value = getattr(event, required_arg)
            parameters.append(value)
        else:
            logger.warning("Plugin {} asked for invalid argument '{}', cancelling execution!"
                           .format(hook.description, required_arg))
            logger.debug("Valid arguments are: {}".format(dir(event)))
            return None
    return parameters


class PluginManager:
    """
    PluginManager is the core of CloudBot plugin loading.

    PluginManager loads Plugins, and adds their Hooks to easy-access dicts/lists.

    Each Plugin represents a file, and loads hooks onto itself using find_hooks.

    Plugins are the lowest level of abstraction in this class. There are four different plugin types:
    - CommandPlugin is for bot commands
    - RawPlugin hooks onto irc_raw irc lines
    - RegexPlugin loads a regex parameter, and executes on irc lines which match the regex
    - SievePlugin is a catch-all sieve, which all other plugins go through before being executed.

    :type bot: cloudbot.bot.CloudBot
    :type commands: dict[str, CommandHook]
    :type raw_triggers: dict[str, list[RawHook]]
    :type catch_all_triggers: list[RawHook]
    :type event_type_hooks: dict[cloudbot.event.EventType, list[EventHook]]
    :type regex_hooks: list[(re.__Regex, RegexHook)]
    :type sieves: list[SieveHook]
    """

    def __init__(self, bot):
        """
        Creates a new PluginManager. You generally only need to do this from inside cloudbot.bot.CloudBot
        :type bot: cloudbot.bot.CloudBot
        """
        self.bot = bot

        self.commands = {}
        self.raw_triggers = {}
        self.catch_all_triggers = []
        self.event_type_hooks = {}
        self.regex_hooks = []
        self.sieves = []
        self.shutdown_hooks = []
        self._hook_locks = {}

    @asyncio.coroutine
    def load_all(self, plugin_dir):
        """
        Load a plugin from each *.py file in the given directory.

        :type plugin_dir: str
        """
        path_list = glob.iglob(os.path.join(plugin_dir, '*.py'))
        # Load plugins asynchronously :O
        yield from asyncio.gather(*[self.load_plugin(path) for path in path_list], loop=self.bot.loop)

    @asyncio.coroutine
    def load_plugin(self, path):
        """
        Loads a plugin from the given path and plugin object, then registers all hooks from that plugin.

        :type path: str
        """
        file_path = os.path.abspath(path)
        title = os.path.splitext(os.path.basename(path))[0]

        module_name = "plugins.{}".format(title)
        try:
            plugin_module = importlib.import_module(module_name)
        except Exception:
            logger.exception("Error loading {}:".format(file_path))
            return

        hooks = find_hooks(title, plugin_module)

        # proceed to register hooks

        # run on_start hooks
        for on_start_hook in hooks[HookType.on_start]:
            success = yield from self.launch(on_start_hook, Event(bot=self.bot, hook=on_start_hook))
            if not success:
                logger.warning("Not registering hooks from plugin {}: on_start hook errored".format(title))
                return

        # register events
        for event_hook in hooks[HookType.event]:
            for event_type in event_hook.types:
                if event_type in self.event_type_hooks:
                    self.event_type_hooks[event_type].append(event_hook)
                else:
                    self.event_type_hooks[event_type] = [event_hook]
            self._log_hook(event_hook)

        # register commands
        for command_hook in hooks[HookType.command]:
            for alias in command_hook.aliases:
                if alias in self.commands:
                    logger.warning(
                        "Plugin {} attempted to register command {} which was already registered by {}. "
                        "Ignoring new assignment.".format(title, alias, self.commands[alias].plugin))
                else:
                    self.commands[alias] = command_hook
            self._log_hook(command_hook)

        # register raw hooks
        for raw_hook in hooks[HookType.irc_raw]:
            if raw_hook.is_catch_all():
                self.catch_all_triggers.append(raw_hook)
            else:
                for trigger in raw_hook.triggers:
                    if trigger in self.raw_triggers:
                        self.raw_triggers[trigger].append(raw_hook)
                    else:
                        self.raw_triggers[trigger] = [raw_hook]
            self._log_hook(raw_hook)

        # register regex hooks
        for regex_hook in hooks[HookType.regex]:
            for regex_match in regex_hook.regexes:
                self.regex_hooks.append((regex_match, regex_hook))
            self._log_hook(regex_hook)

        # register sieves
        for sieve_hook in hooks[HookType.sieve]:
            self.sieves.append(sieve_hook)
            self._log_hook(sieve_hook)

        # register shutdown hooks
        for stop_hook in hooks[HookType.on_stop]:
            self.shutdown_hooks.append(stop_hook)
            self._log_hook(stop_hook)

    def _log_hook(self, hook):
        """
        Logs registering a given hook

        :type hook: Hook
        """
        if self.bot.config.get("logging", {}).get("show_plugin_loading", True):
            logger.info("Loaded {}".format(hook))
            logger.debug("Loaded {}".format(repr(hook)))

    @asyncio.coroutine
    def _execute_hook(self, hook, event):
        """
        Runs the specific hook with the given bot and event.

        Returns False if the hook errored, True otherwise.

        :type hook: cloudbot.plugin.Hook
        :type event: cloudbot.event.Event
        :rtype: bool
        """
        parameters = _prepare_parameters(hook, event)
        if parameters is None:
            return False

        try:
            # _internal_run_threaded and _internal_run_coroutine prepare the database, and run the hook.
            # _internal_run_* will prepare parameters and the database session, but won't do any error catching.
            if hook.threaded:
                out = yield from self.bot.loop.run_in_executor(None, hook.function, *parameters)
            else:
                out = yield from hook.function(*parameters)
        except Exception:
            logger.exception("Error in hook {}".format(hook.description))
            event.message("Error in plugin '{}'.".format(hook.plugin))
            return False

        if out is not None:
            if isinstance(out, (list, tuple)):
                # if there are multiple items in the response, return them on multiple lines
                event.reply(*out)
            else:
                event.reply(*str(out).split('\n'))

        return True

    @asyncio.coroutine
    def _sieve(self, sieve, event, hook):
        """
        :type sieve: cloudbot.plugin.Hook
        :type event: cloudbot.event.Event
        :type hook: cloudbot.plugin.Hook
        :rtype: cloudbot.event.Event
        """
        try:
            if sieve.threaded:
                result = yield from self.bot.loop.run_in_executor(None, sieve.function, event)
            else:
                result = yield from sieve.function(event)
        except Exception:
            logger.exception("Error running sieve {} on {}:".format(sieve.description, hook.description))
            return None
        else:
            return result

    @asyncio.coroutine
    def launch(self, hook, event):
        """
        Dispatch a given event to a given hook using a given bot object.

        Returns False if the hook didn't run successfully, and True if it ran successfully.

        :type event: cloudbot.event.Event | cloudbot.event.CommandEvent
        :type hook: cloudbot.plugin.Hook | cloudbot.plugin.CommandHook
        :rtype: bool
        """
        if hook.type not in (HookType.on_start, HookType.on_stop):  # we don't need sieves on on_start or on_stop hooks.
            for sieve in self.bot.plugin_manager.sieves:
                event = yield from self._sieve(sieve, event, hook)
                if event is None:
                    return False

        if hook.type is HookType.command and hook.auto_help and not event.text and hook.doc is not None:
            event.notice_doc()
            return False

        if hook.single_thread:
            # There should only be once instance of this hook running at a time, so let's use a lock for it.
            key = (hook.plugin, hook.function_name)
            if key not in self._hook_locks:
                self._hook_locks[key] = asyncio.Lock(loop=self.bot.loop)

            # Run the plugin with the message, and wait for it to finish
            with (yield from self._hook_locks[key]):
                result = yield from self._execute_hook(hook, event)
        else:
            # Run the plugin with the message, and wait for it to finish
            result = yield from self._execute_hook(hook, event)

        # Return the result
        return result

    @asyncio.coroutine
    def run_shutdown_hooks(self):
        yield from asyncio.gather((self.launch(hook, Event(bot=self.bot, hook=hook)) for hook in self.shutdown_hooks),
                                  loop=self.bot.loop)


class Hook:
    """
    Each hook is specific to one function. This class is never used by itself, rather extended.

    :type type: HookType
    :type plugin: str
    :type function: callable
    :type function_name: str
    :type required_args: list[str]
    :type threaded: bool
    :type run_first: bool
    :type permissions: list[str]
    :type single_thread: bool
    """
    type = None  # to be assigned in subclasses

    def __init__(self, plugin, func_hook):
        """
        :type plugin: str
        :type func_hook: hook._Hook
        """
        self.plugin = plugin
        self.function = func_hook.function
        self.function_name = self.function.__name__

        self.required_args = inspect.getargspec(self.function)[0]
        if self.required_args is None:
            self.required_args = []

        if asyncio.iscoroutine(self.function) or asyncio.iscoroutinefunction(self.function):
            self.threaded = False
        else:
            self.threaded = True

        self.permissions = func_hook.kwargs.pop("permissions", [])
        self.single_thread = func_hook.kwargs.pop("single_instance", False)
        self.run_first = func_hook.kwargs.pop("run_first", False)

        if func_hook.kwargs:
            # we should have popped all the args, so warn if there are any left
            logger.warning("Ignoring extra args {} from {}".format(func_hook.kwargs, self.description))

    @property
    def description(self):
        return "{}:{}".format(self.plugin, self.function_name)

    def __repr__(self, **kwargs):
        result = "type: {}, plugin: {}, permissions: {}, ensure_first: {}, single_thread: {}, threaded: {}".format(
            self.type.name, self.plugin, self.permissions, self.run_first, self.single_thread, self.threaded)
        if kwargs:
            result = ", ".join(itertools.chain(("{}: {}".format(*item) for item in kwargs.items()), (result,)))

        return "{}[{}]".format(type(self).__name__, result)


class CommandHook(Hook):
    """
    :type name: str
    :type aliases: list[str]
    :type doc: str
    :type auto_help: bool
    """
    type = HookType.command

    def __init__(self, plugin, cmd_hook):
        """
        :type plugin: str
        :type cmd_hook: cloudbot.util.hook._CommandHook
        """
        self.auto_help = cmd_hook.kwargs.pop("autohelp", True)

        self.name = cmd_hook.main_alias
        self.aliases = list(cmd_hook.aliases)  # turn the set into a list
        self.aliases.remove(self.name)
        self.aliases.insert(0, self.name)  # make sure the name, or 'main alias' is in position 0
        self.doc = cmd_hook.doc

        super().__init__(plugin, cmd_hook)

    def __repr__(self):
        return super().__repr__(name=self.name, aliases=self.aliases[1:])

    def __str__(self):
        return "command {} from {}".format("/".join(self.aliases), self.plugin)


class RegexHook(Hook):
    """
    :type regexes: set[re.__Regex]
    """
    type = HookType.regex

    def __init__(self, plugin, regex_hook):
        """
        :type plugin: Plugin
        :type regex_hook: cloudbot.util.hook._RegexHook
        """
        self.regexes = regex_hook.regexes

        super().__init__(plugin, regex_hook)

    def __repr__(self):
        return super().__repr__(triggers=", ".join(regex.pattern for regex in self.regexes))

    def __str__(self):
        return "regex {} from {}".format(self.function_name, self.plugin)


class RawHook(Hook):
    """
    :type triggers: set[str]
    """
    type = HookType.irc_raw

    def __init__(self, plugin, irc_raw_hook):
        """
        :type plugin: Plugin
        :type irc_raw_hook: cloudbot.util.hook._RawHook
        """
        super().__init__(plugin, irc_raw_hook)

        self.triggers = irc_raw_hook.triggers

    def is_catch_all(self):
        return "*" in self.triggers

    def __repr__(self):
        return super().__repr__(triggers=self.triggers)

    def __str__(self):
        return "irc raw {} ({}) from {}".format(self.function_name, ",".join(self.triggers), self.plugin)


class SieveHook(Hook):
    type = HookType.sieve

    def __init__(self, plugin, sieve_hook):
        """
        :type plugin: Plugin
        :type sieve_hook: cloudbot.util.hook._SieveHook
        """
        super().__init__(plugin, sieve_hook)

    def __str__(self):
        return "sieve {} from {}".format(self.function_name, self.plugin)


class EventHook(Hook):
    """
    :type types: set[cloudbot.event.EventType]
    """
    type = HookType.event

    def __init__(self, plugin, event_hook):
        """
        :type plugin: Plugin
        :type event_hook: cloudbot.util.hook._EventHook
        """
        super().__init__(plugin, event_hook)

        self.types = event_hook.types

    def __str__(self):
        return "event {} ({}) from {}".format(self.function_name, ",".join(str(t) for t in self.types),
                                              self.plugin)


class OnStartHook(Hook):
    type = HookType.on_start

    def __init__(self, plugin, on_load_hook):
        """
        :type plugin: Plugin
        :type on_load_hook: cloudbot.util.hook._OnStartHook
        """
        super().__init__(plugin, on_load_hook)

    def __str__(self):
        return "on_start {} from {}".format(self.function_name, self.plugin)


class OnStopHook(Hook):
    type = HookType.on_stop

    def __init__(self, plugin, on_load_hook):
        """
        :type plugin: Plugin
        :type on_load_hook: cloudbot.util.hook._OnStartHook
        """
        super().__init__(plugin, on_load_hook)

    def __str__(self):
        return "on_stop {} from {}".format(self.function_name, self.plugin)


_hook_type_to_plugin = {
    HookType.command: CommandHook,
    HookType.regex: RegexHook,
    HookType.irc_raw: RawHook,
    HookType.sieve: SieveHook,
    HookType.event: EventHook,
    HookType.on_start: OnStartHook,
    HookType.on_stop: OnStopHook,
}
