#!/usr/bin/env python2
'''
  Folding@Home Client Control (FAHControl)
  Copyright (C) 2010-2016 Stanford University

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
'''

import os
import re
import subprocess
import socket
import select
import errno
import time
import sys
import traceback
import requests

#from fah.util import OrderedDict
from collections import OrderedDict

if sys.platform == 'win32':
    from ctypes import windll
    WSAGetLastError = windll.ws2_32.WSAGetLastError

debug = False
WSAEWOULDBLOCK = 10035

class Connection:
    def __init__(self, address = 'localhost', port = 36330, password = None,
                 retry_rate = 5):
        self.address = address
        self.port = int(port)
        self.password = password
        self.init_commands = []
        self.retry_rate = retry_rate

        self.socket = None
        self.reset()


    def set_init_commands(self, commands):
        self.init_commands = commands

        if self.is_connected():
            map(self.queue_command, self.init_commands)


    def get_status(self):
        if self.connected: return 'Online'
        #if self.socket is None: return 'Offline'
        return 'Connecting'


    def is_connected(self):
        if self.socket is None: return False
        if self.connected: return True

        rlist, wlist, xlist = select.select([], [self.socket], [self.socket], 0)

        if len(wlist) != 0: self.connected = True
        elif len(xlist) != 0:
            self.fail_reason = 'refused'
            self.close()

        return self.connected


    def can_write(self):
        rlist, wlist, xlist = select.select([], [self.socket], [], 0)
        return len(wlist) != 0


    def can_read(self):
        rlist, wlist, xlist = select.select([self.socket], [], [], 0)
        return len(rlist) != 0


    def reset(self):
        self.close()
        self.messages = []
        self.readBuf = ''
        self.writeBuf = ''
        self.fail_reason = None
        self.last_message = 0
        self.last_connect = 0


    def open(self):
        self.reset()
        self.last_connect = time.time()

        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setblocking(0)
        err = self.socket.connect_ex((self.address, self.port))

        if err != 0 and not err in [
            errno.EINPROGRESS, errno.EWOULDBLOCK, WSAEWOULDBLOCK]:
            self.fail_reason = 'connect'
            raise Exception('Connection failed: ' + errno.errorcode[err])

        if self.password: self.queue_command('auth "%s"' % self.password)
        map(self.queue_command, self.init_commands)


    def close(self):
        if self.socket is not None:
            try:
                self.socket.shutdown(socket.SHUT_RDWR)
            except: pass
            try:
                self.socket.close()
            except: pass
            self.socket = None

        self.connected = False


    def connection_lost(self):
        print ('Connection lost')
        self.close()
        self.fail_reason = 'closed'
        raise Exception('Lost connection')


    def connection_error(self, err, msg):
        print ('Connection Error: %d: %s' % (err, msg))
        self.close()
        if err == errno.ECONNREFUSED: self.fail_reason = 'refused'
        elif err in [errno.ETIMEDOUT, errno.ENETDOWN, errno.ENETUNREACH]:
            self.fail_reason = 'connect'
        else: self.fail_reason = 'error'


    def read_some(self):
        bytesRead = 0
        try:
            while True:
                buffer = self.socket.recv(10 * 1024 * 1024)
                if len(buffer):
                    #if debug: print 'BUFFER:', buffer
                    self.readBuf += buffer
                    bytesRead += len(buffer)
                else:
                    if bytesRead: return bytesRead
                    self.connection_lost()
                    return 0

        except socket.error as (err, msg):
            # Error codes for nothing to read
            if err not in [errno.EAGAIN, errno.EWOULDBLOCK, WSAEWOULDBLOCK]:
                if bytesRead: return bytesRead
                self.connection_error(err, msg)
                raise

        return bytesRead


    def write_some(self):
        if len(self.writeBuf) == 0: return 0

        bytesWritten = 0
        try:
            while True:
                count = self.socket.send(self.writeBuf)
                if count:
                    self.writeBuf = self.writeBuf[count:]
                    bytesWritten += count
                else:
                    if bytesWritten: return bytesWritten
                    self.connection_lost()
                    return 0

        except socket.error as (err, msg):
            # Error codes for write buffer full
            if err not in [errno.EAGAIN, errno.EWOULDBLOCK, WSAEWOULDBLOCK]:
                if bytesWritten: return bytesWritten
                self.connection_error(err, msg)
                raise

        return bytesWritten


    def queue_command(self, command):
        if debug: print ('command: ' + command)
        self.writeBuf += command + '\n'


    def parse_message(self, version, type, data):
        try:
            msg = eval(data, {}, {})
            #if debug: print 'MSG:', type, msg
            self.messages.append((version, type, msg))
            self.last_message = time.time()
        except Exception as e:
            print ('ERROR parsing PyON message: %s: %s'
                   % (str(e), data.encode('string_escape')))


    def parse(self):
        start = self.readBuf.find('\nPyON ')
        if start != -1:
            eol = self.readBuf.find('\n', start + 1)
            if eol != -1:
                line = self.readBuf[start + 1: eol]
                tokens = line.split(None, 2)

                if len(tokens) < 3:
                    self.readBuf = self.readBuf[eol:]
                    raise Exception('Invalid PyON line: ' + line.encode('string_escape'))

                version = int(tokens[1])
                type = tokens[2]

                end = self.readBuf.find('\n---\n', start)
                if end != -1:
                    data = self.readBuf[eol + 1: end]
                    self.parse_message(version, type, data)
                    self.readBuf = self.readBuf[end + 4:]
                    return True

        return False


    def update(self):
        try:
            try:
                if not self.is_connected():
                    if self.socket is None:
                        if self.last_connect + self.retry_rate < time.time():
                            self.open()

                    elif self.last_connect + 60 < time.time():
                        self.close() # Retry connect

                if not self.is_connected(): return

                self.write_some()
                if self.read_some():
                    while self.parse(): continue

            # Handle special case for OSX disconnect
            except socket.error as e:
                if sys.platform == 'darwin' and e.errno == errno.EPIPE:
                    self.fail_reason = 'refused'
                    self.close()

                else: raise

        except Exception as e:
            print ('ERROR on connection to %s:%d: %s' % (self.address, self.port, e))

        # Timeout connection
        if self.connected and self.last_message and \
                self.last_message + 10 < time.time():
            print ('Connection timed out')
            self.close()

# ('PyON 1 units:\n', [
#     {
#         'timeremaining': '7.77 days',
#         'ppd': '21844',
#         'assigned': '2020-03-24T03:37:18Z',
#         'attempts': 0,
#         'deadline': '2020-04-01T08:25:17Z',
#         'cs': '155.247.166.220',
#         'id': '02',
#         'unit': '0x0000000880fccb0a5e6fb579e154aff9',
#         'slot': '01',
#         'state': 'RUNNING',
#         'creditestimate': '9405',
#         'core': '0x22',
#         'run': 0,
#         'nextattempt': '0.00 secs',
#         'clone': 5038,
#         'error': 'NO_ERROR',
#         'totalframes': 100,
#         'ws': '128.252.203.10',
#         'framesdone': 99,
#         'gen': 1,
#         'tpf': '6 mins 12 secs',
#         'percentdone': '99.38%',
#         'basecredit': '9405',
#         'project': 11761,
#         'eta': '3 mins 49 secs',
#         'timeout': '2020-03-25T03:37:18Z',
#         'waitingon': ''
#     }, {
#         'timeremaining': 'unknown time',
#         'ppd': '0',
#         'assigned': '<invalid>',
#         'attempts': 12,
#         'deadline': '<invalid>',
#         'cs': '0.0.0.0',
#         'id': '01',
#         'unit': '0x00000000000000000000000000000000',
#         'slot': '00',
#         'state': 'DOWNLOAD',
#         'creditestimate': '0',
#         'core': 'unknown',
#         'run': 0,
#         'nextattempt': '1 hours 26 mins',
#         'clone': 0,
#         'error': 'NO_ERROR',
#         'totalframes': 0,
#         'ws': '40.114.52.201',
#         'framesdone': 0,
#         'gen': 0,
#         'tpf': '0.00 secs',
#         'percentdone': '0.00%',
#         'basecredit': '0',
#         'project': 0,
#         'eta': '0.00 secs',
#         'timeout': '<invalid>',
#         'waitingon': 'WS Assignment'
#     }, {
#         'timeremaining': '8.20 days',
#         'ppd': '9405',
#         'assigned': '2020-03-24T13:53:48Z',
#         'attempts': 0,
#         'deadline': '2020-04-01T18:41:47Z',
#         'cs': '155.247.166.220',
#         'id': '00',
#         'unit': '0x0000001080fccb0a5e6d7d103fda2892',
#         'slot': '01',
#         'state': 'READY',
#         'creditestimate': '9405',
#         'core': '0x22',
#         'run': 0,
#         'nextattempt': '0.00 secs',
#         'clone': 4845,
#         'error': 'NO_ERROR',
#         'totalframes': 0,
#         'ws': '128.252.203.10',
#         'framesdone': 0,
#         'gen': 8,
#         'tpf': '14 mins 24 secs',
#         'percentdone': '0.00%',
#         'basecredit': '9405',
#         'project': 11760,
#         'eta': '24 hours 00 mins',
#         'timeout': '2020-03-25T13:53:48Z',
#         'waitingon': ''
#     }
# ])
            
# ('PyON 1 units:\n', [
#  {
#   'timeremaining': '5.30 days',
#   'ppd': '1810',
#   'assigned': '2020-03-18T14:43:45Z',
#   'attempts': 0,
#   'deadline': '2020-03-23T23:11:00Z',
#   'cs': '155.247.164.214',
#   'id': '00',
#   'unit': '0x000000169bf7a4d55e655f50ef574c67',
#   'slot': '00',
#   'state': 'RUNNING',
#   'creditestimate': '220',
#   'core': '0xa7',
#   'run': 9,
#   'nextattempt': '0.00 secs',
#   'clone': 85,
#   'error': 'NO_ERROR',
#   'totalframes': 100,
#   'ws': '155.247.164.213',
#   'framesdone': 42,
#   'gen': 21,
#   'tpf': '1 mins 45 secs',
#   'percentdone': '42.00%',
#   'basecredit': '220',
#   'project': 14304,
#   'eta': '1 hours 41 mins',
#   'timeout': '2020-03-21T01:20:25Z',
#   'waitingon': ''
#  }, {'timeremaining': '8.17 days', 'ppd': '64574', 'assigned': '2020-03-18T15:04:31Z', 'attempts': 0, 'deadline': '2020-03-26T19:52:30Z', 'cs': '128.252.203.4', 'id': '01', 'unit': '0x000000078ca304e75e6bac2c2c8fe44f', 'slot': '01', 'state': 'RUNNING', 'creditestimate': '15396', 'core': '0x22', 'run': 0, 'nextattempt': '0.00 secs', 'clone': 7323, 'error': 'NO_ERROR', 'totalframes': 100, 'ws': '140.163.4.231', 'framesdone': 13, 'gen': 3, 'tpf': '3 mins 26 secs', 'percentdone': '13.54%', 'basecredit': '15396', 'project': 11747, 'eta': '4 hours 56 mins', 'timeout': '2020-03-19T15:04:31Z', 'waitingon': ''}])

def get_uptime():
    o = subprocess.check_output('uptime', shell=True)
    m = re.search("([0-9.]+)[^0-9]+([0-9.]+)[^0-9]+([0-9.]+)$", o)
    return {'raw': o, 'data': [float(v) for v in m.groups()]}

def get_nvidia():
    try:
        o = subprocess.check_output(
            ('nvidia-smi'
             ' --query-gpu=temperature.gpu,utilization.gpu,utilization.memory'
             ' --format=csv'),
            shell=True
        )
        d = dict(zip(*[i.split(',') for i in o.splitlines()]))
        return d
    except subprocess.CalledProcessError:
        return {}

def send_to_tsdb(options, units, uptime, nvidia):
    def make_point(m, v, **kw):
        tags = {
            'host': socket.gethostname(),
            'user': options['user']
        }
        tags.update(kw)
        return {
            'metric': m,
            'timestamp': time.time(),
            'value': v,
            'tags': tags 
        }

    send_data = [
        make_point('sys.load.1min', uptime['data'][0]),
        make_point('sys.load.5min', uptime['data'][1]),
        make_point('sys.load.15min', uptime['data'][2]),
    ]
    if nvidia:
        send_data.extend([
            make_point('sys.gpu.temperature', float(nvidia['temperature.gpu'])),
            make_point('sys.gpu.memory',
                       float(nvidia[' utilization.memory [%]'].strip(' %'))),
            make_point('sys.gpu.utilization',
                       float(nvidia[' utilization.gpu [%]'].strip(' %')))
        ])
    states = {}
    errors = {}
    for slot in units:
        states[slot['state'].lower()] = states.get(slot['state'].lower(), 0) + 1
        errors[slot['error'].lower()] = errors.get(slot['error'].lower(), 0) + 1
        send_data.extend([
            make_point('fah.project', slot['project'], slot=int(slot['slot'])),
            make_point('fah.complete', float(slot['percentdone'].strip('%')),
                       slot=int(slot['slot']))
        ])
    for k, v in states.items():
        send_data.append(make_point('fah.num_' + k, v))
    for k, v in errors.items():
        send_data.append(make_point('fah.error_' + k, v))

    try:
        r = requests.post(
            'http://opentsdb.fah.dyn.gutwin.org:4242/api/put?summary',
            json=send_data,
            timeout=1.1
        )
        print r.status_code, r.text
    except Exception, ex:
        pass

    
if __name__ == '__main__':
    init = [
        'options'
    ]
    conn = Connection()
    conn.set_init_commands(init)

    options = {}
    
    while True:
        conn.update()

        for version, type, data in conn.messages:
            #print ('PyON %d %s:\n' % (version, type), data)
            if type == 'options':
                options = data
                conn.queue_command('updates add 0 2 $queue-info')
            else:
                uptime = get_uptime()
                nvidia = get_nvidia()
                print '\033[2J\033[1;1H'
                print uptime['raw'].strip()
                print ' '.join('%s:%s' % i for i in nvidia.items())
                for i in data:
                    print 'slot', i['slot'], i['state'], 'project', i['project'], i['percentdone'], 'eta', i['eta'], 'tpf', i['tpf']
                print
                #send_to_tsdb(
                #    options=options,
                #    units=data,
                #    uptime=uptime,
                #    nvidia=nvidia
                #)
                
        conn.messages = []

        time.sleep(0.1)
