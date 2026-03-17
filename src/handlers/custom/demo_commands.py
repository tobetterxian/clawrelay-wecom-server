"""
Demo custom commands module.

This module demonstrates how to create bot-specific custom commands.
To create your own commands:
1. Create a new Python file in src/handlers/custom/
2. Define command handler classes extending CommandHandler
3. Implement the register_commands(command_router) function
4. Add the module path to your bot's custom_commands list in config/bots.yaml
"""

from src.handlers.command_handlers import CommandHandler


class EchoCommandHandler(CommandHandler):
    """Echo back the user's message."""
    command = "echo"
    description = "Echo back your message"

    def handle(self, cmd: str, stream_id: str, user_id: str) -> tuple[str, None]:
        text = cmd.replace("/echo", "", 1).strip()
        if not text:
            return "Usage: /echo <your message>", None
        return f"Echo: {text}", None


class PingCommandHandler(CommandHandler):
    """Simple ping/pong health check command."""
    command = "ping"
    description = "Check if the bot is alive"

    def handle(self, cmd: str, stream_id: str, user_id: str) -> tuple[str, None]:
        return "Pong!", None


def register_commands(command_router):
    """Register custom commands with the command router."""
    command_router.register(EchoCommandHandler())
    command_router.register(PingCommandHandler())
