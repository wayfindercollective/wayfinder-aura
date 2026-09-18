"""Native macOS sleep, wake, and display-change notifications."""

from __future__ import annotations

import sys


class MacOSLifecycleObserver:
    """Retained NSWorkspace observer with plain Python callbacks."""

    def __init__(self, native_observer, centers):
        self.native_observer = native_observer
        self.centers = centers

    @classmethod
    def start(
        cls,
        *,
        on_sleep=None,
        on_wake=None,
        on_screens_changed=None,
        on_visibility_changed=None,
    ):
        if sys.platform != "darwin":
            return None
        try:
            import objc
            from AppKit import (
                NSApplicationDidChangeScreenParametersNotification,
                NSApplicationDidHideNotification,
                NSApplicationDidUnhideNotification,
                NSWorkspace,
                NSWorkspaceDidWakeNotification,
                NSWorkspaceWillSleepNotification,
            )
            from Foundation import NSObject, NSNotificationCenter

            class _Observer(NSObject):
                def initWithCallbacks_(self, callbacks):
                    self = objc.super(_Observer, self).init()
                    if self is not None:
                        self._callbacks = callbacks
                    return self

                def workspaceWillSleep_(self, _notification):
                    callback = self._callbacks.get("sleep")
                    if callback:
                        callback()

                def workspaceDidWake_(self, _notification):
                    callback = self._callbacks.get("wake")
                    if callback:
                        callback()

                def workspaceScreensChanged_(self, _notification):
                    callback = self._callbacks.get("screens")
                    if callback:
                        callback()

                def applicationVisibilityChanged_(self, _notification):
                    callback = self._callbacks.get("visibility")
                    if callback:
                        callback()

            callbacks = {
                "sleep": on_sleep,
                "wake": on_wake,
                "screens": on_screens_changed,
                "visibility": on_visibility_changed,
            }
            observer = _Observer.alloc().initWithCallbacks_(callbacks)
            workspace_center = NSWorkspace.sharedWorkspace().notificationCenter()
            app_center = NSNotificationCenter.defaultCenter()
            workspace_center.addObserver_selector_name_object_(
                observer,
                "workspaceWillSleep:",
                NSWorkspaceWillSleepNotification,
                None,
            )
            workspace_center.addObserver_selector_name_object_(
                observer,
                "workspaceDidWake:",
                NSWorkspaceDidWakeNotification,
                None,
            )
            app_center.addObserver_selector_name_object_(
                observer,
                "workspaceScreensChanged:",
                NSApplicationDidChangeScreenParametersNotification,
                None,
            )
            for notification in (
                NSApplicationDidHideNotification,
                NSApplicationDidUnhideNotification,
            ):
                app_center.addObserver_selector_name_object_(
                    observer,
                    "applicationVisibilityChanged:",
                    notification,
                    None,
                )
            return cls(observer, (workspace_center, app_center))
        except Exception as exc:
            print(f"[macOS] lifecycle observer unavailable: {exc}", flush=True)
            return None

    def close(self) -> None:
        try:
            for center in self.centers:
                center.removeObserver_(self.native_observer)
        except Exception:
            pass
        self.native_observer = None
        self.centers = ()
