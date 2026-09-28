"""Real-time log window for Magic Stack (requirement 4).

Two pieces:

* ``LogBuffer`` — a thread-safe ring-buffer ``logging.Handler`` attached to the
  root logger. Captures every record app / proxy / SSH emits so the window (and
  the file handler) see the same stream. Headless-tested by
  ``tests/test_log_buffer.py``.

* ``LogWindow`` — an ``NSWindow`` + ``NSScrollView`` + ``NSTextView`` that polls
  the buffer every ~500 ms and appends new lines. Text is selectable for copy
  (full lines, no truncation) but not editable; the view auto-scrolls to the
  newest line unless the user has scrolled up to read history.

The SSH subprocess verbose stderr is mirrored into the root logger by
``app.py``（经 ConnectionCoordinator 的 ``ssh_log_sink``）so a single
LogBuffer holds the unified stream — this module never reaches into
``proxy.py``.
"""
import logging
import threading
from collections import deque

import objc
from AppKit import (
    NSApp, NSColor, NSFont, NSMakeRect, NSScrollView, NSTextView, NSWindow,
    NSWindowStyleMaskClosable, NSWindowStyleMaskResizable, NSWindowStyleMaskTitled,
)
from Foundation import NSObject, NSRange

BUFFER_LINES = 1000        # ring buffer capacity (req 4: ~1000 lines)
POLL_INTERVAL = 0.5        # seconds between buffer → view syncs


class LogBuffer(logging.Handler):
    """Thread-safe ring buffer of formatted log records.

    Attached to the root logger; ``snapshot()`` returns a point-in-time copy of
    the buffered lines so the UI can render without holding the lock.
    """

    def __init__(self, capacity=BUFFER_LINES):
        super().__init__()
        self._lines = deque(maxlen=capacity)
        self._lock = threading.Lock()
        # Monotonic count of records ever emitted; never resets, even after the
        # ring wraps. The window renders against (lines, total) so its cursor
        # keeps advancing once len(lines) saturates at capacity — otherwise new
        # lines stop appearing after the first capacity lines (window freeze,
        # regression C-1).
        self._total = 0
        # Match the file-handler format but with a short time-only prefix so the
        # window stays narrow. Date adds noise for a live tail.
        self.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-5s %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        ))

    def emit(self, record):
        # logging.Handler.emit wraps format() in its own try/except for us, but
        # we are belt-and-suspenders: a buggy formatter must never break the
        # caller's logging path.
        try:
            line = self.format(record)
        except Exception:
            return
        with self._lock:
            self._lines.append(line)
            self._total += 1

    def snapshot(self):
        """Return ``(lines, total)`` — a point-in-time copy of the buffered
        lines (oldest → newest) plus the monotonic emission count.

        Both are read under one lock so the pair is consistent. ``total`` is the
        number of records ever emitted and never resets; ``len(lines)`` saturates
        at capacity once the ring fills, but ``total`` keeps growing. Callers
        diff on ``total`` to survive ring wrap (regression C-1).
        """
        with self._lock:
            return list(self._lines), self._total


# Strong refs to live LogWindow instances. NSWindow targets/delegates are weak;
# without this the Python wrapper is GC'd when show_log_window() returns and the
# timer + window die silently.
_active_windows = set()
_current_window = None   # singleton LogWindow; reused across clicks (W-1)


class LogWindow(NSObject):
    """Live, selectable, auto-scrolling log viewer backed by a LogBuffer."""

    def init(self):
        self = objc.super(LogWindow, self).init()
        if self is None:
            return None
        self._buffer = None
        self._rendered = 0      # monotonic total count already rendered (survives ring wrap)
        self._timer = None
        self._window = None
        self._text = None
        return self

    # ── lifecycle ──────────────────────────────────────────────────────

    def showWithBuffer_(self, buffer):
        """Build/refresh the window and keep it live. Idempotent across clicks.

        States:
        * window open + timer live → just bring to front (never stack a 2nd
          window/timer — regression W-1).
        * window built but closed  → restart the timer + resync, then re-show
          (windowWillClose_ invalidated the timer on close).
        * never built              → build UI, start timer, sync, show.
        """
        self._buffer = buffer

        if self._window is not None and self._timer is not None:
            self._window.makeKeyAndOrderFront_(None)
            NSApp.activateIgnoringOtherApps_(True)
            return

        if self._window is None:
            self.buildUI()

        self.startTimer()
        self.sync_(None)

        self._window.makeKeyAndOrderFront_(None)
        NSApp.activateIgnoringOtherApps_(True)

    def buildUI(self):
        """Create the NSWindow + scroll view + text view (once per instance)."""
        W, H = 760, 480
        # Center on screen (menu-bar app has no key window).
        from AppKit import NSScreen
        screen = NSScreen.mainScreen()
        if screen is not None:
            sf = screen.visibleFrame()
            cx = sf.origin.x + (sf.size.width - W) / 2
            cy = sf.origin.y + (sf.size.height - H) / 2
        else:
            cx, cy = 100, 100

        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(cx, cy, W, H),
            (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
             | NSWindowStyleMaskResizable),
            2, False,
        )
        from shared import i18n
        win.setTitle_(i18n.t("window.log.title"))
        win.setReleasedWhenClosed_(False)
        win.setDelegate_(self)
        self._window = win

        c = win.contentView()
        frame = c.frame()
        scroll = NSScrollView.alloc().initWithFrame_(
            NSMakeRect(0, 0, frame.size.width, frame.size.height)
        )
        scroll.setHasVerticalScroller_(True)
        scroll.setHasHorizontalScroller_(False)
        scroll.setAutohidesScrollers_(True)
        scroll.setBorderType_(0)  # no bezel; window already frames it
        scroll.setAutoresizingMask_(18)  # NSViewWidthSizable | NSViewHeightSizable

        content_size = scroll.contentSize()
        tv = NSTextView.alloc().initWithFrame_(
            NSMakeRect(0, 0, content_size.width, content_size.height)
        )
        # Canonical scroll-view resizing for NSTextView, otherwise it renders
        # but ignores wrapping / scrolling（NSTextView 经典 gotcha——须先设
        # scroll view 的 content size，否则文本不换行不滚动）。
        tv.setMinSize_((0.0, content_size.height))
        tv.setMaxSize_((1.0e7, 1.0e7))
        tv.setVerticallyResizable_(True)
        tv.setHorizontallyResizable_(False)
        tv.setAutoresizingMask_(2)  # NSViewWidthSizable
        tv.textContainer().setContainerSize_((content_size.width, 1.0e7))
        tv.textContainer().setWidthTracksTextView_(True)
        tv.setEditable_(False)          # log is read-only
        tv.setSelectable_(True)         # ...but copyable (req 4 AC: 可选中复制)
        tv.setRichText_(False)
        tv.setUsesFontPanel_(False)
        tv.setFont_(NSFont.userFixedPitchFontOfSize_(11))
        tv.setTextColor_(NSColor.textColor())
        tv.setBackgroundColor_(NSColor.textBackgroundColor())
        scroll.setDocumentView_(tv)
        c.addSubview_(scroll)
        self._text = tv

    def startTimer(self):
        """(Re)create and schedule the refresh NSTimer on the main run loop.

        NSTimer retains its target; windowWillClose_ invalidates to break the
        cycle. Defensive: invalidate any stray timer first so a reopen never
        stacks a second one.
        """
        from Foundation import NSTimer, NSRunLoop
        if self._timer is not None:
            self._timer.invalidate()
        self._timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
            POLL_INTERVAL, self, "sync:", None, True,
        )
        NSRunLoop.currentRunLoop().addTimer_forMode_(self._timer, "NSScheduledRunLoopMode")

    # ── timer callback ─────────────────────────────────────────────────

    def sync_(self, timer):
        """Append any new buffered lines to the text view and auto-scroll.

        The render cursor (``self._rendered``) tracks the buffer's monotonic
        ``total``, NOT ``len(lines)``: once the ring fills, ``len(lines)``
        saturates at capacity and a length-based cursor would never advance,
        freezing the window (regression C-1). Diffing on ``total`` keeps new
        lines flowing indefinitely.

        Auto-scroll only when the view is already at the bottom — if the user
        scrolled up to read history, don't yank them down on each tick.
        """
        if self._buffer is None or self._text is None:
            return

        lines, total = self._buffer.snapshot()
        if total <= self._rendered:
            return

        new = total - self._rendered
        if new > len(lines):
            # Reader lagged past ring capacity: the lines we still owed were
            # dropped from the ring. Reset the view to the surviving tail.
            self._text.setString_("")
            chunk = lines
        else:
            chunk = lines[-new:]
        self._rendered = total

        # NSScrollView floatValue: for a flipped NSTextView, ~1.0 == bottom.
        scroll = self._text.enclosingScrollView()
        was_at_bottom = True
        if scroll is not None:
            was_at_bottom = scroll.verticalScroller().floatValue() > 0.999

        # Append. Each formatted record has no trailing newline, so join with
        # \n and prepend a separator if the existing content doesn't end in one.
        current = str(self._text.string())
        sep = "" if (current == "" or current.endswith("\n")) else "\n"
        insertion = sep + "\n".join(chunk) + "\n"
        self._text.replaceCharactersInRange_withString_(
            NSRange(len(current), 0), insertion,
        )

        if was_at_bottom:
            end = len(str(self._text.string()))
            self._text.scrollRangeToVisible_(NSRange(end, 0))

    # ── teardown ───────────────────────────────────────────────────────

    def windowWillClose_(self, notification):
        if self._timer is not None:
            self._timer.invalidate()
            self._timer = None
        _active_windows.discard(self)


def show_log_window(buffer):
    """Public entry: show the live log window wired to ``buffer``.

    Single-instance: repeated "查看日志" clicks reuse one LogWindow instead of
    stacking N windows + N 500ms timers (regression W-1). The instance is reused
    even after the user closes it — showWithBuffer_ restarts the timer on reopen.
    """
    global _current_window
    if _current_window is not None:
        _current_window.showWithBuffer_(buffer)
        return _current_window
    w = LogWindow.alloc().init()
    _active_windows.add(w)
    _current_window = w
    w.showWithBuffer_(buffer)
    return w
