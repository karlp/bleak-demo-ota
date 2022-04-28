#!/usr/bin/env python3
"""
blk-reconnect: test connecting and reconnecting to a device that is rebooting. (like silabs ota apploader!)

See also:
    UG489 - Gecko bootloader user guide (gsdk 4+)
    AN1086 - Gecko bootloader Bluetooth
"""
import argparse
import asyncio
import logging
import struct
import time
import uuid

import bleak
import bleak.backends.device
import bleak.backends.scanner
import bleak.backends.service
import bleak.uuids

class SL_OTA_UUIDS:
    SVC = uuid.UUID("1d14d6ee-fd63-4fa1-bfa4-8f47b42119f0")
    CCONTROL = uuid.UUID("F7BF3564-FB6D-4E53-88A4-5E37E0326063")
    CDATA =    uuid.UUID("984227F3-34FC-4045-A5D0-2C581F81A153")
    CAPPLOADER_VERSION = uuid.UUID("4F4A2368-8CCA-451E-BFFF-CF0E2EE23E9F")
    COTA_VERSION = uuid.UUID("4CC07BCF-0868-4B32-9DAD-BA4CC41E5316")
    CGECKO_BL_VERSION = uuid.UUID("25F05C0A-E917-46E9-B2A5-AA2BE1245AFE")
    CAPP_VERSION = uuid.UUID("0D77CC11-4AC1-49F2-BFA9-CD96AC7A92F8")


class SL_OTA_COMMANDS:
    START = [0]
    FINISH = [3]
    DISCONNECT = [4]  # This will also reboot, which is fine...



logging.basicConfig(format='%(asctime)s [%(levelname)s] %(name)s %(message)s', level=logging.DEBUG)
#logging.getLogger("bleak").setLevel(logging.INFO)


def get_args():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("-d", "--device", help="Device to connect to",
                        #required=True,
                        default="D0:CF:5E:D9:12:3D")
    parser.add_argument("--reliable", help="Use acknowledged writes, slower, but more reliable", default=False, action="store_true")
    #parser.add_argument("-f", "--file", help="OTA upgrade file", type=argparse.FileType('rb'), required=True)
    options = parser.parse_args()
    return options


async def runthing(opts):
    def my_filter(dev: bleak.backends.device.BLEDevice, adv: bleak.backends.scanner.AdvertisementData):
        if dev.address == opts.device:
            logging.info("Matched by address!")
            return True
        if dev.name == opts.device:
            logging.info("Matched by name!")
            return True
        logging.debug("ignoring: %s (%s)", dev.address, dev.name)
        return False

    dev = await bleak.BleakScanner.find_device_by_filter(my_filter)
    if not dev:
        raise bleak.BleakError(f"Couldn't find a matching device for: {opts.device}")

    disconn_event = asyncio.Event()
    def handle_disconnect1(d: bleak.BleakClient):
        logging.info("Lost connection with: %s (this is expected)", d)
        # semantically we want to set a flag here to wait on...
        d._device_path = None # hack from https://github.com/hbldh/bleak/issues/713
        disconn_event.set()

    def is_dfu_mode(svcs: bleak.backends.service.BleakGATTServiceCollection):
        """
        Given a list of services, check for the presence of the OTA data characteristic,
        indicating that we're in DFU mode.
        :param svcs:
        :return:
        """
        for s in svcs:
            if s.uuid == str(SL_OTA_UUIDS.SVC):
                for c in s.characteristics:
                    if c.uuid == str(SL_OTA_UUIDS.CDATA):
                        return True
        return False

    def get_control_handle(svcs: bleak.backends.service.BleakGATTServiceCollection, expected_ota: bool):
        """
        return the correct control char, depending on whether we believe we should be in
        dfu mode or normal mode.
        :param svcs:
        :return:
        """
        plausible = None
        for s in svcs:
            real_ota_mode = False
            if s.uuid == str(SL_OTA_UUIDS.SVC):
                for c in s.characteristics:
                    if c.uuid == str(SL_OTA_UUIDS.CCONTROL):
                        plausible = c
                    if c.uuid == str(SL_OTA_UUIDS.CDATA):
                        real_ota_mode = True
            if plausible and real_ota_mode == expected_ota:
                return plausible
            # else, continue looking at next service..
            plausible = None
        return None

    async with bleak.BleakClient(dev, disconnected_callback=handle_disconnect1) as client:

        # Attempt to just go round rebooting between the two modes ~forever to test robust handling...
        while True:
            svcs = await client.get_services()
            # Find any matching services. We _expect_ one, and only one, but we get cached
            # with different handle ids...
            sl_ota = [s for s in svcs if s.uuid == str(SL_OTA_UUIDS.SVC)]
            for s in sl_ota:
                [print(f"SL OTA handle: {s.handle} char: {c.handle} uuid: {c.uuid}") for c in s.characteristics]
            if len(sl_ota) < 1:
                raise bleak.BleakError(f"Device doesn't appear to have the OTA service?")

            # We only _desire_ that our list of services is what is currently exposed by the device, but apparently
            # we get cached services with different handle ids...
            workaround_duplicate_charids = True
            if is_dfu_mode(svcs):
                print(f"Requesting OTA -> APP")
                if workaround_duplicate_charids:
                    handle = get_control_handle(svcs, True)
                    answer = await client.write_gatt_char(handle, SL_OTA_COMMANDS.DISCONNECT, True)
                else:
                    answer = await client.write_gatt_char(SL_OTA_UUIDS.CCONTROL, SL_OTA_COMMANDS.DISCONNECT, True)
                if answer:
                    pass # no response with bleak write anyway
                else:
                    print(f"no reply to control command, already disconnected?")
            else:
                print(f"Requesting APP -> OTA")
                if workaround_duplicate_charids:
                    handle = get_control_handle(svcs, False)
                    answer = await client.write_gatt_char(handle, SL_OTA_COMMANDS.START, True)
                else:
                    answer = await client.write_gatt_char(SL_OTA_UUIDS.CCONTROL, SL_OTA_COMMANDS.DISCONNECT, True)
                if answer:
                    pass # no response with bleak write anyway
                else:
                    print(f"wrote command, but got no reply... (already disconnected?)")

            # here, we have almost definitely been disconnected...
            print(f"ok, waiting on the disconnected event....")
            await disconn_event.wait()
            # So, try and reconnect!
            while not client.is_connected:
                print(f"Attempting to reconnect...")
                await asyncio.sleep(1)
                await client.connect()
            print(f"Ok, connected, going around the loop again shortly.")
            await asyncio.sleep(5)



async def domain(opts):
    bleak.uuids.register_uuids({
        str(SL_OTA_UUIDS.SVC): "SiLabs OTA service",
        str(SL_OTA_UUIDS.CCONTROL): "SL OTA Control",
        str(SL_OTA_UUIDS.CDATA): "SL OTA Data",
        str(SL_OTA_UUIDS.CAPPLOADER_VERSION): "SL OTA AppLoader Version",
        str(SL_OTA_UUIDS.CGECKO_BL_VERSION): "SL OTA Gecko BL Version",
        str(SL_OTA_UUIDS.COTA_VERSION): "SL OTA Version",
        str(SL_OTA_UUIDS.CAPP_VERSION): "SL OTA App version",
    })
    c_task = runthing(opts)
    await asyncio.gather(c_task)
    logging.info("main done")

if __name__ == "__main__":
    a = get_args()
    asyncio.run(domain(a))

    logging.warning("run off the end of main")
