#!/usr/bin/env python3
"""
crsf_stick_viz.py — CLI (curses/ASCII) визуализатор стиков и AUX-каналов
по CRSF-пакетам (RC_CHANNELS_PACKED, frame type 0x16).

Источники данных:
  --stdin                    читать сырые CRSF-байты из stdin (для tee-пайплайна на клиенте)
  --unix-dgram PATH          забиндить UNIX datagram-сокет (замена UDP для local IPC)
  --unix-stream PATH         подключиться клиентом к существующему UNIX stream-сокету
  --udp HOST:PORT     слушать сырые CRSF-фреймы по UDP (по умолчанию 0.0.0.0:5555)
  --serial DEV --baud BAUD   читать напрямую с UART (нужен pyserial)

Пример (tee на клиенте, параллельно с основным потребителем потока):
  cat /dev/ttyUSB0 | tee >(python3 crsf_stick_viz.py --stdin) | main_consumer
  # либо, если поток уже идёт в UDP на другой процесс:
  socat /dev/ttyUSB0,raw,b420000 - | tee >(python3 crsf_stick_viz.py --stdin) > /dev/udp/127.0.0.1/5555

  python3 crsf_stick_viz.py --udp 0.0.0.0:5555
  python3 crsf_stick_viz.py --serial /dev/ttyUSB0 --baud 420000

  # UNIX datagram — форвардер шлёт sendto() прямо в файл сокета вместо UDP:
  python3 crsf_stick_viz.py --unix-dgram /tmp/crsf.sock

  # UNIX stream — сокет уже поднят через socat с fork (несколько читателей):
  socat UNIX-LISTEN:/tmp/crsf.sock,fork,reuseaddr /dev/ttyUSB0,raw,b420000 &
  python3 crsf_stick_viz.py --unix-stream /tmp/crsf.sock

В режиме --stdin клавиатура и экран curses открываются напрямую через /dev/tty,
чтобы не конфликтовать с CRSF-потоком на fd0. Ctrl+C или 'q' — выход.
"""

import argparse
import curses
import os
import select
import socket
import sys
import time

# ---------------------------------------------------------------------------
# CRSF constants
# ---------------------------------------------------------------------------

CRSF_SYNC_BYTES = (0xC8, 0xEE, 0xEC, 0xEA)  # FC addr, TX-module, ext src variants
CRSF_FRAMETYPE_RC_CHANNELS_PACKED = 0x16
CRSF_MAX_FRAME_LEN = 64
NUM_CHANNELS = 16

# CRC8 DVB-S2 lookup table (poly 0xD5), used by CRSF
def _make_crc8_table(poly=0xD5):
    table = []
    for i in range(256):
        crc = i
        for _ in range(8):
            if crc & 0x80:
                crc = ((crc << 1) ^ poly) & 0xFF
            else:
                crc = (crc << 1) & 0xFF
        table.append(crc)
    return table

CRC8_TABLE = _make_crc8_table()

def crc8_dvb_s2(data: bytes) -> int:
    crc = 0
    for b in data:
        crc = CRC8_TABLE[crc ^ b]
    return crc


def unpack_channels(payload: bytes):
    """Распаковать 22 байта (16 каналов по 11 бит, little-endian bitstream)."""
    bits = 0
    bitcount = 0
    channels = []
    for byte in payload:
        bits |= byte << bitcount
        bitcount += 8
        while bitcount >= 11 and len(channels) < NUM_CHANNELS:
            channels.append(bits & 0x7FF)
            bits >>= 11
            bitcount -= 11
    return channels


def crsf_to_us(raw: int) -> int:
    """CRSF raw (172..1811, центр 992) -> микросекунды (988..2012, центр 1500)."""
    return int(round((raw - 992) * (1024.0 / 1639.0) + 1500))


def us_to_norm(us: int, lo=988, hi=2012):
    """Нормализация в диапазон -1.0..1.0"""
    mid = (lo + hi) / 2.0
    span = (hi - lo) / 2.0
    return max(-1.0, min(1.0, (us - mid) / span))


# ---------------------------------------------------------------------------
# CRSF frame extraction from a rolling byte buffer
# ---------------------------------------------------------------------------

class CrsfFrameParser:
    def __init__(self):
        self.buf = bytearray()

    def feed(self, data: bytes):
        self.buf.extend(data)

    def pop_frames(self):
        """Генератор: извлекает валидные (frametype, payload) фреймы из буфера."""
        while True:
            # найти байт синхронизации
            sync_pos = None
            for i, b in enumerate(self.buf):
                if b in CRSF_SYNC_BYTES:
                    sync_pos = i
                    break
            if sync_pos is None:
                self.buf.clear()
                return
            if sync_pos > 0:
                del self.buf[:sync_pos]

            if len(self.buf) < 3:
                return  # ждём ещё данных

            length = self.buf[1]  # включает type + payload + crc
            if length < 2 or length > CRSF_MAX_FRAME_LEN:
                # мусорный байт синхронизации, сдвигаемся дальше
                del self.buf[0:1]
                continue

            full_len = 2 + length  # sync + len_byte + (type+payload+crc)
            if len(self.buf) < full_len:
                return  # весь фрейм ещё не пришёл

            frame = bytes(self.buf[:full_len])
            del self.buf[:full_len]

            frametype = frame[2]
            payload_and_crc = frame[2:-1]
            crc_received = frame[-1]
            crc_calc = crc8_dvb_s2(payload_and_crc)
            if crc_calc != crc_received:
                continue  # битый фрейм, пропускаем

            payload = frame[3:-1]
            yield frametype, payload


# ---------------------------------------------------------------------------
# Data sources
# ---------------------------------------------------------------------------

class UdpSource:
    def __init__(self, host, port):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        # чуть увеличим приёмный буфер ОС — на случай кратковременных
        # задержек в обработке при потоке 250 Hz (26 байт * 250 ≈ 6.5 KB/s,
        # сам трафик копеечный, буфер нужен только как амортизатор джиттера)
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 16)
        except OSError:
            pass
        self.sock.bind((host, port))

    def read(self, timeout=0.02):
        r, _, _ = select.select([self.sock], [], [], timeout)
        if not r:
            return b""
        # ВАЖНО: recvfrom() отдаёт ровно один datagram за вызов. При 250 Hz
        # (кадр каждые 4 мс) буфер сокета легко накапливает несколько
        # фреймов, если между select()-ами прошло время дольше 4 мс
        # (перерисовка, GC-пауза и т.п.) — поэтому вычитываем ВСЁ, что уже
        # лежит в очереди, одним проходом, иначе визуализация будет
        # показывать устаревшее положение стиков.
        chunks = []
        while True:
            try:
                data, _ = self.sock.recvfrom(4096)
                chunks.append(data)
            except BlockingIOError:
                break
        return b"".join(chunks)

    def close(self):
        self.sock.close()


class UnixDgramSource:
    """UNIX datagram-сокет — сохраняет границы сообщений как UDP, но без
    накладных расходов сетевого стека (loopback всё равно проходит через
    TCP/IP-путь ядра, AF_UNIX — нет). Подходит, если форвардер просто меняет
    sendto(('127.0.0.1', 5555)) на sendto('/tmp/crsf.sock')."""

    def __init__(self, path):
        self.path = path
        # убираем протухший файл сокета от прошлого запуска (bind падает,
        # если файл уже существует)
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        self.sock.bind(path)
        try:
            os.chmod(path, 0o666)  # чтобы форвардер с другим uid мог писать
        except OSError:
            pass

    def read(self, timeout=0.02):
        r, _, _ = select.select([self.sock], [], [], timeout)
        if not r:
            return b""
        # тот же drain-цикл, что и для UDP: вычитываем все накопившиеся
        # datagram'ы за один read(), см. UdpSource
        chunks = []
        while True:
            try:
                data, _ = self.sock.recvfrom(4096)
                chunks.append(data)
            except BlockingIOError:
                break
        return b"".join(chunks)

    def close(self):
        self.sock.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass


class UnixStreamSource:
    """Клиент подключается к уже поднятому UNIX stream-сокету, например:
    socat UNIX-LISTEN:/tmp/crsf.sock,fork,reuseaddr /dev/ttyUSB0,raw,b420000

    С такой связкой к одному сокету можно подключить несколько независимых
    читателей (fork на каждое подключение) — аналог tee, но через сокет."""

    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(path)  # для локального сокета connect() мгновенный
        self.sock.setblocking(False)

    def read(self, timeout=0.02):
        r, _, _ = select.select([self.sock], [], [], timeout)
        if not r:
            return b""
        try:
            data = self.sock.recv(4096)
        except BlockingIOError:
            return b""
        if not data:
            raise EOFError("unix stream socket closed by peer")
        return data

    def close(self):
        self.sock.close()


class StdinSource:
    """Читает сырые CRSF-байты из fd0 — типичный случай, когда клиент разводит
    поток через `tee` (serial/socat -> tee -> [этот скрипт, основной потребитель])."""

    def __init__(self):
        self.fd = sys.stdin.fileno()

    def read(self, timeout=0.02):
        r, _, _ = select.select([self.fd], [], [], timeout)
        if r:
            try:
                data = os.read(self.fd, 4096)
            except OSError:
                return b""
            if not data:
                # EOF: сторона, пишущая в tee, закрылась
                raise EOFError("stdin closed (upstream tee/producer exited)")
            return data
        return b""

    def close(self):
        pass


class SerialSource:
    def __init__(self, dev, baud):
        import serial  # pyserial, локальный импорт чтобы не требовать его для UDP-режима
        self.ser = serial.Serial(dev, baudrate=baud, timeout=0)

    def read(self, timeout=0.02):
        time.sleep(timeout)
        n = self.ser.in_waiting
        if n:
            return self.ser.read(n)
        return b""

    def close(self):
        self.ser.close()


# ---------------------------------------------------------------------------
# ASCII rendering helpers
# ---------------------------------------------------------------------------

STICK_W = 21   # нечётное, чтобы был центр
STICK_H = 11

def render_stick_box(win, y0, x0, xnorm, ynorm, label):
    """Рисует рамку STICK_W x STICK_H с точкой стика внутри."""
    # рамка
    win.addstr(y0, x0, "+" + "-" * (STICK_W - 2) + "+")
    for row in range(1, STICK_H - 1):
        win.addstr(y0 + row, x0, "|" + " " * (STICK_W - 2) + "|")
    win.addstr(y0 + STICK_H - 1, x0, "+" + "-" * (STICK_W - 2) + "+")

    # центр-крест (пунктиром), чтобы видеть нейтраль
    cx = x0 + STICK_W // 2
    cy = y0 + STICK_H // 2
    for row in range(1, STICK_H - 1):
        try:
            win.addch(y0 + row, cx, ord(':'))
        except curses.error:
            pass
    for col in range(1, STICK_W - 1):
        try:
            win.addch(cy, x0 + col, ord('-'))
        except curses.error:
            pass

    # позиция стика: xnorm/ynorm в [-1..1], ось Y инвертирована (вверх = положительно)
    px = int(round((xnorm + 1) / 2 * (STICK_W - 3))) + x0 + 1
    py = int(round((1 - ynorm) / 2 * (STICK_H - 3))) + y0 + 1
    px = max(x0 + 1, min(x0 + STICK_W - 2, px))
    py = max(y0 + 1, min(y0 + STICK_H - 2, py))
    try:
        win.addch(py, px, ord('O'), curses.A_BOLD)
    except curses.error:
        pass

    win.addstr(y0 + STICK_H, x0, label.center(STICK_W))


def render_bar(win, y, x, width, norm01, label, value_str):
    """norm01 in [0,1], горизонтальный бар."""
    filled = int(round(norm01 * width))
    bar = "#" * filled + "-" * (width - filled)
    win.addstr(y, x, f"{label:>5} [{bar}] {value_str}")


def render_switch(win, y, x, raw_us, label):
    """Для AUX каналов отображаем и бар, и грубую позицию переключателя (1/2/3)."""
    n = us_to_norm(raw_us)
    if n < -0.35:
        pos = "LOW "
    elif n > 0.35:
        pos = "HIGH"
    else:
        pos = "MID "
    norm01 = (n + 1) / 2
    render_bar(win, y, x, 20, norm01, label, f"{raw_us}us {pos}")


# ---------------------------------------------------------------------------
# Main curses loop
# ---------------------------------------------------------------------------

def main(stdscr, source, args):
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.timeout(20)

    parser = CrsfFrameParser()
    channels = [992] * NUM_CHANNELS
    frames_total = 0
    last_frame_ts = 0.0
    fps_counter = 0
    fps = 0.0
    fps_ts = time.time()

    # Терминалу не нужно обновляться в такт CRSF-потоку (250 Hz) — глаз
    # не различает разницу выше ~30 Hz, а erase()+addstr() на каждой
    # итерации лишь жгут CPU. Разбор фреймов идёт на полной скорости,
    # редрисовка — троттлится отдельным таймером.
    RENDER_HZ = 30.0
    RENDER_INTERVAL = 1.0 / RENDER_HZ
    last_render = 0.0

    while True:
        # выход по 'q'
        try:
            key = stdscr.getch()
            if key in (ord('q'), ord('Q'), 27):
                break
        except curses.error:
            pass

        try:
            data = source.read(timeout=0.005)
        except EOFError:
            break

        if data:
            parser.feed(data)
            for frametype, payload in parser.pop_frames():
                if frametype == CRSF_FRAMETYPE_RC_CHANNELS_PACKED and len(payload) >= 22:
                    channels = unpack_channels(payload[:22])
                    frames_total += 1
                    fps_counter += 1
                    last_frame_ts = time.time()

        now = time.time()
        if now - fps_ts >= 1.0:
            fps = fps_counter / (now - fps_ts)
            fps_counter = 0
            fps_ts = now

        if now - last_render < RENDER_INTERVAL:
            continue  # ещё рано перерисовывать — экономим CPU
        last_render = now

        stdscr.erase()
        h, w = stdscr.getmaxyx()

        title = " CRSF Stick / AUX Visualizer "
        stdscr.addstr(0, max(0, (w - len(title)) // 2), title, curses.A_REVERSE)

        age = now - last_frame_ts if last_frame_ts else None
        status = f"frames: {frames_total}  fps: {fps:5.1f}  "
        status += "link: OK" if (age is not None and age < 0.5) else "link: NO SIGNAL"
        stdscr.addstr(1, 2, status, curses.A_BOLD if (age is not None and age < 0.5) else curses.A_DIM)

        us = [crsf_to_us(c) for c in channels]

        # Mode 2: CH1=Roll, CH2=Pitch, CH3=Throttle, CH4=Yaw (стандартная CRSF раскладка)
        roll_n = us_to_norm(us[0])
        pitch_n = us_to_norm(us[1])
        throttle_n = us_to_norm(us[2])
        yaw_n = us_to_norm(us[3])

        y0 = 3
        left_x = 4
        right_x = left_x + STICK_W + 8

        render_stick_box(stdscr, y0, left_x, yaw_n, throttle_n, "THR / YAW (L)")
        render_stick_box(stdscr, y0, right_x, roll_n, pitch_n, "ROLL / PITCH (R)")

        # числовые значения основных каналов
        info_y = y0 + STICK_H + 2
        stdscr.addstr(info_y, left_x,
                      f"CH1 Roll:{us[0]:5d}us  CH2 Pitch:{us[1]:5d}us  "
                      f"CH3 Thr:{us[2]:5d}us  CH4 Yaw:{us[3]:5d}us")

        # AUX каналы CH5..CH16
        aux_y = info_y + 2
        stdscr.addstr(aux_y, left_x, "AUX channels:", curses.A_UNDERLINE)
        for i in range(4, NUM_CHANNELS):
            row = aux_y + 1 + (i - 4)
            if row >= h - 1:
                break
            render_switch(stdscr, row, left_x, us[i], f"AUX{i-3}")

        footer = "q — выход"
        if h > 0:
            stdscr.addstr(min(h - 1, aux_y + 1 + (NUM_CHANNELS - 4) + 1), left_x, footer, curses.A_DIM)

        stdscr.refresh()


def run_with_tty(source, args):
    """Запуск curses с явным /dev/tty для ввода/вывода, оставляя fd0 (stdin)
    свободным под CRSF-байты, приходящие через tee."""
    tty = open("/dev/tty", "r+b", buffering=0)
    term_type = os.environ.get("TERM", "xterm")
    screen = curses.newterm(term_type, tty.fileno(), tty.fileno())
    curses.set_term(screen)
    stdscr = curses.initscr()
    curses.noecho()
    curses.cbreak()
    stdscr.keypad(True)
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    try:
        main(stdscr, source, args)
    finally:
        stdscr.keypad(False)
        curses.echo()
        curses.nocbreak()
        curses.endwin()
        tty.close()


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stdin", action="store_true",
                     help="читать сырые CRSF-байты из stdin (клиентский tee-пайплайн)")
    ap.add_argument("--unix-dgram", metavar="PATH", default=None,
                     help="забиндить UNIX datagram-сокет по PATH (замена UDP для local IPC)")
    ap.add_argument("--unix-stream", metavar="PATH", default=None,
                     help="подключиться клиентом к существующему UNIX stream-сокету по PATH "
                          "(например, socat UNIX-LISTEN:PATH,fork)")
    ap.add_argument("--udp", metavar="HOST:PORT", default="0.0.0.0:5555",
                     help="слушать UDP с сырыми CRSF-фреймами (по умолчанию 0.0.0.0:5555)")
    ap.add_argument("--serial", metavar="DEV", default=None,
                     help="читать напрямую с UART, например /dev/ttyUSB0 (требует pyserial)")
    ap.add_argument("--baud", type=int, default=420000, help="скорость UART (по умолчанию 420000)")
    return ap.parse_args()


def _make_source(args):
    if args.stdin:
        return StdinSource()
    if args.unix_dgram:
        return UnixDgramSource(args.unix_dgram)
    if args.unix_stream:
        try:
            return UnixStreamSource(args.unix_stream)
        except FileNotFoundError:
            sys.exit(f"error: сокет '{args.unix_stream}' не существует "
                     f"(сервер/socat ещё не запущен?)")
        except ConnectionRefusedError:
            sys.exit(f"error: '{args.unix_stream}' существует, но никто не слушает "
                     f"(stale socket file? удали и перезапусти сервер)")
    if args.serial:
        return SerialSource(args.serial, args.baud)
    host, port = args.udp.split(":")
    return UdpSource(host, int(port))


if __name__ == "__main__":
    args = parse_args()
    source = _make_source(args)

    try:
        if args.stdin:
            run_with_tty(source, args)
        else:
            curses.wrapper(main, source, args)
    except KeyboardInterrupt:
        pass
    finally:
        source.close()
