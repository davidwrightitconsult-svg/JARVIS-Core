from jarvis.core.commands import CommandRouter, CommandType


def test_status_command():
    command = CommandRouter().route("STATUS")
    assert command.type == CommandType.STATUS


def test_scan_command_with_arguments():
    command = CommandRouter().route("scan politics")
    assert command.type == CommandType.SCAN
    assert command.args == ("politics",)


def test_unknown_command():
    command = CommandRouter().route("place order")
    assert command.type == CommandType.UNKNOWN
