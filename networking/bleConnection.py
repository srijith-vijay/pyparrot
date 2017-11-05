from bluepy.btle import Peripheral, UUID, DefaultDelegate, BTLEException
from utils.colorPrint import color_print
import struct

class MamboDelegate(DefaultDelegate):
    """
    Handle BLE notififications
    """
    def __init__(self, handle_map, mambo):
        DefaultDelegate.__init__(self)
        self.handle_map = handle_map
        self.mambo = mambo
        self.mambo._debug_print("initializing notification delegate", 10)

    def handleNotification(self, cHandle, data):
        #print "handling notificiation from channel %d" % cHandle
        #print "handle map is %s " % self.handle_map[cHandle]
        #print "channel map is %s " % self.mambo.characteristic_receive_uuids[self.handle_map[cHandle]]
        #print "data is %s " % data

        channel = self.mambo.characteristic_receive_uuids[self.handle_map[cHandle]]
        if channel == 'ACK_DRONE_DATA':
            # data received from drone (needs to be ack on 1e)
            self.mambo._update_sensors(data, ack=True)
        elif channel == 'NO_ACK_DRONE_DATA':
            # data from drone (including battery and others), no ack
            self.mambo._debug_print("drone data - no ack needed", 2)
            self.mambo._update_sensors(data, ack=False)
        elif channel == 'ACK_COMMAND_SENT':
            # ack 0b channel, SEND_WITH_ACK
            self.mambo._debug_print("Ack!  command received!", 2)
            self.mambo._set_command_received('SEND_WITH_ACK', True)
        elif channel == 'ACK_HIGH_PRIORITY':
            # ack 0c channel, SEND_HIGH_PRIORITY
            self.mambo._debug_print("Ack!  high priority received", 2)
            self.mambo._set_command_received('SEND_HIGH_PRIORITY', True)
        else:
            self.mambo._debug_print("unknown channel %s sending data " % channel, 10)
            self.mambo._debug_print(cHandle)


class BLEConnection:
    def __init__(self, address):
        """
             Initialize with its BLE address - if you don't know the address, call findMambo
             and that will discover it for you.

             :param address: unique address for this mambo
             """
        self.address = address
        self.drone = Peripheral()

        # the following UUID segments come from the Mambo and from the documenation at
        # http://forum.developer.parrot.com/t/minidrone-characteristics-uuid/4686/3
        # the 3rd and 4th bytes are used to identify the service
        self.service_uuids = {
            'fa00': 'ARCOMMAND_SENDING_SERVICE',
            'fb00': 'ARCOMMAND_RECEIVING_SERVICE',
            'fc00': 'PERFORMANCE_COUNTER_SERVICE',
            'fd21': 'NORMAL_BLE_FTP_SERVICE',
            'fd51': 'UPDATE_BLE_FTP',
            'fe00': 'UPDATE_RFCOMM_SERVICE',
            '1800': 'Device Info',
            '1801': 'unknown',
        }
        # the following characteristic UUID segments come from the documentation at
        # http://forum.developer.parrot.com/t/minidrone-characteristics-uuid/4686/3
        # the 4th bytes are used to identify the characteristic
        # the usage of the channels are also documented here
        # http://forum.developer.parrot.com/t/ble-characteristics-of-minidrones/5912/2
        self.characteristic_send_uuids = {
            '0a': 'SEND_NO_ACK',  # not-ack commands (PCMD only)
            '0b': 'SEND_WITH_ACK',  # ack commands (all piloting commands)
            '0c': 'SEND_HIGH_PRIORITY',  # emergency commands
            '1e': 'ACK_COMMAND'  # ack for data sent on 0e
        }

        # counters for each packet (required as part of the packet)
        self.characteristic_send_counter = {
            'SEND_NO_ACK': 0,
            'SEND_WITH_ACK': 0,
            'SEND_HIGH_PRIORITY': 0,
            'ACK_COMMAND': 0,
            'RECEIVE_WITH_ACK': 0
        }

        # the following characteristic UUID segments come from the documentation at
        # http://forum.developer.parrot.com/t/minidrone-characteristics-uuid/4686/3
        # the 4th bytes are used to identify the characteristic
        # the types of commands and data coming back are also documented here
        # http://forum.developer.parrot.com/t/ble-characteristics-of-minidrones/5912/2
        self.characteristic_receive_uuids = {
            '0e': 'ACK_DRONE_DATA',  # drone data that needs an ack (needs to be ack on 1e)
            '0f': 'NO_ACK_DRONE_DATA',  # data from drone (including battery and others), no ack
            '1b': 'ACK_COMMAND_SENT',  # ack 0b channel, SEND_WITH_ACK
            '1c': 'ACK_HIGH_PRIORITY',  # ack 0c channel, SEND_HIGH_PRIORITY
        }

        # these are the FTP incoming and outcoming channels
        # the handling characteristic seems to be the one to send commands to (per the SDK)
        # information gained from reading ARUTILS_BLEFtp.m in the SDK
        self.characteristic_ftp_uuids = {
            '22': 'NORMAL_FTP_TRANSFERRING',
            '23': 'NORMAL_FTP_GETTING',
            '24': 'NORMAL_FTP_HANDLING',
            '52': 'UPDATE_FTP_TRANSFERRING',
            '53': 'UPDATE_FTP_GETTING',
            '54': 'UPDATE_FTP_HANDLING',
        }

        # FTP commands (obtained via ARUTILS_BLEFtp.m in the SDK)
        self.ftp_commands = {
            "list": "LIS",
            "get": "GET"
        }

        # need to save for communication (but they are initialized in connect)
        self.services = None
        self.send_characteristics = dict()
        self.receive_characteristics = dict()
        self.handshake_characteristics = dict()
        self.ftp_characteristics = dict()

        self.data_types = {
            'ACK': 1,
            'DATA_NO_ACK': 2,
            'LOW_LATENCY_DATA': 3,
            'DATA_WITH_ACK': 4
        }

        # store whether a command was acked
        self.command_received = {
            'SEND_WITH_ACK': False,
            'SEND_HIGH_PRIORITY': False,
            'ACK_COMMAND': False
        }

        # instead of parsing the XML file every time, cache the results
        self.command_tuple_cache = dict()
        self.sensor_tuple_cache = dict()

        # maximum number of times to try a packet before assuming it failed
        self.max_packet_retries = 3

    def connect(self, num_retries):
        """
        Connects to the drone and re-tries in case of failure the specified number of times

        :param: num_retries is the number of times to retry

        :return: True if it succeeds and False otherwise
        """

        # first try to connect to the wifi
        try_num = 1
        connected = False
        while (try_num < num_retries and not connected):
            try:
                self._connect()
                connected = True
            except BTLEException:
                color_print("retrying connections", "INFO")
                try_num += 1

        # fall through, return False as something failed
        return connected

    def _reconnect(self, num_retries):
        """
        Reconnect to the drone (assumed the BLE crashed)

        :param: num_retries is the number of times to retry

        :return: True if it succeeds and False otherwise
        """
        try_num = 1
        success = False
        while (try_num < num_retries and not success):
            try:
                color_print("trying to re-connect to the mambo at address %s" % self.address, "WARN")
                self.drone.connect(self.address, "random")
                color_print("connected!  Asking for services and characteristics", "SUCCESS")
                success = True
            except BTLEException:
                color_print("retrying connections", "WARN")
                try_num += 1

        if (success):
            # do the magic handshake
            self._perform_handshake()

        return success

    def _connect(self):
        """
        Connect to the mambo to prepare for flying - includes getting the services and characteristics
        for communication

        :return: throws an error if the drone connection failed.  Returns void if nothing failed.
        """
        color_print("trying to connect to the mambo at address %s" % self.address, "INFO")
        self.drone.connect(self.address, "random")
        color_print("connected!  Asking for services and characteristics", "SUCCESS")

        # re-try until all services have been found
        allServicesFound = False

        # used for notifications
        handle_map = dict()

        while not allServicesFound:
            # get the services
            self.services = self.drone.getServices()

            # loop through the services
            for s in self.services:
                hex_str = self._get_byte_str_from_uuid(s.uuid, 3, 4)

                # store the characteristics for receive & send
                if (self.service_uuids[hex_str] == 'ARCOMMAND_RECEIVING_SERVICE'):
                    # only store the ones used to receive data
                    for c in s.getCharacteristics():
                        hex_str = self._get_byte_str_from_uuid(c.uuid, 4, 4)
                        if hex_str in self.characteristic_receive_uuids:
                            self.receive_characteristics[self.characteristic_receive_uuids[hex_str]] = c
                            handle_map[c.getHandle()] = hex_str


                elif (self.service_uuids[hex_str] == 'ARCOMMAND_SENDING_SERVICE'):
                    # only store the ones used to send data
                    for c in s.getCharacteristics():
                        hex_str = self._get_byte_str_from_uuid(c.uuid, 4, 4)
                        if hex_str in self.characteristic_send_uuids:
                            self.send_characteristics[self.characteristic_send_uuids[hex_str]] = c


                elif (self.service_uuids[hex_str] == 'UPDATE_BLE_FTP'):
                    # store the FTP info
                    for c in s.getCharacteristics():
                        hex_str = self._get_byte_str_from_uuid(c.uuid, 4, 4)
                        if hex_str in self.characteristic_ftp_uuids:
                            self.ftp_characteristics[self.characteristic_ftp_uuids[hex_str]] = c

                elif (self.service_uuids[hex_str] == 'NORMAL_BLE_FTP_SERVICE'):
                    # store the FTP info
                    for c in s.getCharacteristics():
                        hex_str = self._get_byte_str_from_uuid(c.uuid, 4, 4)
                        if hex_str in self.characteristic_ftp_uuids:
                            self.ftp_characteristics[self.characteristic_ftp_uuids[hex_str]] = c

                # need to register for notifications and write 0100 to the right handles
                # this is sort of magic (not in the docs!) but it shows up on the forum here
                # http://forum.developer.parrot.com/t/minimal-ble-commands-to-send-for-take-off/1686/2
                # Note this code snippet below more or less came from the python example posted to that forum (I adapted it to my interface)
                for c in s.getCharacteristics():
                    if self._get_byte_str_from_uuid(c.uuid, 3, 4) in \
                            ['fb0f', 'fb0e', 'fb1b', 'fb1c', 'fd22', 'fd23', 'fd24', 'fd52', 'fd53', 'fd54']:
                        self.handshake_characteristics[self._get_byte_str_from_uuid(c.uuid, 3, 4)] = c

            # check to see if all 8 characteristics were found
            allServicesFound = True
            for r_id in self.characteristic_receive_uuids.itervalues():
                if r_id not in self.receive_characteristics:
                    color_print("setting to false in receive on %s" % r_id)
                    allServicesFound = False

            for s_id in self.characteristic_send_uuids.itervalues():
                if s_id not in self.send_characteristics:
                    color_print("setting to false in send")
                    allServicesFound = False

            for f_id in self.characteristic_ftp_uuids.itervalues():
                if f_id not in self.ftp_characteristics:
                    color_print("setting to false in ftp")
                    allServicesFound = False

            # and ensure all handshake characteristics were found
            if len(self.handshake_characteristics.keys()) != 10:
                color_print("setting to false in len")
                allServicesFound = False

        # do the magic handshake
        self._perform_handshake()

        # initialize the delegate to handle notifications
        self.drone.setDelegate(MamboDelegate(handle_map, self))

    def _perform_handshake(self):
        """
        Magic handshake
        Need to register for notifications and write 0100 to the right handles
        This is sort of magic (not in the docs!) but it shows up on the forum here
        http://forum.developer.parrot.com/t/minimal-ble-commands-to-send-for-take-off/1686/2

        :return: nothing
        """
        color_print("magic handshake to make the drone listen to our commands")

        # Note this code snippet below more or less came from the python example posted to that forum (I adapted it to my interface)
        for c in self.handshake_characteristics.itervalues():
            # for some reason bluepy characteristic handle is two lower than what I need...
            # Need to write 0x0100 to the characteristics value handle (which is 2 higher)
            self.drone.writeCharacteristic(c.handle + 2, struct.pack("<BB", 1, 0))

    def disconnect(self):
        """
        Disconnect the BLE connection.  Always call this at the end of your programs to
        cleanly disconnect.

        :return: void
        """
        self.drone.disconnect()

    def _get_byte_str_from_uuid(self, uuid, byte_start, byte_end):
        """
        Extract the specified byte string from the UUID btle object.  This is an ugly hack
        but it was necessary because of the way the UUID object is represented and the documentation
        on the byte strings from Parrot.  You give it the starting byte (counting from 1 since
        that is how their docs count) and the ending byte and it returns that as a string extracted
        from the UUID.  It is assumed it happens before the first - in the UUID.

        :param uuid: btle UUID object
        :param byte_start: starting byte (counting from 1)
        :param byte_end: ending byte (counting from 1)
        :return: string with the requested bytes (to be used as a key in the lookup tables for services)
        """
        uuid_str = format("%s" % uuid)
        idx_start = 2 * (byte_start - 1)
        idx_end = 2 * (byte_end)

        my_hex_str = uuid_str[idx_start:idx_end]
        return my_hex_str

    def _send_command_packet_ack(self, packet):
        """
        Sends the actual packet on the ack channel.  Internal function only.

        :param packet: packet constructed according to the command rules (variable size, constructed elsewhere)
        :return: True if the command was sent and False otherwise
        """
        try_num = 0
        self._set_command_received('SEND_WITH_ACK', False)
        while (try_num < self.max_packet_retries and not self.command_received['SEND_WITH_ACK']):
            self._debug_print("sending command packet on try %d" % try_num, 2)
            self._safe_ble_write(characteristic=self.send_characteristics['SEND_WITH_ACK'], packet=packet)
            #self.send_characteristics['SEND_WITH_ACK'].write(packet)
            try_num += 1
            self._debug_print("sleeping for a notification", 2)
            #notify = self.drone.waitForNotifications(1.0)
            self.smart_sleep(0.5)
            #self._debug_print("awake %s " % notify, 2)

        return self.command_received['SEND_WITH_ACK']



    def _send_noparam_command_packet_ack(self, command_tuple):
        """
        Send a command on the ack channel - where all commands except PCMD go, per
        http://forum.developer.parrot.com/t/ble-characteristics-of-minidrones/5912/2

        the id of the last command sent (for use in ack) is the send counter (which is incremented before sending)

        Ensures the packet was received or sends it again up to a maximum number of times.

        :param command_tuple: 3 tuple of the command bytes.  0 padded for 4th byte
        :return: True if the command was sent and False otherwise
        """
        self.characteristic_send_counter['SEND_WITH_ACK'] = (self.characteristic_send_counter['SEND_WITH_ACK'] + 1) % 256
        packet = struct.pack("<BBBBBB", self.data_types['DATA_WITH_ACK'], self.characteristic_send_counter['SEND_WITH_ACK'],
                             command_tuple[0], command_tuple[1], command_tuple[2], 0)
        return self._send_command_packet_ack(packet)



    def _send_enum_command_packet_ack(self, command_tuple, enum_value, usb_id=None):
        """
        Send a command on the ack channel with enum parameters as well (most likely a flip).
        All commands except PCMD go on the ack channel per
        http://forum.developer.parrot.com/t/ble-characteristics-of-minidrones/5912/2

        the id of the last command sent (for use in ack) is the send counter (which is incremented before sending)

        :param command_tuple: 3 tuple of the command bytes.  0 padded for 4th byte
        :param enum_value: the enum index
        :return: nothing
        """
        self.characteristic_send_counter['SEND_WITH_ACK'] = (self.characteristic_send_counter['SEND_WITH_ACK'] + 1) % 256
        if (usb_id is None):
            packet = struct.pack("<BBBBBBI", self.data_types['DATA_WITH_ACK'], self.characteristic_send_counter['SEND_WITH_ACK'],
                                 command_tuple[0], command_tuple[1], command_tuple[2], 0,
                                 enum_value)
        else:
            self._debug_print(self.data_types['DATA_WITH_ACK'], self.characteristic_send_counter['SEND_WITH_ACK'],
                              command_tuple[0], command_tuple[1], command_tuple[2], 0, usb_id, enum_value, 1)
            packet = struct.pack("<BBBBBBBI", self.data_types['DATA_WITH_ACK'], self.characteristic_send_counter['SEND_WITH_ACK'],
                                 command_tuple[0], command_tuple[1], command_tuple[2], 0,
                                 usb_id, enum_value)
        return self._send_command_packet_ack(packet)