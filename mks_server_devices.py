# Copyright (C) 2007  Matthew Neeley
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

# CHANGE LOG
#
# 24 March 2012 - Daniel Sank
#
# Reworked server 

"""
### BEGIN NODE INFO
[info]
name = MKS Gauge Server Devices
version = 2.0
description = 

[startup]
cmdline = %PYTHON% %FILE%
timeout = 20

[shutdown]
message = 987654321
timeout = 20
### END NODE INFO
"""

from labrad import types as T, util
from labrad.server import LabradServer, setting

from twisted.python import log
from twisted.internet import defer
from twisted.internet.defer import inlineCallbacks, returnValue

from datetime import datetime

CHANNELS = ['ch1', 'ch2']

class MKSGaugeWrapper(DeviceWrapper):
    pass

class MKSServer(DeviceServer):
    name = 'MKS Gauge Server Devices'

    @inlineCallbacks
    def initServer(self):
        pass
        
        
    @inlineCallbacks
    def findDevices(self):
        cxn = self.client
        reg = cxn.registry
        #Find out what gauge groups exist
        p = reg.packet()
        p.cd(REGISTRY_PATH)
        p.dir(key='dir')
        resp = yield p.send()
        gaugeGroupNames = resp['dir'][1]
        #Read the gaugeGroups from the registry
        p = reg.packet()
        for gaugeGroupName in gaugeGroupNames:
            p.get(gaugeGroupName, key = gaugeGroupName)
        resp = yield p.send()
        gaugeGroups = [resp[group] for group in gaugeGroupNames]            
        self.gaugeGroups = [(server, ID, [dict(port=port, ch1=ch1, ch2=ch2) for port,ch1,ch2 in gauges]) for server,ID,gauges in gaugeGroups]
        print 'Gauge groups loaded: '
        print self.gaugeGroups

    
    def serverConnected(self, ID, name):
        """Try to connect to gauges when a server connects."""
        self.findGauges()
        
    def serverDisconnected(self, ID, name):
        """Drop gauges from the list when a server disconnects."""
        for S in self.gaugeGroups:
            if S['ID'] == ID:
                print "'%s' disconnected." % S['server']
                S['ID'] = None
                removals = [G for G in self.gauges if G['server'].ID == ID]
                for G in removals:
                    self.gauges.remove(G)
    
    @inlineCallbacks
    def loadRegistryInfo(self):
        """ Each registry key should be in the form ('SerialServerName',ID,[(<COM PORT NAME>,<CH1 Name>,<CH2 Name>),...])
        
        ID should be an LabRAD w, -1 for no ID
        """
        reg = self.client.registry
        #Find out what gauge groups exist
        p = reg.packet()
        p.cd(['','Servers','MKS Gauges'])
        p.dir(key='dir')
        resp = yield p.send()
        gaugeGroupNames = resp['dir'][1]
        #Read the gaugeGroups from the registry
        p = reg.packet()
        for gaugeGroupName in gaugeGroupNames:
            p.get(gaugeGroupName, key = gaugeGroupName)
        resp = yield p.send()
        gaugeGroups = [resp[group] for group in gaugeGroupNames]            
        self.gaugeGroups = [(server, ID, [dict(port=port, ch1=ch1, ch2=ch2) for port,ch1,ch2 in gauges]) for server,ID,gauges in gaugeGroups]
        print 'Gauge groups loaded: '
        print self.gaugeGroups
        
    @inlineCallbacks
    def findGauges(self):
        """Look for gauges and servers."""
        cxn = self.client
        yield cxn.refresh()
        yield self.loadRegistryInfo()
        for serverName, ID, gauges in self.gaugeGroups:
            print 'serverName: ',serverName
            print 'ID: ',ID
            if ID != -1:
                continue
            if serverName in cxn.servers:
                server = cxn[serverName]
                ports = yield server.list_serial_ports()
                for gauge in gauges:
                    if gauge['port'] in ports:
                        yield self.connectToGauge(server, gauge)
        log.msg('Server ready')
                
        # for S in self.gaugeServers:
            # if S['ID'] is not None:
                # continue
            # if S['server'] in cxn.servers:
                # log.msg('Connecting to %s...' % S['server'])
                # ser = cxn[S['server']]
                # ports = yield ser.list_serial_ports()
                # for G in S['gauges']:
                    # if G['port'] in ports:
                        # yield self.connectToGauge(ser, G)
                # S['ID'] = ser.ID
        # log.msg('Server ready')


    @inlineCallbacks
    def connectToGauge(self, ser, G):
        """Connect to a single gauge."""
        port = G['port']
        ctx = G['context'] = ser.context()
        log.msg('  Connecting to %s...' % port)
        try:
            res = yield ser.open(port, context=ctx)
            ready = G['ready'] = res==port
        except:
            ready = False
        if ready:
            # set up baudrate
            p = ser.packet(context=ctx)\
                   .baudrate(9600L)\
                   .timeout()
            yield p.send()
            res = yield ser.read(context=ctx)
            while res:
                res = yield ser.read(context=ctx)
            yield ser.timeout(T.Value(2, 's'), context=ctx)

            # check units
            p = ser.packet()\
                   .write_line('u')\
                   .read_line(key='units')
            res = yield p.send(context=ctx)
            if 'units' in res.settings:
                G['units'] = res['units']
            else:
                G['ready'] = False

            # create a packet to read the pressure
            p = ser.packet(context=ctx)\
                   .write_line('p')\
                   .read_line(key='pressure')
            G['packet'] = p

            G['server'] = ser

        if ready:
            self.gauges.append(G)
            log.msg('    OK')
        else:
            log.msg('    ERROR')
        

    @setting(2, 'Get Gauge List', returns=['*s: Gauge names'])
    def list_gauges(self, c):
        """Request a list of available gauges."""
        gauges = [G[ch] for G in self.gauges
                        for ch in CHANNELS if G[ch]]
        return gauges


    @setting(1, 'Get Readings', returns=['*v[Torr]: Readings'])
    def get_readings(self, c):
        """Request current readings."""
        deferreds = [G['packet'].send() for G in self.gauges]
        res = yield defer.DeferredList(deferreds, fireOnOneErrback=True)
        
        readings = []
        strs = []
        for rslt, G in zip(res, self.gauges):
            s = rslt[1].pressure
            strs.append(s)
            r = rslt[1].pressure.split()
            for ch, rdg in zip(CHANNELS, r):
                if G[ch]:
                    try:
                        readings += [T.Value(float(rdg), 'Torr')]
                    except:
                        readings += [T.Value(0, 'Torr')]
        returnValue(readings)


__server__ = MKSServer()

if __name__ == '__main__':
    from labrad import util
    util.runServer(__server__)    
