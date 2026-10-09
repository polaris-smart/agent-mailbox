"""macOS menu-bar desktop shell for the workbench.

The native bundle is a browser launcher plus a local HTTP service. Advertising
it as a plain browser launcher makes the UI show up in the user's default
browser, where any relaunch stacks another window. This module renders the
workbench inside an app-owned WKWebView window and keeps a status-bar item,
Clash Verge / CC Switch style, so the UI lives in the app.

Importing this module requires pyobjc (macOS only); callers must fall back to
``webbrowser`` when it is unavailable.
"""

from __future__ import annotations

import signal
import threading
import webbrowser

import objc
from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSBackingStoreBuffered,
    NSImage,
    NSMakeRect,
    NSMenu,
    NSMenuItem,
    NSStatusBar,
    NSVariableStatusItemLength,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable,
    NSWindowStyleMaskResizable,
    NSWindowStyleMaskTitled,
)
from Foundation import NSURL, NSObject, NSURLRequest
from PyObjCTools import AppHelper
from WebKit import WKUserContentController, WKUserScript, WKWebView, WKWebViewConfiguration

WINDOW_WIDTH = 1200
WINDOW_HEIGHT = 800
SYSTEM_SYMBOL = "envelope.open.fill"


# **AM-14** ✓（Codex：不得用 `mode=packaged` 判断 ✗ —— 普通浏览器也能访问成品 App 的服务 ✓）
# ⇒ 由**原生外壳**（WKWebView）注入桌面标记 ✓ 浏览器不注入 ✓
DESKTOP_MARKER_SCRIPT = """
document.documentElement.dataset.shell = "desktop";
// Popover 能力必须**在真实 WKWebView 里探测** ✗（Codex：不能只凭系统版本假定 ✓）
document.documentElement.dataset.popoverApi =
  (typeof HTMLElement !== "undefined" && typeof HTMLElement.prototype.togglePopover === "function")
    ? "yes" : "no";
"""


def install_desktop_marker(configuration) -> None:
    """给 App 的 WKWebView 注入桌面标记 ✓（document start ⇒ 页面脚本一跑就能看到 ✓）。"""
    controller = WKUserContentController.alloc().init()
    script = WKUserScript.alloc().initWithSource_injectionTime_forMainFrameOnly_(
        DESKTOP_MARKER_SCRIPT,
        0,
        True,  # 0 = WKUserScriptInjectionTimeAtDocumentStart
    )
    controller.addUserScript_(script)
    configuration.setUserContentController_(controller)


class _DesktopDelegate(NSObject):
    """Owns the status-bar item and the single workbench window."""

    def initWithURL_(self, url):
        delegate = objc.super(_DesktopDelegate, self).init()
        if delegate is None:
            return None
        delegate._url = url
        delegate._window = None
        delegate._status_item = None
        return delegate

    # -- window management -------------------------------------------------
    def _show_window(self) -> None:
        window = self._ensure_window()
        window.makeKeyAndOrderFront_(None)
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)

    def _ensure_window(self):
        if self._window is not None:
            return self._window
        rect = NSMakeRect(0, 0, WINDOW_WIDTH, WINDOW_HEIGHT)
        style = (
            NSWindowStyleMaskTitled
            | NSWindowStyleMaskClosable
            | NSWindowStyleMaskMiniaturizable
            | NSWindowStyleMaskResizable
        )
        window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style, NSBackingStoreBuffered, False
        )
        window.setTitle_("Agent Mailbox")
        window.setReleasedWhenClosed_(False)
        window.setDelegate_(self)
        configuration = WKWebViewConfiguration.alloc().init()
        install_desktop_marker(configuration)  # AM-14：桌面标记 ✓ 只在此外壳里注入 ✓
        web_view = WKWebView.alloc().initWithFrame_configuration_(rect, configuration)
        window.setContentView_(web_view)
        window.setMinSize_(NSMakeRect(0, 0, 900, 600).size)
        request = NSURLRequest.requestWithURL_(NSURL.URLWithString_(self._url))
        web_view.loadRequest_(request)
        window.center()
        self._window = window
        return window

    # -- NSApplicationDelegate --------------------------------------------
    def applicationDidFinishLaunching_(self, _notification):
        self._install_status_item()
        self._show_window()

    def applicationShouldHandleReopen_hasVisibleWindows_(self, _sender, _has_visible_windows):
        # A second launch (Dock/Launchpad/`open -b`) focuses the existing
        # window instead of stacking browser windows.
        self._show_window()
        return True

    def applicationShouldTerminateAfterLastWindowClosed_(self, _sender):
        # Menu-bar app: closing the window keeps the service running, the
        # status-bar item reopens it (Clash Verge / CC Switch behaviour).
        return False

    def windowShouldClose_(self, _sender):
        self._window.orderOut_(None)
        return False

    # -- status bar ---------------------------------------------------------
    def _install_status_item(self) -> None:
        item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
        button = item.button()
        image = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            SYSTEM_SYMBOL, "Agent Mailbox"
        )
        if image is not None:
            button.setImage_(image)
        else:  # pragma: no cover - depends on macOS symbol availability
            button.setTitle_("AM")
        menu = NSMenu.alloc().init()
        menu.addItem_(self._menu_item("打开工作台", "showWorkbench:"))
        menu.addItem_(self._menu_item("在浏览器中打开", "openInBrowser:"))
        menu.addItem_(NSMenuItem.separatorItem())
        menu.addItem_(self._menu_item("退出 Agent Mailbox", "terminate:"))
        item.setMenu_(menu)
        self._status_item = item

    def _menu_item(self, title: str, action: str) -> NSMenuItem:
        menu_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, "")
        menu_item.setTarget_(self)
        return menu_item

    # -- menu actions -------------------------------------------------------
    def showWorkbench_(self, _sender):
        self._show_window()

    def openInBrowser_(self, _sender):
        webbrowser.open(self._url)

    def terminate_(self, _sender):
        AppHelper.stopEventLoop()


def run_desktop(url: str, *, service_stopped: threading.Event | None = None) -> None:
    """Show the workbench in an app-owned window and run the AppKit loop.

    Blocks until the user quits from the status-bar menu (or SIGTERM); the
    HTTP service itself keeps running on its own background threads.
    """
    app = NSApplication.sharedApplication()
    # Accessory policy: no Dock icon and no unfinished-launch bounce even if
    # the bundle Info.plist loses LSUIElement.
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    delegate = _DesktopDelegate.alloc().initWithURL_(url)
    app.setDelegate_(delegate)
    previous_handler = signal.signal(signal.SIGTERM, lambda *_args: AppHelper.stopEventLoop())
    if service_stopped is not None:

        def stop_when_service_ends():
            service_stopped.wait()
            AppHelper.callAfter(AppHelper.stopEventLoop)

        threading.Thread(target=stop_when_service_ends, daemon=True).start()
    try:
        app.run()
    finally:
        signal.signal(signal.SIGTERM, previous_handler)
