# import typing
import functools

# from DesViz import SubWindow

_recording_aliases = {}

# Requires a primary alias
def tag_command(primary_alias, *aliases):
    """
    Give a command a primary alias (and optionally more).
    command_name(cls, function_name) retrieves the primary alias

    decorate the parent class with @names_commands to get a class property "named_commands" with the list at that time.
    """
    def command(func):
        """Record a command's aliases, return it unchanged. Its name is the first non-primary alias."""
        cls = func.__qualname__.rsplit(".", 1)[0]
        if cls not in _recording_aliases:
            _recording_aliases[cls] = {}
        _recording_aliases[cls][func.__name__] = [primary_alias, func.__name__, *aliases]

        return func
    return command

def command_name(cls, func_name: str):
    """Get the primary alias of a tagged command from its name.
    Tag commands with @tag_command"""
    cls = cls.__qualname__
    
    return _recording_aliases.get(cls,{}).get(func_name,[None])[0]

def names_commands(cls): #There's probably a better way to do this. Oh well, the cost of this is a magic-overwrite. (Should probably be done by a parent-class inherited from this module with access to the scope. That can go in a later commit once the other changes are ready. TODO.)
    """Set a class proprty 'named_commands' to an {alias: name} dict for previously-tagged commands.
    No guarantees about inheritance."""

    class_aliases = _recording_aliases.get(cls.__name__)
    cls.named_commands = {alias: command for command, aliases in class_aliases.items() for alias in aliases}
    return cls

class ImpostorCallee:
    """Imitates another class for the aid of logging. Intercepts method calls and may run a chosen action with the arguments instead."""

    def __init__(self, original_class, action = None):
        """Make sure to pass a class not an instance.
        Action should have the following signature:
            action(original_func_name: str, *args, **kwargs)
        """
        self.imitated = original_class
        self.action = action

    def __getattr__(self, name):
        if hasattr(self.imitated, name) and callable(getattr(self.imitated, name)):
            other = getattr(self.imitated, name)

            @functools.wraps(other)
            def imitation(*args, **kwargs):
                """I'm not the real thing."""
                # print(f"args: {args}, kwargs { kwargs }")
                if self.action is not None:
                    return self.action(name, *args, **kwargs)
            
            return imitation
        else:
            return None

def command_of(cls):
    """Wrapper form of logger_command_lookup_lambda"""

    def ponder_name(func):
        return logger_command_lookup_lambda(cls, func)
    return ponder_name

def logger_command_lookup_lambda(cls, func):
    """If a class has aliased the function, swap the primary alias in for the command name"""
    @functools.wraps(func)
    def logger_command_lookup(func_name, *args, **kwargs):
        """Look up the command name, run something else."""
        return func(command_name(cls, func_name), *args, **kwargs)
    return logger_command_lookup
