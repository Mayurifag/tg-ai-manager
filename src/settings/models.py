from dataclasses import dataclass


@dataclass
class GlobalSettings:
    id: int = 1
    # autoread_only_new removed: it is now a hardcoded condition in logic
    autoread_service_messages: bool = (
        False  # Covers: pins, joins, leaves, photo changes, group settings
    )
    autoread_polls: bool = False
    autoread_bots: str = "@lolsBotCatcherBot"  # Comma separated list of usernames/IDs
    autoread_regex: str = ""
    autoread_self: bool = False
