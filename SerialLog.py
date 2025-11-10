#!/usr/bin/env python3
"""
poll_serial.py - Serial poller with Tkinter live plotting (dark theme)

Features:
- CLI: -p <port> -s <speed> -t <timeout ms> -w <between-requests ms> -f <file>
  -? "request_string" overrides cells list
  -d debug, --text-only
- Polls serial, writes log per cycle (ms timestamps), GUI with Tkinter:
  - multi-line colored graphs (all on same canvas, per-channel Y scaling, |value| on plot)
  - individual Y-grid and labels near grid line (no overlaps)
  - legend single-line, text colored per channel
  - Pause / Start / Restart
  - tooltip: top-left placed near cursor so cursor within tooltip; tooltip remains until cursor leaves it
  - X axis displayed in seconds (1 decimal)
"""
import argparse
import serial
import sys
import time
import threading
import re
from collections import deque, OrderedDict
import tkinter as tk
from tkinter import Canvas, Frame, Button, font as tkFont

# ---- Configuration ----
MAX_POINTS = 2000
RAW_READ_SLEEP = 0.05
INITIAL_WAIT = 2.0
GRID_LINES = 5
LEGEND_FONT_SIZE = 20
GRID_LABEL_FONT_SIZE = 10
LEGEND_GAP = 12  # px gap between legend entries
TOOLTIP_OFFSET = 8  # desired cursor offset inside tooltip (cursor will be at (offset, offset) inside tooltip)

# ---- Helpers ----
def try_float(x):
    try:
        return float(x)
    except Exception:
        return 0.0

def parse_response(resp: str):
    """Return ordered list of tuples (addr, value) parsed from string like $10:123$20:432,CRC"""
    if not resp:
        return []
    s = resp.strip()
    if ',' in s:
        s = s[:s.rfind(',')]
    return re.findall(r'\$(\d+):([0-9A-Za-z\.\-]+)', s)

def parse_response_dict(resp: str):
    return dict(parse_response(resp))

def build_arg_parser():
    p = argparse.ArgumentParser(description="Serial poller with Tkinter plotting")
    p.add_argument('-p','--port', required=True, help='Serial port (e.g. COM3 or /dev/ttyUSB0)')
    p.add_argument('-s','--speed', type=int, default=115200, help='Baud rate (default 115200)')
    p.add_argument('-t','--timeout', type=int, default=1000, help='Poll interval ms (default 1000)')
    p.add_argument('-w','--wait', type=int, default=20, help='Delay between cell requests ms (default 20)')
    p.add_argument('-f','--file', help='Log file (optional)')
    p.add_argument('-?','--request', dest='request_str', help='Custom request string (overrides cell list)')
    p.add_argument('-d','--debug', action='store_true', help='Debug: print raw unparsed responses')
    p.add_argument('--text-only', action='store_true', help='Run without GUI (text/log only)')
    p.add_argument('cells', nargs='*', help='Cells: <Name>:<Addr> or <Addr>')
    return p

# ---- Tooltip class (canvas-based, persistent while cursor inside) ----
class CanvasTooltip:
    def __init__(self, canvas):
        self.canvas = canvas
        self.bg_id = None
        self.text_ids = []
        self.bounds = None
        self.font = tkFont.Font(family='Arial', size=10)

    def show(self, cursor_x, cursor_y, time_line, channel_lines, channel_colors, bg='#111111', pad=6):
        """
        Show tooltip so that cursor is INSIDE tooltip at position (TOOLTIP_OFFSET, TOOLTIP_OFFSET).
        cursor_x, cursor_y: canvas coords of cursor.
        time_line: single string (time), channel_lines: list of strings, channel_colors: list of colors.
        """
        self.hide()
        # assemble lines: first time (white), then channel lines colored.
        lines = []
        colors = []
        if time_line:
            lines.append(time_line)
            colors.append('white')
        for ln, col in zip(channel_lines, channel_colors):
            lines.append(ln)
            colors.append(col)
        if not lines:
            return

        # measure
        widths = [self.font.measure(s) for s in lines]
        text_w = max(widths)
        text_h = self.font.metrics('linespace')
        total_h = text_h * len(lines)

        # desired top-left so that cursor is inside at (TOOLTIP_OFFSET, TOOLTIP_OFFSET)
        x0 = int(cursor_x - TOOLTIP_OFFSET)
        y0 = int(cursor_y - TOOLTIP_OFFSET)
        x1 = x0 + text_w + pad * 2
        y1 = y0 + total_h + pad * 2

        # clamp to canvas size but ensure cursor remains inside [x0..x1] and [y0..y1]
        cw = self.canvas.winfo_width()
        ch = self.canvas.winfo_height()
        # adjust if tooltip goes beyond right/bottom
        dx = max(0, x1 - cw)
        dy = max(0, y1 - ch)
        x0 -= dx
        x1 -= dx
        y0 -= dy
        y1 -= dy
        # ensure not negative
        if x0 < 0:
            shift = -x0
            x0 += shift; x1 += shift
        if y0 < 0:
            shift = -y0
            y0 += shift; y1 += shift
        # ensure cursor inside; if shifting to clamp pushed cursor out, nudge so cursor inside
        if not (x0 <= cursor_x <= x1):
            # place x0 so cursor at offset if possible, else clamp
            x0 = max(0, min(cursor_x - TOOLTIP_OFFSET, cw - (text_w + pad*2)))
            x1 = x0 + text_w + pad * 2
        if not (y0 <= cursor_y <= y1):
            y0 = max(0, min(cursor_y - TOOLTIP_OFFSET, ch - (total_h + pad*2)))
            y1 = y0 + total_h + pad * 2

        # draw background
        try:
            self.bg_id = self.canvas.create_rectangle(x0, y0, x1, y1, fill=bg, outline='#666666')
        except Exception:
            self.bg_id = None
        # draw texts
        tx = x0 + pad
        cur_y = y0 + pad
        for s, col in zip(lines, colors):
            tid = self.canvas.create_text(tx, cur_y, anchor='nw', text=s, fill=col, font=self.font)
            self.text_ids.append(tid)
            cur_y += text_h
        # ensure text above bg
        if self.bg_id:
            for tid in self.text_ids:
                try:
                    self.canvas.tag_raise(tid, self.bg_id)
                except Exception:
                    pass
        self.bounds = (x0, y0, x1, y1)

    def hide(self):
        try:
            if self.bg_id:
                self.canvas.delete(self.bg_id)
        except Exception:
            pass
        for tid in self.text_ids:
            try:
                self.canvas.delete(tid)
            except Exception:
                pass
        self.bg_id = None
        self.text_ids = []
        self.bounds = None

    def contains(self, x, y):
        if not self.bounds:
            return False
        x0, y0, x1, y1 = self.bounds
        return (x0 <= x <= x1) and (y0 <= y <= y1)

# ---- Main poller class ----
class PollSerial:
    def __init__(self, args):
        self.args = args
        self.port = args.port
        self.baud = args.speed
        self.interval_ms = args.timeout
        self.request_wait_ms = args.wait
        self.request_str = args.request_str
        self.debug = args.debug
        self.text_only = args.text_only
        self.log_filename = args.file

        # parse channels
        self.cell_list = []
        if not self.request_str:
            for token in args.cells:
                if ':' in token:
                    name, addr = token.split(':', 1)
                    self.cell_list.append((name.strip(), addr.strip()))
                else:
                    a = token.strip()
                    if a:
                        self.cell_list.append((a, a))

        # channels: name -> {'addr','xs','ys','vals'}
        self.channels = OrderedDict()
        if not self.request_str:
            for name, addr in self.cell_list:
                self.channels[name] = {
                    'addr': addr,
                    'xs': deque(maxlen=MAX_POINTS),
                    'ys': deque(maxlen=MAX_POINTS),
                    'vals': deque(maxlen=MAX_POINTS)
                }

        # colors
        self.colors = {}
        self.color_cycle = ['#e6194b','#3cb44b','#ffe119','#4363d8','#f58231',
                            '#911eb4','#46f0f0','#f032e6','#bcf60c','#fabebe',
                            '#008080','#e6beff','#9a6324','#fffac8','#800000']
        self.next_color = 0

        # threading/time
        self.channels_lock = threading.Lock()
        self.start_time = None        # perf_counter when polling started
        self.pause_accum = 0.0        # seconds total paused
        self.pause_start = None
        self.paused = False
        self.stop_event = threading.Event()
        self.poll_thread = None
        self.ser = None

        # logging
        self.log_file = None
        if self.log_filename:
            self._open_log_file()

        # GUI
        self.root = None
        self.canvas = None
        self.tooltip = None
        self.legend_font = None
        self.grid_font = None

    def _open_log_file(self):
        try:
            existed = False
            try:
                existed = open(self.log_filename, 'r').readable()
            except Exception:
                existed = False
            self.log_file = open(self.log_filename, 'a', newline='')
            if not existed:
                if self.request_str:
                    self.log_file.write("Time_ms, Values\n")
                else:
                    header = "Time_ms" + ''.join([f", {nm}" for nm, _ in self.cell_list])
                    self.log_file.write(header + '\n')
                self.log_file.flush()
        except Exception as e:
            print(f"Cannot open log file '{self.log_filename}': {e}")
            self.log_file = None

    def assign_color(self, name):
        if name in self.colors:
            return self.colors[name]
        c = self.color_cycle[self.next_color % len(self.color_cycle)]
        self.colors[name] = c
        self.next_color += 1
        return c

    def add_channel_if_missing(self, name, addr):
        with self.channels_lock:
            if name in self.channels:
                return
            self.channels[name] = {
                'addr': addr,
                'xs': deque(maxlen=MAX_POINTS),
                'ys': deque(maxlen=MAX_POINTS),
                'vals': deque(maxlen=MAX_POINTS)
            }

    def open_serial(self):
        try:
            self.ser = serial.Serial(self.port, self.baud, timeout=0.1)
        except Exception as e:
            print(f"Error opening serial port {self.port}: {e}")
            sys.exit(1)

    def initial_raw_read(self):
        print(f"Initial wait {INITIAL_WAIT} s — printing raw incoming data...")
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < INITIAL_WAIT:
            time.sleep(RAW_READ_SLEEP)
            try:
                if self.ser.in_waiting:
                    raw = self.ser.read(self.ser.in_waiting)
                    try:
                        s = raw.decode(errors='ignore')
                    except Exception:
                        s = repr(raw)
                    print(s, end='', flush=True)
            except Exception:
                pass
        print("\n--- polling start ---\n")

    def send_request(self, s: str):
        try:
            self.ser.write(s.encode('ascii'))
            self.ser.flush()
        except Exception:
            pass

    def read_all_response(self) -> str:
        time.sleep(RAW_READ_SLEEP)
        try:
            n = self.ser.in_waiting
            if n:
                raw = self.ser.read(n)
                return raw.decode(errors='ignore')
        except Exception:
            pass
        return ''

    def log_cycle_cellmode(self, timestamp_ms, parsed_dict):
        if not self.log_file:
            return
        try:
            vals = []
            for name, addr in self.cell_list:
                v = parsed_dict.get(addr, '0')
                vals.append(v)
            row = f"{timestamp_ms}" + ''.join([f", {v}" for v in vals]) + '\n'
            self.log_file.write(row)
            self.log_file.flush()
        except Exception:
            pass

    def log_cycle_querymode(self, timestamp_ms, parsed_ordered):
        if not self.log_file:
            return
        try:
            if parsed_ordered:
                vals = [v for _, v in parsed_ordered]
                row = f"{timestamp_ms}, " + ', '.join(vals) + '\n'
            else:
                row = f"{timestamp_ms}, \n"
            self.log_file.write(row)
            self.log_file.flush()
        except Exception:
            pass

    def poll_loop(self):
        if self.start_time is None:
            self.start_time = time.perf_counter()
        interval = self.interval_ms / 1000.0
        inter_req = self.request_wait_ms / 1000.0

        while not self.stop_event.is_set():
            if self.paused:
                time.sleep(0.1)
                continue

            cycle_start = time.perf_counter()
            now = time.perf_counter()
            timestamp_ms = int((now - self.start_time - self.pause_accum) * 1000)

            if self.request_str:
                self.send_request(self.request_str + '\n')
                resp = self.read_all_response()
                parsed = parse_response(resp)
                if parsed:
                    with self.channels_lock:
                        for addr, val in parsed:
                            display_name = None
                            for nm, ch in self.channels.items():
                                if ch['addr'] == addr:
                                    display_name = nm
                                    break
                            if display_name is None:
                                display_name = addr
                                self.add_channel_if_missing(display_name, addr)
                            ch = self.channels[display_name]
                            ch['xs'].append(timestamp_ms)
                            ch['ys'].append(abs(try_float(val)))
                            ch['vals'].append(try_float(val))
                            print(f"[{timestamp_ms} ms] {display_name}: {val}")
                else:
                    if self.debug and resp and resp.strip():
                        print(f"[DEBUG] Unparsed raw: {repr(resp)}")
                self.log_cycle_querymode(timestamp_ms, parsed)

            else:
                for name, addr in self.cell_list:
                    self.send_request(f"?{addr}\n")
                    time.sleep(inter_req)
                resp = self.read_all_response()
                parsed_dict = parse_response_dict(resp)
                with self.channels_lock:
                    for name, addr in self.cell_list:
                        val = parsed_dict.get(addr, '0')
                        ch = self.channels[name]
                        ch['xs'].append(timestamp_ms)
                        ch['ys'].append(abs(try_float(val)))
                        ch['vals'].append(try_float(val))
                        print(f"[{timestamp_ms} ms] {name}: {val}")
                self.log_cycle_cellmode(timestamp_ms, parsed_dict)

            elapsed = time.perf_counter() - cycle_start
            to_sleep = interval - elapsed
            if to_sleep > 0:
                waited = 0.0
                step = min(0.1, to_sleep)
                while waited < to_sleep and not self.stop_event.is_set() and not self.paused:
                    time.sleep(step)
                    waited += step

    def start_polling_thread(self):
        if self.poll_thread and self.poll_thread.is_alive():
            return
        if self.start_time is None:
            self.start_time = time.perf_counter()
            self.pause_accum = 0.0
        self.stop_event.clear()
        self.poll_thread = threading.Thread(target=self.poll_loop, daemon=True)
        self.poll_thread.start()

    def pause_polling(self):
        if self.paused:
            return
        self.paused = True
        self.pause_start = time.perf_counter()

    def resume_polling(self):
        if not self.paused:
            # if thread not running, start it
            if not (self.poll_thread and self.poll_thread.is_alive()):
                self.start_polling_thread()
            return
        dur = time.perf_counter() - (self.pause_start or time.perf_counter())
        self.pause_accum += dur
        self.pause_start = None
        self.paused = False

    def restart_polling(self):
        # stop current thread properly
        if self.poll_thread and self.poll_thread.is_alive():
            self.stop_event.set()
            self.poll_thread.join(timeout=2.0)
        self.stop_event.clear()

        # clear data
        with self.channels_lock:
            for ch in self.channels.values():
                ch['xs'].clear(); ch['ys'].clear(); ch['vals'].clear()

        # reset timers
        self.start_time = time.perf_counter()
        self.pause_accum = 0.0
        self.pause_start = None
        self.paused = False

        # start new thread
        self.poll_thread = threading.Thread(target=self.poll_loop, daemon=True)
        self.poll_thread.start()

    # ---- GUI ----
    def start_gui(self):
        self.root = tk.Tk()
        self.root.title("Serial Poller Live Plot")
        self.root.geometry("800x600")

        btn_frame = Frame(self.root)
        btn_frame.pack(side='top', fill='x')
        Button(btn_frame, text="Start", command=self.resume_polling).pack(side='left')
        Button(btn_frame, text="Pause", command=self.pause_polling).pack(side='left')
        Button(btn_frame, text="Restart", command=self.restart_polling).pack(side='left')

        self.canvas = Canvas(self.root, bg='black')
        self.canvas.pack(fill='both', expand=True)

        self.legend_font = tkFont.Font(family='Arial', size=LEGEND_FONT_SIZE)
        self.grid_font = tkFont.Font(family='Arial', size=GRID_LABEL_FONT_SIZE)

        self.tooltip = CanvasTooltip(self.canvas)

        self.root.protocol("WM_DELETE_WINDOW", self.stop)
        # bind mouse on canvas for tooltip control
        self.canvas.bind("<Motion>", self.on_mouse_move)
        self.canvas.bind("<Leave>", lambda e: self.tooltip.hide())
        self.root.after(100, self.update_canvas)
        self.root.mainloop()

    def update_canvas(self):
        try:
            self.canvas.delete('all')
        except Exception:
            pass

        w = self.canvas.winfo_width()
        h = self.canvas.winfo_height()
        if w < 10 or h < 10:
            self.root.after(100, self.update_canvas)
            return

        left, top, right, bottom = 70, 20, w - 20, h - 70
        self.canvas.create_rectangle(left, top, right, bottom, outline='white')

        with self.channels_lock:
            all_x = []
            for ch in self.channels.values():
                all_x.extend(ch['xs'])
            if not all_x:
                # draw empty x label placeholder
                self.canvas.create_text(left - 60, bottom + 18, anchor='nw', text='', fill='white', font=self.grid_font)
                self.root.after(100, self.update_canvas)
                return
            x_min = min(all_x)
            x_max = max(all_x)
            if x_min == x_max:
                x_max = x_min + 1

            placed_label_rects = []

            # draw each channel
            for name, ch in self.channels.items():
                if not ch['xs']:
                    continue
                ys_abs = [abs(v) for v in ch['ys']] if ch['ys'] else [0.0]
                y_min = 0.0
                y_max = max(ys_abs) if ys_abs else 1.0
                if y_max == 0:
                    y_max = 1.0

                # compute points
                pts = []
                for xv, real_v in zip(ch['xs'], ch['vals']):
                    yv = abs(real_v)
                    x = int(left + (xv - x_min) / (x_max - x_min) * (right - left))
                    y = int(bottom - (yv - y_min) / (y_max - y_min) * (bottom - top))
                    pts.append((x, y))

                color = self.assign_color(name)
                for i in range(1, len(pts)):
                    x0, y0 = pts[i-1]; x1, y1 = pts[i]
                    self.canvas.create_line(x0, y0, x1, y1, fill=color, width=2)

                # per-channel grid & labels; topmost moved down by half-step
                step = (y_max - y_min) / GRID_LINES
                for i_line in range(GRID_LINES + 1):
                    y_val = y_min + i_line * (y_max - y_min) / GRID_LINES
                    if i_line == GRID_LINES:
                        y_val = max(y_min, y_val - (step / 2.0))
                    y_canvas = int(bottom - (y_val - y_min) / (y_max - y_min) * (bottom - top))
                    self.canvas.create_line(left, y_canvas, right, y_canvas, fill="#444444", dash=(4,2))

                    txt = f"{y_val:.2f}"
                    text_h = self.grid_font.metrics('linespace') + 2
                    desired_top = y_canvas - text_h // 2
                    top_candidate = desired_top
                    bottom_candidate = top_candidate + text_h
                    attempt = 0
                    while attempt < 50:
                        overlap = False
                        for (t, b) in placed_label_rects:
                            if not (bottom_candidate < t - 1 or top_candidate > b + 1):
                                overlap = True
                                break
                        if not overlap:
                            break
                        k = (attempt // 2) + 1
                        if attempt % 2 == 0:
                            top_candidate = desired_top - k * (text_h + 2)
                        else:
                            top_candidate = desired_top + k * (text_h + 2)
                        bottom_candidate = top_candidate + text_h
                        attempt += 1
                    if top_candidate < top:
                        top_candidate = top; bottom_candidate = top_candidate + text_h
                    if bottom_candidate > bottom:
                        bottom_candidate = bottom; top_candidate = bottom_candidate - text_h
                    placed_label_rects.append((top_candidate, bottom_candidate))
                    self.canvas.create_text(left - 6, top_candidate + text_h // 2, anchor='e', text=txt, fill=color, font=self.grid_font)

            # X axis labels in seconds (1 decimal). left label moved further left.
            x_min_s = x_min / 1000.0
            x_max_s = x_max / 1000.0
            self.canvas.create_text(left - 60, bottom + 18, anchor='nw', text=f"{x_min_s:.1f} s", fill='white', font=self.grid_font)
            self.canvas.create_text(right + 0, bottom + 18, anchor='ne', text=f"{x_max_s:.1f} s", fill='white', font=self.grid_font)

            # legend as flowing colored texts
            legend_x = left
            legend_y = bottom + 6
            for name, ch in self.channels.items():
                color = self.assign_color(name)
                last_val = ch['vals'][-1] if ch['vals'] else 0
                text = f"{name}: {last_val}"
                self.canvas.create_text(legend_x, legend_y, anchor='nw', text=text, fill=color, font=self.legend_font)
                text_w = self.legend_font.measure(text)
                legend_x += text_w + LEGEND_GAP

        self.root.after(100, self.update_canvas)

    def on_mouse_move(self, event):
        # persistent tooltip logic
        if not self.canvas:
            return
        x = event.x; y = event.y
        w = self.canvas.winfo_width(); h = self.canvas.winfo_height()
        left, top, right, bottom = 70, 20, w - 20, h - 70

        # outside plotting area -> hide tooltip
        if x < left or x > right or y < top or y > bottom:
            self.tooltip.hide()
            return

        # if tooltip visible and cursor still inside -> keep it
        if self.tooltip.bounds and self.tooltip.contains(x, y):
            return

        # if tooltip visible but cursor left its bounds -> hide it and continue processing to maybe show new tooltip
        if self.tooltip.bounds and not self.tooltip.contains(x, y):
            self.tooltip.hide()

        # build tooltip content: one time line + channel lines
        channel_lines = []
        channel_colors = []
        global_xs = []
        with self.channels_lock:
            for ch in self.channels.values():
                global_xs.extend(ch['xs'])
            if not global_xs:
                return
            gx_min = min(global_xs); gx_max = max(global_xs)
            if gx_min == gx_max:
                gx_max = gx_min + 1
            # compute global x under cursor
            x_val_global = gx_min + (x - left) / (right - left) * (gx_max - gx_min)
            time_s = x_val_global / 1000.0
            time_line = f"Time: {time_s:.1f} s"

            for name, ch in self.channels.items():
                if not ch['xs']:
                    continue
                x_min = min(ch['xs']); x_max = max(ch['xs'])
                if x_min == x_max:
                    idx = 0
                else:
                    x_val = x_min + (x - left) / (right - left) * (x_max - x_min)
                    idx = min(range(len(ch['xs'])), key=lambda i: abs(ch['xs'][i] - x_val))
                v = ch['vals'][idx]
                channel_lines.append(f"{name}: {v:.3g}")
                channel_colors.append(self.assign_color(name))

        if channel_lines:
            # tooltip top-left set so that cursor is inside at offset (TOOLTIP_OFFSET, TOOLTIP_OFFSET)
            self.tooltip.show(x, y, time_line, channel_lines, channel_colors, bg='#111111', pad=6)

    def start(self):
        self.open_serial()
        self.initial_raw_read()
        self.start_polling_thread()
        if self.text_only:
            try:
                while self.poll_thread.is_alive():
                    time.sleep(0.1)
            except KeyboardInterrupt:
                self.stop()
        else:
            self.start_gui()

    def stop(self):
        self.stop_event.set()
        self.paused = False
        if self.poll_thread and self.poll_thread.is_alive():
            self.poll_thread.join(timeout=1.0)
        try:
            if self.ser and self.ser.is_open:
                self.ser.close()
        except Exception:
            pass
        try:
            if self.log_file:
                self.log_file.close()
        except Exception:
            pass
        if self.root:
            try:
                self.root.quit()
                self.root.destroy()
            except Exception:
                pass
        return

# ---- main ----
def main():
    parser = build_arg_parser()
    args = parser.parse_args()
    ps = PollSerial(args)
    try:
        ps.start()
    except KeyboardInterrupt:
        ps.stop()
    except Exception as e:
        print("Fatal error:", e)
        ps.stop()

if __name__ == "__main__":
    main()

