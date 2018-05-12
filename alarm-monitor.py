#!/usr/bin/env python
#
# Decoder for Texecom Connect API/Protocol
#
# Copyright (C) 2018 Joseph Heenan
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import socket
import time
import os
import sys
import re

import crcmod
import hexdump


class TexecomConnect:
    LENGTH_HEADER = 4
    HEADER_START = 't'
    HEADER_TYPE_COMMAND = 'C'
    HEADER_TYPE_RESPONSE = 'R'
    HEADER_TYPE_MESSAGE = 'M' # unsolicited message
    
    CMD_LOGIN = chr(1)
    CMD_GETZONEDETAILS = chr(3)
    CMD_GETLCDDISPLAY = chr(13)
    CMD_GETPANELIDENTIFICATION = chr(22)
    CMD_GETDATETIME = chr(23)
    CMD_SETEVENTMESSAGES = chr(37)
    
    ZONETYPE_UNUSED = 0

    CMD_RESPONSE_ACK = '\x06'
    CMD_RESPONSE_NAK = '\x15'
    
    MSG_DEBUG = chr(0)
    MSG_ZONEEVENT = chr(1)
    MSG_AREAEVENT = chr(2)
    MSG_OUTPUTEVENT = chr(3)
    MSG_USEREVENT = chr(4)
    MSG_LOGEVENT = chr(5)
    
    def __init__(self, host, port , message_handler_func):
        self.host = host
        self.port = port
        self.crc8_func = crcmod.mkCrcFun(poly=0x185, rev=False, initCrc=0xff)
        self.nextseq = 0
        self.message_handler_func = message_handler_func
        self.print_network_traffic = False
        self.last_command_time = 0
        self.last_received_seq = -1
        self.zone = {}

    def hexstr(self,s):
        return " ".join("{:02x}".format(ord(c)) for c in s)

    def connect(self):
        self.s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # 2-3 seconds is mentioned in section 5.5 of protocol specification
        # Increasing this value is not recommended as it will mean if the
        # panel fails to respond to a command (as it sometimes does it it
        # sends an event at the same time we send a command) it will take
        # longer for us to realise and resend the command
        self.s.settimeout(2)
        self.s.connect((self.host, self.port))
        # if we send the login message to fast the panel ignores it; texecom
        # recommend 500ms, see:
        # http://texecom.websitetoolbox.com/post/show_single_post?pid=1303528828&postcount=4&forum=627911
        time.sleep(0.5)
        
    def getnextseq(self):
        if self.nextseq == 256:
            self.nextseq = 0
        next=self.nextseq
        self.nextseq += 1
        return next

    
    def recvresponse(self):
        """Receive a response to a command. Automatically handles any
        messages that arrive first"""
        while True:
            header = self.s.recv(self.LENGTH_HEADER)
            if self.print_network_traffic:
                self.log("Received message header:")
                hexdump.hexdump(header)
            if header == "+++":
                self.log("Panel has forcibly dropped connection, possibly due to inactivity")
                self.s = None
                return None
            msg_start,msg_type,msg_length,msg_sequence = list(header)
            payload = self.s.recv(ord(msg_length) - self.LENGTH_HEADER)
            if self.print_network_traffic:
                self.log("Received message payload:")
                hexdump.hexdump(payload)
            payload, msg_crc = payload[:-1], ord(payload[-1])
            expected_crc = self.crc8_func(header+payload)
            if msg_start != 't':
                self.log("unexpected msg start: "+hex(ord(msg_start)))
                return None
            if msg_crc != expected_crc:
                self.log("crc: expected="+str(expected_crc)+" actual="+str(msg_crc))
                return None
            if msg_type == self.HEADER_TYPE_RESPONSE:
                if msg_sequence != self.last_sequence:
                    self.log("response seq: expected="+str(self.last_sequence)+" actual="+str(msg_sequence))
                    # FIXME: send command again
                    return None
            elif msg_type == self.HEADER_TYPE_MESSAGE:
                if self.last_received_seq != -1:
                    next_msg_seq = self.last_received_seq + 1
                    if next_msg_seq == 256:
                        next_msg_seq = 0
                    if msg_sequence != chr(next_msg_seq):
                        self.log("message seq: expected="+str(next_msg_seq)+" actual="+str(msg_sequence))
                        # should maybe process anyway unless it looks like a dup?
                        return None
                self.last_received_seq = ord(msg_sequence)
            # FIXME: check we received the full expected length
            # FIXME: if panel takes over 2 second to reply probably something is wrong and we need to resend the command with same sequence number
            if msg_type == self.HEADER_TYPE_COMMAND:
                self.log("received command unexpectedly")
                return None
            elif msg_type == self.HEADER_TYPE_RESPONSE:
                return payload
            elif msg_type == self.HEADER_TYPE_MESSAGE:
                self.message_handler_func(payload)
    
    def sendcommandbody(self, body):
        self.last_sequence = chr(self.getnextseq())
        data = self.HEADER_START+self.HEADER_TYPE_COMMAND+\
          chr(len(body)+5)+self.last_sequence+body
        data += chr(self.crc8_func(data))
        if self.print_network_traffic:
            self.log("Sending command:")
            hexdump.hexdump(data)
        self.s.send(data)
        self.last_command_time = time.time()
        self.last_command = data

    def login(self, udl):
        response = self.sendcommand(self.CMD_LOGIN, udl)
        if response == self.CMD_RESPONSE_NAK:
            self.log("NAK response from panel")
            return False
        elif response != self.CMD_RESPONSE_ACK:
            self.log("unexpected ack payload: "+hex(ord(response)))
            return False
        return True

    def set_event_messages(self):
        DEBUG_FLAG = 1
        ZONE_EVENT_FLAG = 1<<1
        AREA_EVENT_FLAG = 1<<2
        OUTPUT_EVENT_FLAG = 1<<3
        USER_EVENT_FLAG = 1<<4
        LOG_FLAG = 1<<5
        events = ZONE_EVENT_FLAG | AREA_EVENT_FLAG | OUTPUT_EVENT_FLAG | USER_EVENT_FLAG | LOG_FLAG
        body = chr(events & 0xff)+chr(events >> 8)
        response = self.sendcommand(self.CMD_SETEVENTMESSAGES, body)
        if response == self.CMD_RESPONSE_NAK:
            self.log("NAK response from panel")
            return False
        elif response != self.CMD_RESPONSE_ACK:
            self.log("unexpected ack payload: "+hex(ord(response)))
            return False
        return True

    def log(self, string):
        timestamp = time.strftime("%Y-%m-%d %X")
        print(timestamp + ": " + string)

    def sendcommand(self, cmd, body):
        if body:
            body = cmd+body
        else:
            body = cmd
        self.sendcommandbody(body)
        retries = 3
        while retries > 0:
            retries -= 1
            try:
                response=self.recvresponse()
                break
            except socket.timeout:
                # FIXME: this maybe isn't quite right as if we get multiple
                # events from the panel that will delay us resending until
                # we don't get any events for 2 second
                self.log("Timeout waiting for response, resending last command")
                # NB: sequence number will be the same as last attempt
                self.s.send(self.last_command)

        commandid,payload = response[0],response[1:]
        if commandid != cmd:
            if commandid == self.CMD_LOGIN and payload[0] == self.CMD_RESPONSE_NAK:
                self.log("Received 'Log on NAK' from panel - session has timed out and needs to be restarted")
                return None
            self.log("Got response for wrong command id: Expected "+hex(ord(cmd))+", got "+hex(ord(commandid)))
            self.log("Payload: "+self.hexstr(payload))
            return None
        return payload

    def get_date_time(self):
        datetime = self.sendcommand(self.CMD_GETDATETIME, None)
        if datetime == None:
            return None
        if len(datetime) < 6:
            self.log("GETDATETIME: response too short")
            self.log("Payload: "+self.hexstr(payload))
            return None
        datetime = bytearray(datetime)
        datetimestr = '20{2:02d}/{1:02d}/{0:02d} {3:02d}:{4:02d}:{5:02d}'.format(*datetime)
        self.log("Panel date/time: "+datetimestr)
        return datetimestr

    def get_lcd_display(self):
        lcddisplay = self.sendcommand(self.CMD_GETLCDDISPLAY, None)
        if lcddisplay == None:
            return None
        if len(lcddisplay) != 32:
            self.log("GETLCDDISPLAY: response wrong length")
            self.log("Payload: "+self.hexstr(payload))
            return None
        self.log("Panel LCD display: "+lcddisplay)
        return lcddisplay

    def get_panel_identification(self):
        panelid = self.sendcommand(self.CMD_GETPANELIDENTIFICATION, None)
        if panelid == None:
            return None
        if len(panelid) != 32:
            self.log("GETPANELIDENTIFICATION: response wrong length")
            self.log("Payload: "+self.hexstr(payload))
            return None
        self.log("Panel identification: "+panelid)
        return panelid

    def get_zone_details(self, zone):
        # FIXME: length of command & response varies depending on number of zones/areas on panel
        details = self.sendcommand(self.CMD_GETZONEDETAILS, chr(zone))
        if details == None:
            return None
        if len(details) < 34:
            self.log("GETZONEDETAILS: response wrong length")
            self.log("Payload: "+self.hexstr(payload))
            return None
        zonetype, areabitmap, zonetext = ord(details[0]), ord(details[1]), details[2:]
        zonetext = zonetext.replace("\x00", " ")
        zonetext = re.sub(r'\W+', ' ', zonetext)
        zonetext = zonetext.strip()
        if zonetype != self.ZONETYPE_UNUSED:
            self.log("zone {:d} zone type {:d} area bitmap {:x} text '{}'".
                format(zone, zonetype, areabitmap, zonetext))
        return (zonetype, areabitmap, zonetext)

    def get_all_zones(self):
        idstr = tc.get_panel_identification()
        panel_type,num_of_zones,something,firmware_version = idstr.split()
        num_of_zones = int(num_of_zones)
        for zone in range(1, num_of_zones + 1):
            # FIXME: if an event arrives whilst we're waiting for a response, it seems the panel doesn't reply, so we need to timeout and send again
            zonetype, areabitmap, zonetext = tc.get_zone_details(zone)
            zonedata = {
              'type' : zonetype,
              'areas' : areabitmap,
              'text' : zonetext
            }
            self.zone[zone] = zonedata

    def event_loop(self):
        while True:
            try:
                global garage_pir_activated_at
                if garage_pir_activated_at > 0:
                    active_for = time.time() - garage_pir_activated_at
                    self.log("Garage PIR active for {:.1f} minutes".format(active_for/60))
                    if active_for > 4*60:
                        garage_pir_activated_at=time.time()
                        os.system("./garage-pir.sh 'still active'")
                payload = tc.recvresponse()
        
            except socket.timeout:
                # FIXME: this should be in recvresponse, otherwise we
                # won't send if we get a continual stream of events from the
                # panels
                assert self.last_command_time > 0
                time_since_last_command = time.time() - self.last_command_time
                if time_since_last_command > 30:
                    # send any message to reset the panel's 60 second timeout
                    result = tc.get_date_time()
                    if result == None:
                        self.log("'get date time' failed; exiting")
                        # TODO could just reconnect
                        sys.exit(1)

    def debug_print_message(self, payload):
        msg_type,payload = payload[0],payload[1:]
        if msg_type == tc.MSG_DEBUG:
            self.log("Debug message: "+tc.hexstr(payload))
        elif msg_type == tc.MSG_ZONEEVENT:
            if len(payload) == 2:
                zone_number = ord(payload[0])
                zone_bitmap = ord(payload[1])
            elif len(payload) == 3:
                zone_number = ord(payload[0])+(ord(payload[1])<<8)
                zone_bitmap = ord(payload[2])
            else:
                self.log("unknown payload length")
            zone_state = zone_bitmap & 0x3
            zone_str = ["secure","active","tamper","short"][zone_bitmap & 0x3]
            if zone_bitmap & (1 << 2):
                zone_str += ", fault"
            if zone_bitmap & (1 << 3):
                zone_str += ", failed test"
            if zone_bitmap & (1 << 4):
                zone_str += ", alarmed"
            if zone_bitmap & (1 << 5):
                zone_str += ", manual bypassed"
            if zone_bitmap & (1 << 6):
                zone_str += ", auto bypassed"
            if zone_bitmap & (1 << 7):
                zone_str += ", zone masked"
            zone_text = self.zone[zone_number]['text']
            self.log("Zone event message: zone {:d} '{}' {}".
              format(zone_number, zone_text, zone_str))
        elif msg_type == tc.MSG_AREAEVENT:
            area_number = ord(payload[0])
            area_state = ord(payload[1])
            area_state_str = ["disarmed", "in exit", "in entry", "armed", "part armed", "in alarm"][area_state]
            self.log("Area event message: area "+str(area_number)+" "+area_state_str)
        elif msg_type == tc.MSG_OUTPUTEVENT:
            locations = ["Panel outputs",
            "Digi outputs",
            "Digi Channel low 8",
            "Digi Channel high 8",
            "Redcare outputs",
            "Custom outputs 1",
            "Custom outputs 2",
            "Custom outputs 3",
            "Custom outputs 4",
            "X-10 outputs"]
            output_location = ord(payload[0])
            output_state = ord(payload[1])
            if output_location < len(locations):
                output_name = locations[output_location]
            elif (output_location & 0xf) == 0:
                output_name = "Network {:d} keypad outputs".\
                  format(output_location >> 4, output_location & 0xf)
            else:
                output_name = "Network {:d} expander {:d} outputs".\
                  format(output_location >> 4, output_location & 0xf)
            self.log("Output event message: location {:d}['{}'] now 0x{:02x}".
              format(output_location, output_name, output_state))
        elif msg_type == tc.MSG_USEREVENT:
            user_number = ord(payload[0])
            user_state = ord(payload[1])
            user_state_str = ["code", "tag", "code+tag"][user_state]
            self.log("User event message: logon by user {:d} {}".
              format(user_number, user_state_str))
        elif msg_type == tc.MSG_LOGEVENT:
            self.log("Log event message: "+tc.hexstr(payload))
        else:
            self.log("unknown message type "+str(ord(msg_type))+": "+tc.hexstr(payload))

def message_handler(payload):
    tc.debug_print_message(payload)
    msg_type,payload = payload[0],payload[1:]
    if msg_type == tc.MSG_ZONEEVENT:
        zone_number = ord(payload[0])
        zone_bitmap = ord(payload[1])
        zone_state = zone_bitmap & 0x3
        if zone_number == 73:
            global garage_pir_activated_at
            if zone_state == 1:
                self.log("Garage PIR activated; running script")
                garage_pir_activated_at=time.time()
                os.system("./garage-pir.sh 'activated'")
            else:
                self.log("Garage PIR cleared")
                garage_pir_activated_at=0

# disable buffering to stdout when it's redirected to a file/pipe
# This makes sure any events appear immediately in the file/pipe,
# instead of being queued until there is a full buffer's worth.
class Unbuffered(object):
   def __init__(self, stream):
       self.stream = stream
   def write(self, data):
       self.stream.write(data)
       self.stream.flush()
   def writelines(self, datas):
       self.stream.writelines(datas)
       self.stream.flush()
   def __getattr__(self, attr):
       return getattr(self.stream, attr)

garage_pir_activated_at=0

if __name__ == '__main__':
    texhost = '192.168.1.9'
    port = 10001
    udlpassword = '1234'


    sys.stdout = Unbuffered(sys.stdout)
    tc = TexecomConnect(texhost, port, message_handler)
    tc.connect()
    if not tc.login(udlpassword):
        print("Login failed - udl password incorrect or pre-v4 panel, exiting.")
        sys.exit(1)
    print("login successful")
    if not tc.set_event_messages():
        print("Set event messages failed, exiting.")
        sys.exit(1)
    tc.get_date_time()
    tc.get_all_zones()
    print("Got all zones; waiting for events")
    tc.event_loop()
