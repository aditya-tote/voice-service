from scripts import check_mock_cli


def test_windows_mock_shutdown_targets_only_its_live_process_tree(monkeypatch):
    monkeypatch.setattr(check_mock_cli.sys, "platform", "win32")
    commands = []
    monkeypatch.setattr(check_mock_cli.subprocess, "run", lambda command, **kwargs: commands.append(command))
    class Process:
        pid = 12345
        def poll(self):
            return None
        def wait(self, timeout):
            assert timeout == 5
    check_mock_cli.stop(Process())
    assert commands == [["taskkill", "/PID", "12345", "/T", "/F"]]


def test_mock_shutdown_never_targets_an_already_exited_pid(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("must not target an exited/reusable PID")
    monkeypatch.setattr(check_mock_cli.subprocess, "run", forbidden)
    class Process:
        def poll(self):
            return 0
        def wait(self, timeout):
            assert timeout == 5
    check_mock_cli.stop(Process())
