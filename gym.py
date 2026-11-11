"""Screen-only hexagon timing helper. F1 toggles tracking; F2 stops."""
import argparse
import json
from collections import deque
from dataclasses import dataclass
from pathlib import Path
import time
import threading
import sys

import cv2
import numpy as np


def wait_until(deadline, stop):
    """Use coarse waits away from the deadline and a short precise final wait."""
    while not stop.is_set():
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            return True
        if remaining > .006:
            stop.wait(remaining - .006)
        # The last <=6ms intentionally avoids Windows sleep quantization.
    return False


class TimingResolution:
    def __enter__(self):
        self.enabled = False
        if sys.platform == 'win32':
            import ctypes
            self.winmm = ctypes.WinDLL('winmm')
            self.enabled = self.winmm.timeBeginPeriod(1) == 0
        return self

    def __exit__(self, *args):
        if self.enabled:
            self.winmm.timeEndPeriod(1)


@dataclass
class Ring:
    center: np.ndarray
    inner: float
    outer: float

    @property
    def radius(self):
        return (self.inner + self.outer) / 2


class Tracker:
    def __init__(self, latency, margin):
        self.latency = latency
        self.margin = margin
        self.reset()

    def reset(self):
        self.target = None
        self.target_time = 0
        self.samples = deque(maxlen=6)
        self.fired = False
        self.last_seen = 0
        self.status = 'WAIT TARGET'
        self.velocity = 0.
        self.predicted = None
        self.click_at = None
        self.frame_times = deque(maxlen=8)
        self.target_candidates = deque(maxlen=4)

    def update(self, rings, now, result=None, action_age=0.):
        if result:
            self.reset()
            self.status = 'RESULT ' + result.upper()
            return False, None
        self.frame_times.append(now)
        self.click_at = None
        thick = [r for r in rings if (r.outer - r.inner) / r.radius > .045]
        # The final target can be thinner than the moving outline. Identify it
        # by a stationary inner radius while a concentric outer ring shrinks.
        if self.target is None and len(rings) >= 2:
            ordered = sorted(rings, key=lambda r: r.radius)
            inner, outer = ordered[0], ordered[-1]
            self.target_candidates.append((now, inner, outer.radius))
            history = list(self.target_candidates)
            stationary = (len(history) >= 3 and history[-1][0] - history[0][0] >= .02
                          and max(h[1].radius for h in history) - min(h[1].radius for h in history) < 1.5
                          and all(np.linalg.norm(h[1].center - inner.center) < 3 for h in history))
            if (stationary and history[0][2] - history[-1][2] > 2
                    and outer.radius > inner.outer * 1.2):
                self.target = inner
                self.target_time = now
                self.samples.clear()
        elif self.target is None:
            self.target_candidates.clear()
        if self.target is not None:
            # Refresh a thin locked target without classifying it by thickness.
            stable = [r for r in rings if abs(r.inner - self.target.inner) < 1
                      and abs(r.outer - self.target.outer) < 1
                      and np.linalg.norm(r.center - self.target.center) < 3]
            if stable:
                self.target_time = now
        if thick:
            candidate = max(thick, key=lambda r: (r.outer - r.inner) / r.radius)
            same_step = (self.target is not None and now - self.target_time < .4
                         and np.linalg.norm(candidate.center - self.target.center) < 15)
            clean = (not same_step or
                     (abs(candidate.inner - self.target.inner) < 3 and
                      abs(candidate.outer - self.target.outer) < 3))
            if not same_step:
                self.samples.clear()
            if clean:
                if not same_step:
                    self.target = candidate
                self.target_time = now
        target = self.target
        if target is None or now - self.target_time > .4:
            self.samples.clear()
            self.status = 'WAIT TARGET'
            return False, None
        thin = [r for r in rings if (r.outer - r.inner) / r.radius <= .045
                and np.linalg.norm(r.center - target.center) < target.radius * .1
                and abs(r.radius - target.radius) > (target.outer - target.inner) / 2 + .75]
        if not thin:
            # At contact both white outlines merge. Bridge only a short gap
            # with a recently measured approach, rather than waiting for two rings.
            if now - self.last_seen > .12 or len(self.samples) < 3:
                if now - self.last_seen > .15:
                    self.samples.clear()
                self.status = 'WAIT MOVING'
                return False, target
        else:
            moving = min(thin, key=lambda r: np.linalg.norm(r.center - target.center))
            if moving.radius > target.outer * 1.15:
                self.fired = False
            if self.samples and (now - self.last_seen > .15 or
                                 abs(moving.radius - self.samples[-1][1]) > target.radius * .3):
                self.samples.clear()
            self.last_seen = now
            self.samples.append((now, moving.radius))
        if len(self.samples) < 3 or self.fired:
            self.status = 'WAIT RESET' if self.fired else 'MEASURING'
            return False, target
        times, sizes = np.array(self.samples).T
        times -= now
        velocity, intercept = np.polyfit(times, sizes, 1)
        predicted = intercept + velocity * (self.latency + action_age)
        self.velocity, self.predicted = velocity, predicted
        residual = float(np.max(np.abs(sizes - (velocity * times + intercept))))
        self.status = 'APPROACH' if velocity < -2 else 'WAIT SHRINK'
        # Fixed two-pixel margins previously erased a four-pixel late-round band.
        margin = min(self.margin, (target.outer - target.inner) * .2)
        safe_inner = target.inner + margin
        safe_outer = target.outer - margin
        period = (float(np.median(np.diff(self.frame_times)))
                  if len(self.frame_times) > 1 else 1 / 60)
        period = min(.025, max(.004, period))
        due = ((intercept - target.radius) / -velocity - self.latency
               if velocity < -2 else float('inf'))
        # Schedule the midpoint between captures if the next frame could skip it.
        schedule = predicted > safe_outer and 0 <= due - action_age <= period
        hit = velocity < -2 and residual <= 3 and (safe_inner <= predicted <= safe_outer or schedule)
        if hit:
            self.fired = True
            self.click_at = now + due if schedule else now + action_age
            self.status = 'SCHEDULED' if schedule else 'TRIGGER'
        return hit, target


class HexDetector:
    """Measure light coverage along six edges, without requiring closed contours."""
    def __init__(self):
        self.center = None
        self.maps = None
        self.result = None

    def __call__(self, frame):
        self.result = None
        height, width = frame.shape[:2]
        if self.center is None:
            middle = np.array([width / 2, height / 2])
            hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
            mask = cv2.inRange(hsv, (0, 0, 145), (179, 85, 255))
            contours, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
            centers = []
            for contour in contours:
                x, y, w, h = cv2.boundingRect(contour)
                center = np.array([x + (w - 1) / 2, y + (h - 1) / 2])
                if (cv2.contourArea(contour) > 5000 and .8 < w / h < .95
                        and np.linalg.norm(center - middle) < min(width, height) * .1):
                    centers.append(center)
            if not centers:
                return []
            self.center = np.median(centers, axis=0)
            angles = np.arange(7) * np.pi / 3 - np.pi / 2
            vertices = np.column_stack((np.cos(angles), np.sin(angles)))
            fractions = np.linspace(.05, .95, 40)[:, None]
            edge = np.concatenate([vertices[i] * (1 - fractions) + vertices[i + 1] * fractions
                                   for i in range(6)])
            self.radii = np.arange(40., min(width, height) * .8, .5)
            coordinates = self.center + self.radii[:, None, None] * edge[None, :, :]
            self.maps = (coordinates[:, :, 0].astype(np.float32),
                         coordinates[:, :, 1].astype(np.float32))
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        for color, lower, upper in [('green', (35, 75, 45), (90, 255, 255)),
                                     ('red', (0, 100, 65), (10, 255, 255))]:
            colored = cv2.inRange(hsv, lower, upper).astype(np.float32) / 255
            color_coverage = cv2.remap(colored, *self.maps, cv2.INTER_LINEAR).mean(axis=1)
            if np.count_nonzero(color_coverage > .65) >= 2:
                self.result = color
                return []
        mask = cv2.inRange(hsv, (0, 0, 145), (179, 85, 255)).astype(np.float32) / 255
        coverage = cv2.remap(mask, *self.maps, cv2.INTER_LINEAR).mean(axis=1)
        inside = coverage >= .65
        changes = np.diff(np.r_[False, inside, False].astype(int))
        starts, ends = np.where(changes == 1)[0], np.where(changes == -1)[0]
        return [Ring(self.center.copy(), float(self.radii[s]), float(self.radii[e - 1] + .5))
                for s, e in zip(starts, ends) if e - s >= 2]


def render_preview(frame, rings, target, status, clicks_enabled, hits, successes, misses, flash):
    view = frame.copy()
    accent = (140, 215, 110)
    muted = (155, 166, 175)
    text_color = (235, 240, 243)
    angles = np.arange(6) * np.pi / 3 - np.pi / 2
    unit = np.column_stack((np.cos(angles), np.sin(angles)))
    for ring in rings:
        is_target = target is not None and abs(ring.radius - target.radius) < 2
        color = accent if is_target else (230, 190, 90)
        vertices = np.rint(ring.center + unit * ring.radius).astype(np.int32)
        cv2.polylines(view, [vertices], True, color, 1, cv2.LINE_AA)
    width = min(560, max(480, view.shape[1]))
    view = cv2.resize(view, (width, round(view.shape[0] * width / view.shape[1])))
    header = np.full((144, width, 3), (30, 26, 23), dtype=np.uint8)
    footer = np.full((38, width, 3), (30, 26, 23), dtype=np.uint8)

    def label(canvas, message, x, y, size=.48, color=text_color):
        cv2.putText(canvas, message, (x, y), cv2.FONT_HERSHEY_SIMPLEX,
                    size, color, 1, cv2.LINE_AA)

    label(header, 'TARKOV / GYM', 18, 29, .62)
    mode = 'AUTO' if clicks_enabled else 'PREVIEW'
    label(header, mode, width - 96, 28, .46, accent if clicks_enabled else muted)
    states = {'PAUSED': 'Paused', 'WAIT TARGET': 'Waiting for minigame',
              'WAIT MOVING': 'Waiting for moving ring', 'MEASURING': 'Tracking',
              'APPROACH': 'Tracking', 'WAIT SHRINK': 'Tracking',
              'WAIT RESET': 'Waiting for next step', 'SCHEDULED': 'Timing click',
              'TRIGGER': 'Timing click', 'RESULT GREEN': 'Successful', 'RESULT RED': 'Missed'}
    state = states.get(status, status)
    if flash and not status.startswith('RESULT'):
        state = 'Click sent' if clicks_enabled else 'Click detected'
    color = ((105, 115, 245) if status == 'RESULT RED' else
             muted if status == 'PAUSED' else accent)
    cv2.circle(header, (23, 62), 4, color, -1, cv2.LINE_AA)
    label(header, state, 38, 68, .56, color)
    cv2.line(header, (18, 86), (width - 18, 86), (62, 55, 48), 1)
    for x, value, name in [(18, hits, 'CLICKS' if clicks_enabled else 'SIGNALS'),
                           (width // 3 + 6, successes, 'GREEN'),
                           (width * 2 // 3, misses, 'RED')]:
        label(header, str(value), x, 113, .65)
        label(header, name, x + 42, 111, .36, muted)
    label(header, 'F1  Start / Pause', 18, 136, .38, muted)
    label(header, 'F2  Exit', width - 92, 136, .38, muted)
    label(footer, 'Keep this window outside the capture area', 18, 24, .4, muted)
    return np.vstack((header, view, footer))


def select_region(snapshot, cancelled):
    """Resizable selection on a scaled still image; coordinates stay in screen pixels."""
    height, width = snapshot.shape[:2]
    scale = min(1., 1280 / width, 760 / height)
    preview = cv2.resize(snapshot, (round(width * scale), round(height * scale)))
    points = []
    dragging = False
    title = 'Capture area - drag mouse, Enter: save, C: cancel, F2: exit'

    def mouse_event(event, x, y, flags, param):
        nonlocal dragging
        x = max(0, min(preview.shape[1] - 1, x))
        y = max(0, min(preview.shape[0] - 1, y))
        if event == cv2.EVENT_LBUTTONDOWN:
            points[:] = [(x, y), (x, y)]
            dragging = True
        elif event == cv2.EVENT_MOUSEMOVE and dragging:
            points[1] = (x, y)
        elif event == cv2.EVENT_LBUTTONUP and dragging:
            points[1] = (x, y)
            dragging = False

    cv2.namedWindow(title, cv2.WINDOW_AUTOSIZE)
    cv2.setMouseCallback(title, mouse_event)
    try:
        while not cancelled.is_set():
            view = preview.copy()
            if len(points) == 2:
                cv2.rectangle(view, points[0], points[1], (0, 255, 0), 2)
            cv2.imshow(title, view)
            key = cv2.waitKeyEx(20)
            if key & 0xff in (ord('c'), ord('C')):
                return None
            if key & 0xff in (10, 13, 32) and len(points) == 2 and not dragging:
                x1, x2 = sorted(p[0] for p in points)
                y1, y2 = sorted(p[1] for p in points)
                if x2 > x1 and y2 > y1:
                    # Use actual resized dimensions to avoid rounding drift.
                    sx, sy = width / preview.shape[1], height / preview.shape[0]
                    left, top = round(x1 * sx), round(y1 * sy)
                    right, bottom = round((x2 + 1) * sx), round((y2 + 1) * sy)
                    return left, top, right - left, bottom - top
            if cv2.getWindowProperty(title, cv2.WND_PROP_VISIBLE) < 1:
                return None
        return None
    finally:
        cv2.destroyAllWindows()


def main():
    import mss
    from pynput import keyboard, mouse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--click', action='store_true', help='Enable real mouse clicks')
    parser.add_argument('--monitor', type=int, default=None,
                        help='Capture index from --list-monitors; default: Windows primary screen')
    parser.add_argument('--list-monitors', action='store_true', help='List capture indices and exit')
    parser.add_argument('--roi', type=int, nargs=4, metavar=('X', 'Y', 'W', 'H'))
    parser.add_argument('--configure', action='store_true',
                        help='Select a new capture area, save config and exit')
    parser.add_argument('--latency-ms', type=float, default=20)
    parser.add_argument('--margin', type=float, default=2, help='Safety margin in screen pixels')
    parser.add_argument('--no-preview', action='store_true')
    parser.add_argument('--click-hold-ms', type=float, default=30,
                        help='Duration of left-button press')
    args = parser.parse_args()
    config_path = Path(__file__).with_name('config.json')
    config = {}
    if config_path.exists():
        try:
            content = config_path.read_text(encoding='utf-8-sig').strip()
            config = json.loads(content) if content else {}
            if not isinstance(config, dict):
                raise ValueError('Expected a JSON object')
            monitor_config = config.get('monitor')
            roi_config = config.get('roi')
            if monitor_config is not None and (type(monitor_config) is not int or monitor_config < 1):
                raise ValueError('monitor must be a positive integer')
            if roi_config is not None and (not isinstance(roi_config, list) or len(roi_config) != 4
                                           or any(type(v) is not int for v in roi_config)):
                raise ValueError('roi must contain four integers: X, Y, W, H')
        except (OSError, ValueError) as exc:
            parser.error(f'Invalid config {config_path}: {exc}')
        if args.monitor is None:
            args.monitor = monitor_config
        if args.roi is None:
            args.roi = roi_config
    if args.configure:
        args.roi = None
    if args.latency_ms < 0 or args.margin < 0 or args.click_hold_ms <= 0:
        parser.error('Latency/margin must be nonnegative; click hold must be positive')
    stop, active = threading.Event(), threading.Event()
    held = set()

    def press(key):
        if key in held:
            return
        held.add(key)
        if key == keyboard.Key.f2:
            stop.set()
        elif key == keyboard.Key.f1:
            if active.is_set():
                active.clear()
            else:
                active.set()
            print('Tracking:', active.is_set(), flush=True)

    listener = keyboard.Listener(on_press=press, on_release=lambda key: held.discard(key))
    controller = mouse.Controller()
    tracker = Tracker(args.latency_ms / 1000, args.margin)
    detector = HexDetector()
    with TimingResolution(), mss.MSS() as capture:
        # Windows places the primary display at desktop coordinates (0, 0).
        # MSS enumeration order need not match Windows display numbers.
        primary = next((i for i, m in enumerate(capture.monitors[1:], 1)
                        if m['left'] == 0 and m['top'] == 0), 1)
        for i, m in enumerate(capture.monitors[1:], 1):
            label = ' [primary]' if i == primary else ''
            print(f"Monitor {i}: {m['width']}x{m['height']} "
                  f"at ({m['left']}, {m['top']}){label}")
        if args.list_monitors:
            return
        monitor_index = primary if args.monitor is None else args.monitor
        if not 1 <= monitor_index < len(capture.monitors):
            parser.error('Invalid monitor index')
        monitor = capture.monitors[monitor_index]
        print(f'Capturing monitor {monitor_index}', flush=True)
        if args.roi:
            x, y, w, h = args.roi
        else:
            ready = threading.Event()
            def setup_key(key):
                if key == keyboard.Key.f1:
                    ready.set()
                elif key == keyboard.Key.f2:
                    stop.set()
            print('Open the minigame, then press F1 to capture the screen. F2: cancel.', flush=True)
            setup_listener = keyboard.Listener(on_press=setup_key)
            setup_listener.start()
            try:
                while not ready.wait(.05):
                    if stop.is_set():
                        return
                if stop.is_set():
                    return
                snapshot = np.array(capture.grab(monitor))[:, :, :3].copy()
                print('Drag to select. Enter: save; C: cancel (selection window must have focus).', flush=True)
                selection = select_region(snapshot, stop)
            finally:
                setup_listener.stop()
                setup_listener.join(timeout=1)
            if selection is None:
                print('Selection cancelled; config unchanged.', flush=True)
                return
            x, y, w, h = selection
        if w <= 0 or h <= 0:
            return
        if x < 0 or y < 0 or x + w > monitor['width'] or y + h > monitor['height']:
            parser.error('ROI must fit within the selected monitor')
        if args.roi is None or args.configure:
            config.update(monitor=monitor_index, roi=[int(x), int(y), int(w), int(h)])
            config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + '\n',
                                   encoding='utf-8')
            print(f'Capture area saved: {config_path}', flush=True)
        if args.configure:
            return
        region = dict(left=monitor['left'] + x, top=monitor['top'] + y, width=w, height=h)
        print(f'ROI: --monitor {monitor_index} --roi {x} {y} {w} {h}')
        print('REAL CLICKS' if args.click else 'DRY RUN: no mouse input')
        print('F1: start/pause. F2: exit. Keep preview outside the capture region.', flush=True)
        listener.start()
        hits = 0
        last_hit = 0
        input_delay = .002
        successes = misses = 0
        last_result = None
        try:
            while not stop.is_set():
                started = time.perf_counter()
                frame = np.array(capture.grab(region))[:, :, :3].copy()
                rings = detector(frame)
                hit = False
                if active.is_set():
                    hit, target = tracker.update(rings, started, detector.result,
                                                 action_age=time.perf_counter() - started)
                    if hit:
                        hits += 1
                        last_hit = started
                        if args.click:
                            planned_time = tracker.click_at
                            dispatch_time = planned_time - input_delay
                            if not wait_until(dispatch_time, stop):
                                break
                            command_time = time.perf_counter()
                            controller.press(mouse.Button.left)
                            pressed_time = time.perf_counter()
                            measured_delay = pressed_time - command_time
                            input_delay = .65 * input_delay + .35 * min(.015, measured_delay)
                            stop.wait(max(0, pressed_time + args.click_hold_ms / 1000 - time.perf_counter()))
                            controller.release(mouse.Button.left)
                else:
                    tracker.reset()
                if detector.result and detector.result != last_result:
                    if detector.result == 'green':
                        successes += 1
                    else:
                        misses += 1
                last_result = detector.result
                if not args.no_preview:
                    status = tracker.status if active.is_set() else 'PAUSED'
                    display = render_preview(frame, rings, tracker.target, status,
                                             args.click, hits, successes, misses,
                                             started - last_hit < .5)
                    cv2.imshow('Tarkov Gym', display)
                    cv2.waitKey(1)
                    if cv2.getWindowProperty('Tarkov Gym', cv2.WND_PROP_VISIBLE) < 1:
                        stop.set()
                stop.wait(max(0, 1 / 120 - (time.perf_counter() - started)))
        finally:
            listener.stop()
            if args.click:
                controller.release(mouse.Button.left)
            cv2.destroyAllWindows()


if __name__ == '__main__':
    main()
