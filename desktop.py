from __future__ import annotations

import argparse
import ctypes
import os
import threading
from pathlib import Path

import webview

from app import (
    STATIC_DIR,
    acquire_singleton_mutex,
    create_router_server,
    default_data_dir,
    release_singleton_mutex,
)


APP_NAME = "SuperGrok Router"
APP_ICON = STATIC_DIR / "app-icon.ico"


def focus_existing_window() -> None:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.FindWindowW.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p)
    user32.FindWindowW.restype = ctypes.c_void_p
    handle = user32.FindWindowW(None, APP_NAME)
    if handle:
        user32.ShowWindow(handle, 9)
        user32.SetForegroundWindow(handle)


def show_error(message: str) -> None:
    ctypes.windll.user32.MessageBoxW(None, message, APP_NAME, 0x10)


class ProviderHost:
    def __init__(self, port: int, data_dir: Path, upstream: str):
        self.server, self.singleton = create_router_server("127.0.0.1", port, data_dir, upstream)
        self.thread: threading.Thread | None = None
        self.closed = False

    def start(self) -> None:
        self.server.usage.start()
        self.thread = threading.Thread(target=self.server.serve_forever, name="provider", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.thread and self.thread.is_alive():
            self.server.shutdown()
            self.thread.join(timeout=5)
        self.server.usage.stop()
        self.server.server_close()
        release_singleton_mutex(self.singleton)


class WindowsShell:
    def __init__(self, provider: ProviderHost):
        self.provider = provider
        self.window = None
        self.form = None
        self.icon = None
        self.tray = None
        self.menu = None
        self.timer = None
        self.last_budget_alert = None

    def attach(self, window) -> None:
        import clr

        clr.AddReference("System.Drawing")
        clr.AddReference("System.Windows.Forms")
        from System.Drawing import Icon
        from System.Windows.Forms import ContextMenuStrip, FormWindowState, NotifyIcon, Timer, ToolTipIcon

        self.window = window
        self.form = window.native
        self.icon = Icon(str(APP_ICON))
        self.form.Icon = self.icon

        self.menu = ContextMenuStrip()
        open_item = self.menu.Items.Add("打开")
        exit_item = self.menu.Items.Add("退出")
        open_item.Click += self.restore
        exit_item.Click += self.exit

        self.tray = NotifyIcon()
        self.tray.Icon = self.icon
        self.tray.Text = APP_NAME
        self.tray.ContextMenuStrip = self.menu
        self.tray.DoubleClick += self.restore

        def on_resize(_sender, _event):
            if self.form.WindowState == FormWindowState.Minimized:
                self.form.Hide()
                self.tray.Visible = True

        self.form.Resize += on_resize
        self._on_resize = on_resize

        self.timer = Timer()
        self.timer.Interval = 2000
        self.timer.Tick += self.check_budget
        self.timer.Start()
        self._form_window_state = FormWindowState
        self._tool_tip_icon = ToolTipIcon

    def restore(self, _sender=None, _event=None) -> None:
        if not self.form:
            return
        self.form.Show()
        self.form.WindowState = self._form_window_state.Normal
        self.form.Activate()
        self.tray.Visible = False

    def exit(self, _sender=None, _event=None) -> None:
        if self.timer:
            self.timer.Stop()
        if self.tray:
            self.tray.Visible = False
        if self.form:
            self.form.Close()

    def check_budget(self, _sender=None, _event=None) -> None:
        try:
            agent = next(item for item in self.provider.server.store.agents() if item["id"] == "codex-mcp")
            alert = agent.get("budget_alert")
            if not alert or alert.get("acknowledged") or alert.get("id") == self.last_budget_alert:
                return
            self.last_budget_alert = alert["id"]
            self.tray.Visible = True
            self.tray.ShowBalloonTip(
                5000,
                APP_NAME,
                "MCP 额度闸门已触发，请打开应用授权。",
                self._tool_tip_icon.Warning,
            )
            self.restore()
        except (KeyError, StopIteration):
            return

    def dispose(self) -> None:
        if self.timer:
            self.timer.Stop()
            self.timer.Dispose()
        if self.tray:
            self.tray.Visible = False
            self.tray.Dispose()
        if self.menu:
            self.menu.Dispose()
        if self.icon:
            self.icon.Dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--port", type=int, default=8742)
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--upstream", default=os.environ.get("SGR_UPSTREAM", "https://api.x.ai"))
    args = parser.parse_args()

    desktop_singleton = acquire_singleton_mutex("Local\\SuperGrokRouter.Desktop")
    if desktop_singleton is False:
        focus_existing_window()
        return

    provider = None
    shell = None
    try:
        provider = ProviderHost(args.port, args.data_dir, args.upstream)
        provider.start()
        window = webview.create_window(
            APP_NAME,
            f"http://127.0.0.1:{args.port}",
            width=1280,
            height=720,
            resizable=False,
            background_color="#101412",
            text_select=True,
        )
        shell = WindowsShell(provider)
        window.events.before_show += shell.attach
        storage = args.data_dir / "webview"
        webview.start(gui="edgechromium", private_mode=False, storage_path=str(storage))
    except Exception as exc:
        show_error(str(exc))
    finally:
        if shell:
            shell.dispose()
        if provider:
            provider.stop()
        release_singleton_mutex(desktop_singleton)


if __name__ == "__main__":
    main()
