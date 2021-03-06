#!/usr/bin/env python
"""
pypicolcd, a module for driverless writing to picoLCD
Copyright (C) 2018  Jake Gustafson

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""


import pypicolcd
from pypicolcd import PicoLCD
# from pypicolcd import find_resource
from pypicolcd import get_font_meta
from pypicolcd import to_bool

import sys
import signal
import logging
import json
import asyncore
import socket
import time
from threading import Timer, Thread, Event
try:
    from urllib.parse import unquote
    from urllib.parse import quote
except ImportError:
    from urlparse import unquote
    from urllib import quote
from datetime import datetime
try:
    import systemd.daemon
except ImportError:
    # NOTE: ModuleNotFoundError is only available in Python 3.
    pass

LCD_PORT = 25664
TIME_FMT = "%Y-%m-%d %H:%M"  # :%S
bool_options = ["verbose", "clock"]
allowed_commands = ["clear", "flash", "push", "help", "refresh"]


def customDie(msg, exit_code=1, logger=None):
    print("")
    print("")
    print("ERROR:")
    print(msg)
    print("")
    print("")
    exit(exit_code)


class LCDRequestHandler(asyncore.dispatcher_with_send):
    def __init__(self, sock, service):
        asyncore.dispatcher_with_send.__init__(self, sock)
        self.service = service

    # See https://docs.python.org/2/library/asyncore.html
    def handle_read(self):
        req_bytes = self.recv(pypicolcd.JSON_MAX)
        if req_bytes is not None:
            req_s = req_bytes.decode()
            if len(req_s) > 0:
                if req_s.startswith("GET /"):
                    self.reroute_get(req_s)
                else:
                    print("* the request type is not implemented:"
                          " '{}'".format(req_s))
            else:
                pass
                # TODO: I'm not sure why this happens, but it happens
                #   whenever the connection closes.
                # print("* the client provided a blank get request.")
        else:
            print("* the client provided a NULL get request.")

    def reroute_get(self, req_s):
        if req_s.startswith("GET /"):
            words = req_s.split(" ")
            # print("* got request parts: {}".format(words))
            url_path = words[1]
            # print("* url_path:'{}'".format(url_path))
            mark_i = url_path.find("?")
            params_s = ""
            if mark_i >= 0:
                params_s = url_path[mark_i+1:]
                url_path = url_path[:mark_i]
            # else:
            #     print("* there are no params in the path.")
            # if len(params_s) < 1:
            #     print("* WARNING: url_path's ? is at"
            #           " {}".format(mark_i))
            #     print("* WARNING: and {} is at 2".format(url_path[2]))
            # print("* as '{}'".format(params_s))
            chunks = params_s.split("&")
            params = {}
            for chunk in chunks:
                parts = chunk.split("=")
                name = None
                value = None
                # print("  * contains '{}'".format(chunk))
                if len(parts) >= 1:
                    name = parts[0]
                if len(parts) >= 2:
                    value = unquote(parts[1])
                if len(parts) > 2:
                    print("  * ERROR: malformed URL param:"
                          " '{}'".format(chunk))
                # print("    * as {}:'{}'".format(name, value))
                if name == 'json':
                    try:
                        req = json.loads(value)
                        res = self.service.push_action(req)
                        # print("* The server got a JSON object:"
                        #       " {}".format(req))
                        res_bytes = json.dumps(res).encode()
                        self.send(res_bytes)
                    except json.decoder.JSONDecodeError:
                        msg = ("The client provided invalid json:"
                               " json='{}'".format(value))
                        print("  * ERROR: " + msg)
                        res = {"error": msg}
                        res_bytes = json.dumps(res).encode()
                        self.send(res_bytes)
                    except pypicolcd.DisconnectedError:
                        msg = ("No known picoLCD USB device is"
                               " connected to the server.")
                        # print("* ERROR: {}".format(msg))
                        # NOTE: push_action usually shows the error
                        res = {"error": msg}
                        res_bytes = json.dumps(res).encode()
                        self.send(res_bytes)
                    except ValueError as e:
                        msg = str(e)
                        res = {"error": msg}
                        res_bytes = json.dumps(res).encode()
                        self.send(res_bytes)
                else:
                    params[name] = value
            # try:
            #     req = json.loads(req_s)
            #     res = self.service.push_action(req)
            #     res_bytes = json.dumps(res).encode()
            #     self.send(res_bytes)
            # except json.decoder.JSONDecodeError:
            #     print("* the client provided invalid json:"
            #           " '{}'".format(req_s))
        else:
            raise ValueError("reroute_get can only handle strings"
                             " starting with 'GET /'")


# See https://docs.python.org/2/library/asyncore.html
class LCDServer(asyncore.dispatcher):
    def __init__(self, host, port, service):
        asyncore.dispatcher.__init__(self)
        self.create_socket(socket.AF_INET, socket.SOCK_STREAM)
        self.set_reuse_addr()
        self.bind((host, port))
        self.listen(5)
        print("* lcd-fb is listening on {}:{}".format(host, port))
        self.service = service
        try:
            systemd.daemon.notify('READY=1')
        except NameError:
            # The systemd module did not import.
            pass

    def blab(self, msg):
        if self.service is not None:
            if self.service.p is not None:
                self.service.p.blab(msg)

    def handle_accept(self):
        pair = self.accept()
        if pair is not None:
            now = datetime.now()
            now_s = now.strftime("%Y-%m-%d %H:%M:%S")
            sock, addr = pair
            self.blab("{}: Incoming connection from"
                      " {}".format(now_s, repr(addr)))
            handler = LCDRequestHandler(sock, self.service)


# See <https://stackoverflow.com/questions/12435211/
# python-threading-timer-repeat-function-every-n-seconds>
class ClockThread(Thread):
    def __init__(self, stopEvent, lcd_framebuffer_server):
        Thread.__init__(self)
        self.stopEvent = stopEvent
        self.lfbs = lcd_framebuffer_server

    def run(self):
        print("* The ClockThread started.")
        try:
            while not self.stopEvent.wait(0.5):
                self.lfbs.update_clock()
                if self.lfbs.keepAliveThread is None:
                    if not self.lfbs.noKeepAlive.is_set():
                        # ^ Purposely check the flag for the OTHER
                        # thread, since the following restarts that one:
                        print("* The ClockThread"
                              " is restarting KeepAlive.")
                        self.lfbs._run_keep_alive()
        except Exception as e:
            print("* ClockThread error: {}".format(e))
        finally:
            print("* ClockThread ended.")
            self.lfbs.clockThread = None


class KeepAliveThread(Thread):
    def __init__(self, stopEvent, lcd_framebuffer_server):
        Thread.__init__(self)
        self.stopEvent = stopEvent
        self.lfbs = lcd_framebuffer_server
        self.prev_ready = self.lfbs.p.ready()
        self.prev_msg = None

    def run(self):
        print("* The KeepAliveThread started.")
        try:
            while not self.stopEvent.wait(2.0):
                if self.lfbs.clockThread is None:
                    if not self.lfbs.stopFlag.is_set():
                        # ^ Purposely check the flag for the OTHER
                        # thread, since the following restarts that one:
                        self.prev_ready = False  # why: error ends clock
                        print("* KeepAlive is restarting"
                              " the draw timer...")
                        self.lfbs._run_clock()
                if not self.prev_ready:
                    try:
                        msg = None
                        if not self.lfbs.p.ready():
                            msg = ("* KeepAlive is reconnecting"
                                   " the LCD...")
                            if msg != self.prev_msg:
                                print(msg)
                                self.prev_msg = msg
                            else:
                                msg = None
                            self.lfbs.p.reconnect(silent=True)
                        if self.lfbs.p.ready():
                            if msg is None:
                                print(self.prev_msg)  # say reconnecting
                            self.prev_msg = None
                            print("  * OK. Refreshing...")
                            self.lfbs.p.refresh(enable_reconnect=False)
                            self.prev_ready = True
                    except pypicolcd.DisconnectedError:
                        print("  * FAILED")
                        pass
        except Exception as e:
            print("* KeepAlive error: {}".format(e))
            print(sys.exc_info()[0])
        finally:
            print("* KeepAlive ended.")
            self.lfbs.keepAliveThread = None


def get_bool_options():
    return bool_options


def get_commands():
    return allowed_commands


config_help = {
    "verbose": "Write everything to the server console.",
    "clock": ("Turn the clock on or off (pass x"
              " and/or y along with the clock"
              " option to change its position)."),
    "background": ("Specify the path to an image"
                   " to draw before the text."),
    "foreground": ("Specify the path to an image"
                   " to draw after the text."),
    "backlight": "Set the LCD backlight level (0 to 255).",
    "lines": ("Provide a list of lines that"
              " should display on the screen,"
              " where the next line should wrap"
              " to line under the first."),
    "font": ("Provide the name of a built-in"
             " font (case-insensitive): "
             + " ".join(PicoLCD.get_font_names())),
    "x": "Set the x location for this command.",
    "y": "Set the y location for this command.",
    "clear": "Clear the entire display.",
    "flash": ("Flash the display off to get the"
              " viewer's attention."),
    "push": ("Push text from left to right, then"
             " scroll the display when more text"
             " is written after reaching the"
             " end."),
    "help": "Show a list of options.",
    "refresh": ("Draw the buffer (such as in"
                " case the device disconnected"
                " without the framebuffer"
                " knowing nor invalidating based"
                " on that knowledge).")
}


class LCDFramebufferServer(asyncore.dispatcher_with_send):


    def __init__(self, logger=None):
        self.clockThread = None
        self.keepAliveThread = None
        self.stopFlag = Event()
        self.noKeepAlive = Event()
        self.p = PicoLCD()
        if logger is None:
            logging.getLogger('lcd-fb')
        else:
            self.logger = logger
        self.time_pos = [159, 0]
        self.prev_clock_dt = datetime.now()
        self.prev_clock_s = None
        self.enable_clock = False
        self.prev_enable_clock = self.enable_clock
        # x=159 leaves just enough room for "____-%m-%d %H:%M:%S"
        self.config_help = {}
        self.allowed_names = ["background", "foreground", "backlight",
                              "lines", "font", "x", "y"]
        self._run_clock()
        self._run_keep_alive()

    def _run_clock(self):
        if self.clockThread is not None:
            raise RuntimeError("There is already one clock thread.")
        self.clockThread = ClockThread(self.stopFlag, self)
        self.clockThread.start()

    def _run_keep_alive(self):
        if self.keepAliveThread is not None:
            raise RuntimeError("There is already one KeepAlive thread.")
        self.keepAliveThread = KeepAliveThread(self.noKeepAlive, self)
        self.keepAliveThread.start()

    @staticmethod
    def get_usage():
        s = ""
        s += "==================== Usage ===================="
        s += ("\nParams should be followed by an equal sign"
              " except for booleans and commands, which will be set to"
              " true automatically.")
        s += "\nParams:"
        for k, v in config_help.items():
            s += "\n--" + k
            s += "\n  " + v
        # s += "\n"
        # s += "\nExample:"
        return s

    def update_clock(self):
        if self.enable_clock:
            if self.clockThread is None:
                self._run_clock()
            now = datetime.now()
            now_s = now.strftime(TIME_FMT)
            reconnected = (self.prev_clock_dt < self.p.invalidate_dt)
            if (now_s != self.prev_clock_s) or reconnected:
                if self.p.ready():
                    self.prev_enable_clock = self.enable_clock
                    result = self.p.draw_text_at(
                        self.time_pos,
                        now_s,
                        erase_behind_enable=True
                    )
                    # print("* drawing clock finished:"
                    #       " {}".format(result))
                    # TODO: result should be rect (?), not None.
                    self.prev_clock_dt = now
                    self.prev_clock_s = now_s
        elif self.prev_enable_clock:
            if self.p.ready():
                # result = self.p.draw_text_at(self.time_pos, now_s,
                #                              erase_behind_enable=True)
                # TODO: use the rect from draw_text_at
                self.p.draw_rect((self.time_pos, (256, 8)), False,
                                 filled=True)
                self.prev_enable_clock = False

    def push_action(self, action):
        """
        Process an action dictionary, such as URL params or command line
        params, in either case reduced to names and values.
        """
        prev_verbose = self.p.verbose_enable
        res = {}
        lines = action.get("lines")
        for name, value in action.items():
            if name == "lines":
                pass
            elif name in bool_options:
                action[name] = to_bool(value)
            elif name in self.allowed_names:
                action[name] = value
            elif name in allowed_commands:
                action[name] = True
            else:
                self.p.verbose_enable = prev_verbose
                raise ValueError("{} is an unknown option (value"
                                 " '{}').".format(name, value))
        if action.get("clear") is True:
            self.p.clear()
        if action.get("refresh") is True:
            self.p.invalidate()
            self.p.refresh()
        verbose = action.get("verbose")
        if verbose is not None:
            self.p.verbose_enable = verbose
        font = action.get("font")
        if font is not None:
            meta = get_font_meta(font)
            if meta is None:
                raise ValueError("The font is not known. Try (case"
                                 " doesn't matter):"
                                 " {}".format(font_meta.keys()))


        backlight = action.get("backlight")
        if backlight is not None:
            b = int(backlight)
            # print("* setting backlight to {}...".format(b))
            self.p.set_backlight(b)

        image_path = action.get("foreground")
        x = action.get("x")
        y = action.get("y")
        clock = action.get("clock")
        if clock is not None:
            clock = to_bool(clock)
            self.enable_clock = clock
            if x is not None:
                self.time_pos[0] = x
            if y is not None:
                self.time_pos[1] = y
        if image_path is not None:
            self.show_image(image_path)
        if action.get("push") is True:
            if (x is not None) or (y is not None):
                raise ValueError("x and y cannot be set along with the"
                                 " push option, since push uses control"
                                 " characters and scrolls"
                                 " automatically")
            if font is not None:
                raise ValueError("Custom fonts do not work with push,"
                                 " since it requires a fixed line"
                                 " height (that is an even divisor of"
                                 " the device height; only 8 is"
                                 " implemented).")
            try:
                if lines is not None:
                    all_text = " ".join(lines)
                    print("* pushing {}...".format(all_text))
                    self.p.push_text(all_text)
                    # for line in lines:
                    #     self.p.push_text(line)
            except pypicolcd.DisconnectedError as e:
                print("  * {}".format(e))
        else:
            self.show_lines(lines, font=font, x=x, y=y)

        image_path = action.get("foreground")
        if image_path is not None:
            self.show_image(image_path)

        if action.get("flash") is True:
            self.p.flash()
            self.p.flash()

        res["status"] = "OK"
        if action.get("help") is True:
            res["info"] = self.get_usage()
        self.p.verbose_enable = prev_verbose
        return res

    def show_lines(self, lines, font=None, x=0, y=0):
        shown_count = 0
        # if not self.p.ready():
        if self.p.dc is None:
            error = self.p.error
            if error is None:
                raise RuntimeError("ERROR: could not load device for"
                                   " an unknown reason.")
            else:
                # error already shown by self.p
                return False

        if lines is None:
            lines = []
        if (x is None):
            x = 0
        else:
            x = int(x)
        if (y is None):
            y = 0
        else:
            y = int(y)
        # font = "Press Start"
        if font is None:
            font = self.p.default_font
        meta = get_font_meta(font)
        if meta is None:
            raise RuntimeError("ERROR: '{}' was not"
                               " found.".format(font))
        _LINES_MAX = self.p.get_height() // (meta["default_size"] + 1)
        for line in lines:
            row = y // 8
            if row < _LINES_MAX:
                # p_dfs = self.p.default_font_size
                # self.p.draw_text(
                #     y,
                #     x,
                #     "Default font is " + str(p_dfs) + "pt ninepin"
                # )
                if line is None:
                    raise ValueError("line is None")
                print("* showing '{}'...".format(line))
                self.p.draw_text_at((x, y), line, font=font,
                                    erase_behind_enable=True)
                shown_count += 1
                y += 8
            else:
                raise ValueError("* Only {} line(s) fit(s) on the LCD,"
                                 " so '{}' will not"
                                 " appear.".format(_LINES_MAX, line))
        # print("* show_lines is complete. The LCD should have"
        #       " {} lines.".format(shown_count))

    def show_image(self, path):
        if not os.path.isfile(image_path):
            raise ValueError("{} does not exist.".format(image_path))
        self.p.draw_image((x, y), image_path, brightness=1)

    def handle_signal(self, signum, frame):
        # Any signal should terminate it, since the handler is only
        # set for the signals you want (see signal.signal below, which
        # sets the handler).
        msg = "lcd-fb will close due to signal {} ".format(signum)
        # msg = "lcd-fb got signal {}.".format(signum)
        # exit_signals = [signal.SIGINT, signal.SIGQUIT]
        self.p.draw_text(1, 1, msg)
        now = datetime.now()
        now_s = now.strftime("%Y-%m-%d %H:%M:%S")
        self.p.draw_text(2, 1, "@" + now_s)
        # if signum in exit_signals:
        # logging.info('* closing...')
        print("* " + msg)
        print("* setting thread stop flags in "
              "lcdframebuffer:LCDFramebufferServer:handle_signal...")
        self.stopFlag.set()
        self.noKeepAlive.set()
        self.close()
        time.sleep(1)
        print("* trying to end (join) clock thread manually...")
        self.clockThread.join()
        print("* trying to end (join) keepalive thread manually...")
        self.keepAliveThread.join()
        time.sleep(1)
        print("* trying exit...")
        exit(0)


def main():
    logger = logging.getLogger('lcd-fb')
    action = {}
    lines = []
    for i in range(1, len(sys.argv)):
        arg = sys.argv[i]
        if arg.startswith("--") and not arg.startswith("---"):
            if (len(arg) == 2):
                customDie("There was a blank argument", logger=logger)
            arg_parts = arg[2:].split("=")
            name = arg_parts[0]
            value = None
            if len(arg_parts) > 1:
                value = arg_parts[1]
                action[name] = value
                if len(value) == 0:
                    customDie(
                        "There was a blank value: " + arg,
                        logger=logger
                    )
            else:
                action[name] = True
                value = True

            if len(arg_parts) > 2:
                customDie(
                    "There was more than one '=' in {}".format(arg),
                    logger=logger
                )
        else:
            lines.append(arg)
    if len(lines) > 0:
        action["lines"] = lines
    lfbs = LCDFramebufferServer(logger=logger)

    # See <https://raspberrypi.stackexchange.com/questions/77738/
    # how-to-exit-a-python-daemon-cleanly>
    signal.signal(signal.SIGTERM, lfbs.handle_signal)

    host = action.get("localhost")
    if host is None:
        host = 'localhost'
    else:
        del action["localhost"]
        print("* running as host '{}'".format(host))
    lfbs.push_action(action)

    # See https://docs.python.org/2/library/asyncore.html
    server = None
    try:
        server = LCDServer(host, LCD_PORT, lfbs)
        try:
            asyncore.loop()
        except Exception as e:
            print("* asyncore.loop failed in lcdframebuffer:main:"
                  " {}".format(e))
        finally:
            print("* setting stop flag in lcdframebuffer:main...")
            lfbs.stopFlag.set()
            print("* setting noKeepAlive flag in lcdframebuffer:main...")
            lfbs.noKeepAlive.set()
    except OSError as e:
        print("Binding the LCDFramebufferServer to {}:{} failed."
              "".format(host, LCD_PORT))
        print(str(e))
    # Ignore code below, and use the asynccore subclass above instead.
    # See [Nischaya Sharma's Nov 29, 2018 answer edited Feb 16, 2019 by
    # Mohammad Mahjoub](https://stackoverflow.com/a/53536336)
    # on
    # <https://stackoverflow.com/questions/21233340/
    # sending-string-via-socket-python>
    # s = socket.socket()
    # try:
    #     s.bind(('', LCD_PORT))
    # except OSError as e:
    #     print(e)
    #     print("Try:")
    #     print("sudo fuser -k {}/tcp".format(LCD_PORT))
    #     exit(1)
    # s.listen(5)
    # c, addr = s.accept()
    # print("Socket Up and running with a connection from"
    #       " {}".format(addr))
    # while True:
    #     rcvdData = c.recv(1024).decode()
    #     print("* got '{}'".format(rcvdData))
    #     # print "S:",rcvdData
    #     # sendData = raw_input("N: ")
    #     req = json.loads(rcvdData)
    #     lfbs.push_action(req)
    #     res = {}
    #     res["status"] = "ok"
    #     c.send(json.dumps(res).encode())
    #     # if (sendData == "Bye" or sendData == "bye"):
    #         # break
    # c.close()


if __name__ == "__main__":
    main()
