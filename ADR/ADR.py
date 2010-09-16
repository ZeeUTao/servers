# Copyright (C) 2010  Daniel Sank
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

"""
### BEGIN NODE INFO
[info]
name = ADR
version = 0.1
description = Controls an ADR setup

[startup]
cmdline = %PYTHON% %FILE%
timeout = 20

[shutdown]
message = 987654321
timeout = 20
### END NODE INFO
"""

### TODO
#   Nail down error handling during startup
#   


from labrad.devices import DeviceServer, DeviceWrapper
from labrad import types as T, util
from labrad.server import setting
from twisted.internet.defer import inlineCallbacks, returnValue

import numpy as np
import time, exceptions, labrad.util

#Registry path to ADR configurations
CONFIG_PATH = ['','Servers','ADR']
# 9 Amps is the max, ladies and gentlemen
PS_MAX_CURRENT = 9.0
# if HANDSOFF, don't actually do anything
HANDSOFF = True

class Peripheral(object): #Probably should subclass DeviceWrapper here.
	
	def __init__(self,name,server,ID,ctxt):
		self.name = name
		self.ID = ID
		self.server = server
		self.ctxt = ctxt

	@inlineCallbacks
	def connect(self):
		yield self.server.select_device(self.ID,context=self.ctxt)

class ADRWrapper(DeviceWrapper):

	@inlineCallbacks
	def connect(self, *args, **peripheralDict):
		"""     
		TODO: Add error checking and handling
		"""
		#Give the ADR a client connection to LabRAD.
		#ADR's use the same connection as the ADR server.
		#Each ADR makes LabRAD requests in its own context.
		self.cxn = args[0]
		self.ctxt = self.cxn.context()
		# give us a blank log
		self.logData = []
		# set the defaults of our state variables
		self.stateVars= {	'quenchLimit': 4.0,			# K
							'cooldownLimit': 3.9,		# K
							'rampWaitTime': 0.2,		# s
							'voltageStepUp': 0.004, 	# V
							'voltageStepDown': 0.004,	# V
							'voltageLimit': 0.28,		# V
							'targetCurrent': 8,			# A
							'maxCurrent': 9,			# A
							'fieldWaitTime': 2.0,		# min
							'ruoxSwitch': 2,
							'waitToMagDown': False,
							'autoControl': False,
							'switchPosition': 2,
							'timeMaggedDown': None,			# time when we finished magging down
							'scheduledMagDownTime': None,	# time to start magging down
							'scheduledMagUpTime': None,		# time to start magging up
							'alive': False,
						}
		# different possible statuses
		self.possibleStatuses = ['cooling down', 'ready', 'waiting at field', 'waiting to mag up', 'magging up', 'magging down', 'ready to mag down']
		self.currentStatus = 'cooling down'
		self.sleepTime = 1.0
		# functions and coefficients for converting ruox res to temp
		# these should probably be exported to the registry at some point
		self.ruoxCoefs = [-0.3199412, 5.74884e-8, -8.8409e-11]
		self.highTempRuoxCurve = lambda r, p: 1 / (p[0] + p[1] * r**2 * np.log(r) + p[2] * r**3)
		self.lowTempRuoxCurve = lambda r, p: 1 / (p[0] + p[1] * r * np.log(r) + p[2] * r**2 * np.log(r))
		self.voltToResCalibs = [0.26, 26.03, 25.91, 25.87, 26.36, 26.53] # in mV / kOhm, or microamps
		self.resistanceCutoff = 1725.78
		self.ruoxChannel = 4 - 1 # channel 4, index 3
		# find our peripherals
		yield self.refreshPeripherals()
		# go!
		self.log("Initialization completed. Beginning cycle.")
		self.cycle()

	
	##############################
	# STATE MANAGEMENT FUNCTIONS #
	##############################
	
	# (these are the functions that do stuff) #
	
	@inlineCallbacks
	def cycle(self):
		"""
		this function should get called after the server finishes connecting. it doesn't return.
		each of the statuses will have a sleep for a given amount of time (usually 1s or rampWaitTime).
		"""
		self.state('alive', True)
		while self.state('alive'):
			if self.currentStatus == 'cooling down':
				yield util.wakeupCall(self.sleepTime)
				# check if we're at base, then set status -> ready
				if (yield self.atBase()):
					self.status('ready')
			elif self.currentStatus == 'ready':
				yield util.wakeupCall(self.sleepTime)
				# check if we're at base, then set status -> cooling
				if not (yield self.atBase()):
					self.status('cooling down')
			elif self.currentStatus == 'waiting at field':
				yield util.wakeupCall(self.sleepTime)
				# is it time to mag down?
				if time.time() > self.state('scheduledMagDownTime'):
					if self.state('waitToMagDown'):
						self.status('ready')
					else:
						self.status('magging down')
				else:
					self.psMaxCurrent() # I think?
			elif self.currentStatus == 'waiting to mag up':
				yield util.wakeupCall(self.sleepTime)
				# is it time to mag up?
				if time.time() > self.state('scheduledMagUpTime') and (yield self.atBase()):
					self.status('magging up')
			elif self.currentStatus == 'magging up':
				yield util.wakeupCall(self.state('rampWaitTime'))
				self.clear('timeMaggedDown')
				(quenched, targetReached) = yield self.adrMagStep(True) # True = mag step up
				self.log("%s mag step! Quenched: %s -- Target Reached: %s" % (self.name, quenched, targetReached))
				if quenched:
					self.log("Quenched!")
					self.status('cooling down')
				elif targetReached:
					self.status('waiting at field')
					self.psMaxCurrent() # I think?
				else:
					pass # if at first we don't succeed, mag, mag again
			elif self.currentStatus == 'magging down':
				yield util.wakeupCall(self.state('rampWaitTime'))
				self.clear('scheduledMagDownTime')
				(quenched, targetReached) = yield self.adrMagStep(False)
				self.log("%s mag step! Quenched: %s -- Target Reached: %s" % (self.name, quenched, targetReached))
				if quenched:
					self.log("%s Quenched!" % self.name)
					self.status('cooling down')
				elif targetReached:
					self.status('ready')
					self.state('timeMaggedDown', time.time())
					self.psOutputOff()
			elif self.currentStatus == 'ready to mag down':
				yield util.wakeupCall(self.sleepTime)
				self.psMaxCurrent()
			else:
				yield util.wakeupCall(self.sleepTime)
	
	# these are copied from the LabView program
	# TODO: add error checking
	@inlineCallbacks
	def atBase(self):
		try:
			ls = self.peripheralsConnected['lakeshore']
			temps = yield ls.server.temperatures(context = ls.ctxt)
			returnValue( (temps[1].value < self.state('cooldownLimit')) and (temps[2].value < self.state('cooldownLimit')) )
		except exceptions.KeyError, e:
			#print "ADR %s has no lakeshore" % self.name
			returnValue(False)
	
	def psMaxCurrent(self):
		""" sets the magnet current to the max current. (I think that's what it's supposed to do.) """
		newCurrent = min(PS_MAX_CURRENT, self.state('maxCurrent'))
		if newCurrent < 0:
			newCurrent = PS_MAX_CURRENT
		ps = self.peripheralsConnected['magnet']
		if HANDSOFF:
			self.log("%s magnet current -> %s" % (self.name, newCurrent))
		else:
			magnet.server.current(newCurrent, context=magnet.ctxt)
	
	@inlineCallbacks
	def psOutputOff(self):
		""" Turns off the magnet power supply, basically. """
		ps = self.peripheralsConnected['magnet']
		p = ps.server.packet(context=ps.ctxt)
		p.voltage(0)
		p.current(0)
		if HANDSOFF:
			self.log(p.__str__())
		else:
			yield p.send()
		yield util.wakeupCall(0.5)
		if HANDSOFF:
			self.log("%s magnet output_state -> false" % self.name)
		else:
			yield ps.output_state(False)
		
	@inlineCallbacks
	def psOutputOn(self):
		""" Turns on the power supply. """
		ps = self.peripheralsConnected['magnet']
		p = ps.server.packet(context=ps.ctxt)
		newCurrent = min(PS_MAX_CURRENT, self.state('maxCurrent'))
		if newCurrent < 0:
			newCurrent = PS_MAX_CURRENT
		p.current(newCurrent)
		p.output_state(True)
		if HANDSOFF:
			self.log(p.__str__())
		else:
			yield p.send()
	
	@inlineCallbacks
	def adrMagStep(self, up):
		""" If up is True, mags up a step. If up is False, mags down a step. """
		ls = self.peripheralsConnected['lakeshore']
		temps = yield ls.server.temperatures(context = ls.ctxt)
		volts = yield ls.server.voltages(context = ls.ctxt)
		ps = self.peripheralsConnected['magnet']
		current = yield ps.server.current(context=ps.ctxt)
		voltage = yield ps.server.voltage(context=ps.ctxt)
		quenched = temps[1].value > self.state('quenchLimit') and current > 0.5
		targetReached = (up and self.state('targetCurrent') - current < 0.001) or (not up and self.state('targetCurrent') > current)
		newVoltage = voltage
		if not quenched and not targetReached and volts[6] < self.state('voltageLimit'):
			if up:
				newVoltage += self.state('voltageStepUp')
			else:
				newVoltage -= self.state('voltageStepDown')
		if HANDSOFF:
			self.log("%s magnet voltage -> %s" % (self.name, newVoltage))
		else:
			yield ps.server.voltage(newVoltage, context=ps.context)
		returnValue((quenched, targetReached))
		
	
	#########################
	# DATA OUTPUT FUNCTIONS #
	#########################
	
	# getter/setter for state variables
	def state(self, var, newValue = None):
		if newValue is not None:
			self.stateVars[var] = newValue
			# check for scheduled mag up time
			if var == 'scheduledMagUpTime' and self.currentStatus == 'ready':
				self.status('waiting to mag up')
		return self.stateVars[var]
	# clear a state variable
	def clear(self, var):
		self.stateVars[var] = None
	
	# getter/setter for status
	def status(self, newStatus = None):
		if (newStatus is not None) and (newStatus not in self.possibleStatuses):
			self.log("ERROR: status %s not in possibleStatuses!" % newStatus)
		elif (newStatus is not None) and not (newStatus == self.currentStatus):
			self.currentStatus = newStatus
			if newStatus == 'magging up':
				if self.state('autoControl'):
					self.setHeatSwitch(True)
				self.psOutputOn()
			elif newStatus == 'magging down':
				if self.state('autoControl'):
					self.setHeatSwitch(False)
			elif newStatus == 'ready':
				if self.state('scheduledMagUpTime') > time.time():
					self.status('waiting to mag up')
			elif newStatus == 'waiting at field':
				self.scheduledMagDownTime = time.time() + self.fieldWaitTime * 60
			self.log("ADR %s status is now: %s" % (self.name, self.currentStatus))
		return self.currentStatus
	
	# returns the cold stage resistance and temperature
	# interpreted from "RuOx thermometer.vi" LabView program, such as I can
	# the voltage reading is from lakeshore channel 4 (i.e. index 3)
	@inlineCallbacks
	def ruoxStatus(self):
		try:
			calib = self.voltToResCalibs[self.state('switchPosition') - 1]
			ls = self.peripheralsConnected['lakeshore']
			voltage = (yield ls.server.voltages(context=ls.ctxt))[self.ruoxChannel].value
			resistance = voltage / (calib )#* 10**6) # may or may not need this factor of 10^6
			temp = 0.0
			if resistance < self.resistanceCutoff:
				# high temp (2 to 20 K)
				temp = self.highTempRuoxCurve(resistance, self.ruoxCoefs)
			else:
				# low temp (0.05 to 2 K)
				temp = self.lowTempRuoxCurve(resistance, self.ruoxCoefs)
			returnValue((temp, resistance))
		except Exception, e:
			print e
			returnValue((0.0, 0.0))
			
	#################################
	# PERIPHERAL HANDLING FUNCTIONS	#
	#################################
	
	@inlineCallbacks
	def refreshPeripherals(self):
		self.allPeripherals = yield self.findPeripherals()
		self.peripheralOrphans = {}
		self.peripheralsConnected = {}
		for peripheralName, idTuple in self.allPeripherals.items():
			yield self.attemptPeripheral((peripheralName, idTuple))

	@inlineCallbacks
	def findPeripherals(self):
		"""Finds peripheral device definitions for a given ADR (from the registry)
		OUTPUT
			peripheralDict - dictionary {peripheralName:(serverName,identifier)..}
		"""
		reg = self.cxn.registry
		yield reg.cd(CONFIG_PATH + [self.name])
		dirs, keys = yield reg.dir()
		p = reg.packet()
		for peripheral in keys:
			p.get(peripheral, key=peripheral)
		ans = yield p.send()
		peripheralDict = {}
		for peripheral in keys: #all key names in this directory
			peripheralDict[peripheral] = ans[peripheral]
		returnValue(peripheralDict)

	@inlineCallbacks
	def attemptOrphans(self):
		for peripheralName, idTuple in self.peripheralOrphans.items():
			yield self.attemptPeripheral((peripheralName, idTuple))

	@inlineCallbacks
	def attemptPeripheral(self,peripheralTuple):
		"""
		Attempts to connect to a specified peripheral. If the peripheral's server exists and
		the desired peripheral is known to that server, then the peripheral is selected in
		this ADR's context. Otherwise the peripheral is added to the list of orphans.
		
		INPUTS:
		peripheralTuple - (peripheralName,(serverName,peripheralIdentifier))
		(Note that peripherialIdentifier can either be the full name (e.g. "Kimble GPIB Bus - GPIB0::5")
		or just the node name (e.g. "Kimble")).
		"""
		peripheralName = peripheralTuple[0]
		serverName = peripheralTuple[1][0]
		peripheralID = peripheralTuple[1][1]

		#If the peripheral's server exists, get it,
		if serverName in self.cxn.servers:
			server = self.cxn.servers[serverName]
		#otherwise orphan this peripheral and tell the user.
		else:
			self._orphanPeripheral(peripheralTuple)
			print 'Server ' + serverName + ' does not exist.'
			print 'Check that the server is running and refresh this ADR'
			return

		# If the peripheral's server has this peripheral, select it in this ADR's context.
		devices = yield server.list_devices()
		if peripheralID in [device[1] for device in devices]:
			yield self._connectPeripheral(server, peripheralTuple)
		# if we couldn't find the peripheral directly, check to see if the node name matches
		# (i.e. if the beginnings of the strings match)
		elif peripheralID in [device[1][0:len(peripheralID)] for device in devices]:
			# find the (first) device that matches
			for device in devices:
				if peripheralID == device[1][0:len(peripheralID)]:
					# connect it
					#print "Connecting to %s for %s" % (device
					yield self._connectPeripheral(server, (peripheralName, (serverName, device[1])))
					# don't connect more than one!
					break
		# otherwise, orphan it
		else:
			print 'Server '+ serverName + ' does not have device ' + peripheralID
			self._orphanPeripheral(peripheralTuple)

	@inlineCallbacks
	def _connectPeripheral(self, server, peripheralTuple):
		peripheralName = peripheralTuple[0]
		ID = peripheralTuple[1][1]
		#Make the actual connection to the peripheral device!
		self.peripheralsConnected[peripheralName] = Peripheral(peripheralName,server,ID,self.ctxt)
		yield self.peripheralsConnected[peripheralName].connect()
		print "connected to %s for %s" % (ID, peripheralName)

	def _orphanPeripheral(self,peripheralTuple):
		peripheralName = peripheralTuple[0]
		idTuple = peripheralTuple[1]
		if peripheralName not in self.peripheralOrphans:
			self.peripheralOrphans[peripheralName] = idTuple
	
	#####################
	# LOGGING FUNCTIONS #
	#####################
	
	def log(self, str):
		self.logData.append((time.strftime("%Y-%m-%d %H:%M:%S"), str))
	
	def getLog(self):
		return self.logData
	
# (end of ADRWrapper)

############################
##### ADR SERVER CLASS #####
############################

class ADRServer(DeviceServer):
	name = 'ADR Server'
	deviceName = 'ADR'
	deviceWrapper = ADRWrapper
	
	def initServer(self):
		return DeviceServer.initServer(self)
	
	def stopServer(self):
		return DeviceServer.stopServer(self)

	@inlineCallbacks
	def findDevices(self):
		"""Finds all ADR configurations in the registry at CONFIG_PATH and returns a list of (ADR_name,(),peripheralDictionary).
		INPUTS - none
		OUTPUT - List of (ADRName,(connectionObject,context),peripheralDict) tuples.
		"""
		deviceList=[]
		reg = self.client.registry
		yield reg.cd(CONFIG_PATH)
		resp = yield reg.dir()
		ADRNames = resp[0].aslist
		for name in ADRNames:
			deviceList.append((name,(self.client,)))
		returnValue(deviceList)


	@setting(21, 'refresh peripherals', returns=[''])
	def refresh_peripherals(self,c):
		"""Refreshes peripheral connections for the currently selected ADR"""

		dev = self.selectedDevice(c)
		yield dev.refreshPeripherals()

	@setting(22, 'list all peripherals', returns='*?')
	def list_all_peripherals(self,c):
		dev = self.selectedDevice(c)
		peripheralList=[]
		for peripheral,idTuple in dev.allPeripherals.items():
			peripheralList.append((peripheral,idTuple))
		return peripheralList

	@setting(23, 'list connected peripherals', returns='*?')
	def list_connected_peripherals(self,c):
		dev = self.selectedDevice(c)
		connected=[]
		for name, peripheral in dev.peripheralsConnected.items():
			connected.append((peripheral.name,peripheral.ID))
		return connected

	@setting(24, 'list orphans', returns='*?')
	def list_orphans(self,c):
		dev = self.selectedDevice(c)
		orphans=[]
		for peripheral,idTuple in dev.peripheralOrphans.items():
			orphans.append((peripheral,idTuple))
		return orphans

	@setting(32, 'echo PNA', data=['?'], returns=['?'])
	def echo_PNA(self,c,data):
		dev = self.selectedDevice(c) #this gets the selected ADR
		if 'PNA' in dev.peripheralsConnected.keys():
			PNA = dev.peripheralsConnected['PNA']
			resp = yield PNA.server.echo(data, context=PNA.ctxt)
			returnValue(resp)
	
	@setting(40, 'Voltages', returns=['*v[V]'])
	def voltages(self, c):
		""" Returns the voltages from this ADR's lakeshore diode server. """
		dev = self.selectedDevice(c)
		if 'lakeshore' in dev.peripheralsConnected.keys():
			volts = yield dev.peripheralsConnected['lakeshore'].server.voltages(context=dev.ctxt)
			returnValue(volts)
		else:
			returnValue([0.0]*8)
	
	@setting(41, 'Temperatures', returns=['*v[K]'])
	def temperatures(self, c):
		""" Returns the temperatures from this ADR's lakeshore diode server. """
		dev = self.selectedDevice(c)
		if 'lakeshore' in dev.peripheralsConnected.keys():
			temps = yield dev.peripheralsConnected['lakeshore'].server.temperatures(context=dev.ctxt)
			returnValue(temps)
		else:
			returnValue([0.0]*8)
			
	@setting(42, 'Magnet Status', returns=['(v[V] v[A])'])
	def magnet_status(self, c):
		""" Returns the voltage and current from the magnet power supply. """
		dev = self.selectedDevice(c)
		if 'magnet' in dev.peripheralsConnected.keys():
			mag = dev.peripheralsConnected['magnet']
			current = yield mag.server.voltage(context=mag.ctxt)
			voltage = yield mag.server.current(context=mag.ctxt)
			returnValue((current, voltage))
		else:
			returnValue((0, 0))
			
	@setting(43, 'Compressor Status', returns=['b'])
	def compressor_status(self, c):
		""" Returns True if the compressor is running, false otherwise. """
		dev = self.selectedDevice(c)
		if 'compressor' in dev.peripheralsConnected.keys():
			comp = dev.peripheralsConnected['compressor']
			stat = yield comp.server.status(context=comp.ctxt)
			returnValue(stat)
		else:
			#raise Exception("No compressor selected")
			returnValue(False)
			
	@setting(44, 'Ruox Status', returns=['(v[K] v[Ohm])'])
	def ruox_status(self, c):
		""" Returns the temperature and resistance measured at the cold stage. """
		dev = self.selectedDevice(c)
		return dev.ruoxStatus()
	
	@setting(50, 'List State Variables', returns=['*s'])
	def list_state_variables(self, c):
		""" Returns a list of all the state variables for this ADR. """
		dev = self.selectedDevice(c)
		return dev.stateVars.keys()
	
	@setting(51, 'Set State', variable = 's', value='?')
	def set_state(self, c, variable, value):
		""" Sets the given state variable to the given value. """
		dev = self.selectedDevice(c)
		dev.state(variable, value)
	
	@setting(52, 'Get State', variable = 's', returns=["?"])
	def get_state(self, c, variable):
		""" Gets the value of the given state variable. """
		dev = self.selectedDevice(c)
		return dev.state(variable)
	
	@setting(53, 'Status', returns = ['s'])
	def status(self, c):
		""" Returns the status (e.g. "cooling down", "waiting to mag up", etc.) """
		dev = self.selectedDevice(c)
		return dev.status()
	
	@setting(54, 'List Statuses', returns = ['*s'])
	def list_statuses(self, c):
		""" Returns a list of all allowed statuses. """
		dev = self.selectedDevice(c)
		return dev.possibleStatuses
		
	@setting(55, 'Change Status', value='s')
	def change_status(self, c, value):
		""" Changes the status of the ADR server. """
		dev = self.selectedDevice(c)
		dev.status(value)
		
	@setting(56, "Get Log", returns = ['*(ss)'])
	def get_log(self, c):
		""" Gets this ADR's log. """
		dev = self.selectedDevice(c)
		return dev.getLog()
	
__server__ = ADRServer()

if __name__ == '__main__':
	from labrad import util
	util.runServer(__server__)
