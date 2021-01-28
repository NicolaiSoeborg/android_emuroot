#!/usr/bin/env python3
import argparse
import logging
import threading
import time

# imports for emuroot
from pygdbmi.gdbcontroller import GdbController
from ppadb.client import Client as AdbClient


##################### ADB functions #############################
def kernel_version():
    """ This function returns the kernel version
    """
    client = AdbClient(host="127.0.0.1", port=5037)
    device = client.device(options.device)
    if device == None:
        logging.warning(f'Device name {options.device} invalid. Check "adb devices" to get valid ones')
        raise Exception('Device name invalid. Check "adb devices" to get valid ones')
    result = device.shell("uname -r")
    logging.debug(f" kernel_version() : {result}")
    result = result.split(".")
    ver = result[0] + "." + result[1]
    ver = float(ver)

    offset_selinux = []
    # case kernel version is <= 3.10
    if ver <= 3.10:
        offset_to_comm = 0x288
        offset_to_parent = 0xe0
        offset_selinux.append(0xC0A77548)
        offset_selinux.append(0xC0A7754C)
        offset_selinux.append(0xC0A77550)
        ps_cmd = "ps"

    # case kernel version is > 3.10 et <=3.18
    elif ver > 3.10 and ver <= 3.18:
        offset_to_comm = 0x444
        offset_to_parent = 0xe0
        offset_selinux.append(0xC0C4F288)
        offset_selinux.append(0xC0C4F28C)
        offset_selinux.append(0xC0C4F280)
        ps_cmd = "ps -A"

    else:
        logging.warning(f"Sorry. Android kernel version {ver} not supported yet")
        raise NotImplementedError("Sorry. Android kernel version not supported yet")
    return ver, offset_to_comm, offset_to_parent, offset_selinux, ps_cmd


def check_process_is_running(process, pscmd, devicename):
    """ This function checks if a given process is running (with  adb shell 'ps' command)
    """
    client = AdbClient(host="127.0.0.1", port=5037)
    device = client.device(devicename)
    if device == None:
        logging.warning(f'Device name {device} invalid. Check "adb devices" to get valid ones')
        raise Exception('Device name invalid. Check "adb devices" to get valid ones')
    ps = device.shell(pscmd)
    if process in ps:
        logging.warning(f"[+] OK. {process} is running")
    else:
        logging.warning(f"[+] NOK. It seems like {process} is not running...")
        logging.warning("[+] Android_Emuroot stopped")
        exit(1)


def adb_stager_process(load, devicename):
    """ This method is in charge to launch load.sh in background
    The script copy /system/bin/sh to /data/local/tmp and attempt to change its owner to root  in a loop

    TODO: specify the device id
    """
    logging.info("[+] Launch the stager process")
    client = AdbClient(host="127.0.0.1", port=5037)
    device = client.device(devicename)

    device.shell(f"echo {load} > /data/local/tmp/load.sh")
    device.shell("chmod +x /data/local/tmp/load.sh")
    device.shell("ln -s /system/bin/sh /data/local/tmp/STAGER")

    # Launch STAGER shell

    device.shell("/data/local/tmp/STAGER /data/local/tmp/load.sh")


def stager_clean(devicename):
    """ This function cleans the file system by removing the stager binaries created in adb_stager_process
    """
    logging.info("[+] Clean the stager process")
    client = AdbClient(host="127.0.0.1", port=5037)
    device = client.device(devicename)

    device.shell("rm /data/local/tmp/load.sh /data/local/tmp/STAGER")


##################### GDB functions #############################

class GDB_stub_controller(object):
    def __init__(self, options):
        self.options = options
        self.internal_timeout = 1
        logging.info(" [+] Start the GDB controller and attach it to the remote target")
        self.gdb = GdbController(time_to_check_for_additional_output_sec=options.timeout)
        response = self.gdb.write("target remote :1234")
        for resp in response:
            if "Remote debugging" in resp.get("payload", ""):
                logging.info(" [+] GDB server reached. Continue")
                break
        else:
            logging.warning("GDB server not reachable. Did you start it?")
            self.stop()
            raise Exception("GDB server not reachable. Did you start it?")

    def stop(self):
        logging.info(" [+] Detach and stop GDB controller")
        self.gdb.exit()

    def write_mem(self, addr, val):
        logging.debug(" [+] gdb.write addr: %#x value : %#x" % (addr, val))
        self.gdb.write("set *(unsigned int*) (%#x) = %#x" % (addr, val), timeout_sec=self.internal_timeout)

    def read_mem(self, addr, rec=0):
        try:
            logging.debug(" [+] gdb.read addr [0x%x]: ... " % (addr))
            r = (
                self.gdb.write("x/xw %#x" % addr, timeout_sec=self.internal_timeout)[1]
                .get("payload")
                .split("\\t")[1]
                .replace("\\n", "")
            )
            logging.debug(" [+] gdb.read addr [0x%x]: %s " % (addr, r))
            r = int(r, 16)

            return r
        except (TypeError, ValueError, IndexError, AttributeError):
            if rec == 0:
                logging.warning("Inconsistente GDB response. (GDB timeout or bad format). New try.")
                self.read_mem(addr, rec=1)
            else:
                logging.warning("Inconsistente GDB response. (GDB timeout or bad format). Quit")
                self.stop()
                raise Exception("GDB timeout reached. Quit")

    def read_str(self, addr):
        r = (
            self.gdb.write("x/s %#x" % addr, timeout_sec=self.internal_timeout)[1]
            .get("payload")
            .split("\\t")[1]
            .replace("\\n", "")
        )
        logging.debug(" [+] gdb.read str [0x%x]: %s " % (addr, r))
        return r

    def find(self, name):
        response = self.gdb.write(
            f'find 0xc0000000, +0x40000000, "{name}"',
            read_response=True,
            raise_error_on_timeout=True,
            timeout_sec=options.timeout,
        )
        addresses = []
        # parse gdb response
        for m in response:
            if m.get("payload", "").startswith("0x"):
                val = int(m["payload"][:-2], 16)  # remove tailing "\\n"
                addresses.append(val)
        # return a list of addresses found.
        return addresses

    def disable_selinux(self):
        """ This function sets SELinux enforcement to permissive
        """
        logging.info("[+] Disable SELinux")
        logging.debug("[+] Offsets are: " +
                " - ".join(f'{offset:02X}' for offset in self.options.offset_selinux))

        self.write_mem(self.options.offset_selinux[0], 0)
        self.write_mem(self.options.offset_selinux[1], 0)
        self.write_mem(self.options.offset_selinux[2], 0)

    def set_full_capabilities(self, cred_addr):
        """ This function sets all capabilities of a task to 1
        """
        logging.info("[+] Set full capabilities")
        for ofs in [0x30, 0x34, 0x38, 0x3c, 0x40, 0x44]:
            self.write_mem(cred_addr + ofs, 0xffffffff)

    def set_root_ids(self, cred_addr, effective=True):
        """ This function sets all Linux IDs of a task to 0 (root user)
        @effective: if False, effective IDs are not modified 
        """
        logging.info("[+] Set root IDs")
        for ofs in [0x04, 0x08, 0x0c, 0x10, 0x1c, 0x20]: # uid, gid, suid,sgid, fsuid, fsgid
            self.write_mem(cred_addr + ofs, 0x00000000)
        if effective:
            self.write_mem(cred_addr + 0x14, 0x00000000)  # euid
            self.write_mem(cred_addr + 0x18, 0x00000000)  # egid
        else:
            logging.info("[+] Note: effective ID have not been changed")

    def get_process_task_struct(self, process):
        """ This function returns the task_struct addr for a given process name
        """
        logging.info(f" [+] Get address aligned whose process name is: {process}")
        logging.info(f" [+] This step can take a while (GDB timeout: {options.timeout:.2f}sec). Please wait...")
        addresses = self.find(process)

        candidates = []
        for addr in addresses:
            if addr % 16 == self.options.offset_to_comm % 16:
                candidates.append(addr)

        for c in candidates:
            magic_cred_ptr1 = self.read_mem(c - 8)
            magic_cred_ptr2 = self.read_mem(c - 4)
            if magic_cred_ptr1 == magic_cred_ptr2:
                magic_addr = c
                return magic_addr - self.options.offset_to_comm

        raise Exception("Could not find process task struct")

    def get_adbd_cred_struct(self, stager_addr):
        """ This function returns the cred_struct address of adbd process from a given stager process
        """
        logging.info("[+] Search adbd task struct in the process hierarchy")
        adbd_cred_ptr = ""
        cur = stager_addr
        while True:
            parent_struct_addr = self.read_mem(cur + self.options.offset_to_comm - self.options.offset_to_parent)
            parent_struct_name = self.read_str(parent_struct_addr + self.options.offset_to_comm)
            if str(parent_struct_name) == '"adbd"':
                adbd_cred_ptr = self.read_mem(parent_struct_addr + self.options.offset_to_comm - 4)
                break
            cur = parent_struct_addr
        return adbd_cred_ptr


##################### Emuroot options ###########################

def single_mode(options):
    """
    This function looks for the task struct and cred structure
    for a given process and patch its cred ID and capabilities
    @options: argparse namespace. Uses options.magic_name.
    """
    logging.info("[+] Entering single function")
    logging.info(f"[+] Check if '{options.magic_name}' is running ")

    # Check if the process is running
    check_process_is_running(options.magic_name, options.ps_cmd, options.device)

    # Get task struct address
    gdbsc = GDB_stub_controller(options)
    magic = gdbsc.get_process_task_struct(options.magic_name)
    logging.debug(f"[+] singel_mode(): process task struct of magic is {magic}")

    # Replace the shell creds with id 0x0, keys 0x0, capabilities 0xffffffff
    logging.debug("[+] single_mode(): Replace the process creds with id 0x0, keys 0x0, capabilities 0xffffffff")
    magic_cred_ptr = gdbsc.read_mem(magic + options.offset_to_comm - 8)
    logging.debug(f"[+] single_mode(): magic_cred_ptr is {magic_cred_ptr}")
    gdbsc.set_root_ids(magic_cred_ptr)
    gdbsc.set_full_capabilities(magic_cred_ptr)

    gdbsc.disable_selinux()
    gdbsc.stop()


def setuid_mode(options):
    """
    This function install a sh with suid root on the file system
    @options: argparse namespace. Uses options.path.
    """
    logging.info("[+] Rooting with Android Emuroot via a setuid binary...")

    script = """'#!/bin/bash
cp /system/bin/sh /data/local/tmp/{0}
while :; do
  sleep 5
  if chown root:root /data/local/tmp/{0}; then break; fi
done
mount -o suid,remount /data
chmod 4755 /data/local/tmp/{0}'""".format(options.filename)

    thread = threading.Thread(name="adb_stager", target=adb_stager_process, args=(script, options.device))
    thread.start()
    time.sleep(5)  # to be sure STAGER has been started

    check_process_is_running("STAGER", options.ps_cmd, options.device)
    gdbsc = GDB_stub_controller(options)
    magic = gdbsc.get_process_task_struct("STAGER")

    adbd_cred_ptr = gdbsc.get_adbd_cred_struct(magic)
    gdbsc.set_full_capabilities(adbd_cred_ptr)

    gdbsc.disable_selinux()

    magic_cred_ptr = gdbsc.read_mem(magic + options.offset_to_comm - 8)
    gdbsc.set_root_ids(magic_cred_ptr)
    gdbsc.set_full_capabilities(magic_cred_ptr)

    gdbsc.stop()
    stager_clean(options.device)


def adbd_mode(options):
    """
    This function elevates the privileges of adbd process
    @options: argparse namespace. Uses options.stealth.
    """
    logging.info("adbd mode is chosen")
    logging.debug("[+] Rooting with Android Emuroot via adbd...")

    script = """'#!/bin/bash
cp /system/bin/sh /data/local/tmp/probe
isRoot=0
while :; do
  sleep 5
  if chown root:root /data/local/tmp/probe; then break; fi
done
sleep 5
rm rm /data/local/tmp/probe'"""

    thread = threading.Thread(name="adb_stager", target=adb_stager_process, args=(script, options.device))
    thread.start()
    time.sleep(5)  # to be sure STAGER has been started

    check_process_is_running("STAGER", options.ps_cmd, options.device)
    gdbsc = GDB_stub_controller(options)
    magic = gdbsc.get_process_task_struct("STAGER")

    adbd_cred_ptr = gdbsc.get_adbd_cred_struct(magic)
    gdbsc.set_full_capabilities(adbd_cred_ptr)
    gdbsc.set_root_ids(adbd_cred_ptr, effective=not options.stealth)

    gdbsc.disable_selinux()

    magic_cred_ptr = gdbsc.read_mem(magic + options.offset_to_comm - 8)
    gdbsc.set_root_ids(magic_cred_ptr)
    gdbsc.set_full_capabilities(magic_cred_ptr)

    gdbsc.stop()
    stager_clean(options.device)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Usage:")

    parser.add_argument("-v", "--version", action="version", version='%(prog)s version is 1.0')
    parser.add_argument("-V", "--noverbose", action="count", default=0, help="detailed steps")
    parser.add_argument("-D", "--debug", action="count", default=0, help="for more debug messages")
    parser.add_argument("-t", "--timeout", help="set the GDB timeout value (in seconds)", default=60, type=int)
    parser.add_argument("-d", "--device", default="emulator-5554",
                        help='specify the device name (as printed by "adb device", example: emulator-5554)')

    subparsers = parser.add_subparsers(title="modes")

    parser_single = subparsers.add_parser("single", help="elevates privileges of a given process")
    parser_single.add_argument("--magic-name", required=True,
                               help="name of the process, that will be looked for in memory")
    parser_single.set_defaults(mode_function=single_mode)

    parser_adbd = subparsers.add_parser("adbd", help="elevates adbd privileges")
    parser_adbd.add_argument("--stealth", action="store_true",
                             help="try to make it less obvious that adbd has new privileges")
    parser_adbd.set_defaults(mode_function=adbd_mode)

    parser_setuid = subparsers.add_parser("setuid", help="creates a setuid shell launcher")
    parser_setuid.add_argument("--filename", required=True, help="filename of the setuid shell to create in /data/local/tmp")
    parser_setuid.set_defaults(mode_function=setuid_mode)

    # parse the arguments
    options = parser.parse_args()
    if not hasattr(options, "mode_function"):
        parser.error("Too few arguments")

    # set logging params
    # default logging level is INFO
    loglevel = logging.INFO
    if options.noverbose:
        loglevel = logging.WARNING
    if options.debug:
        loglevel = logging.DEBUG

    logging.basicConfig(
        level=loglevel,
        format="%(asctime)s %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # pin down android kernel version
    options.version, options.offset_to_comm, options.offset_to_parent , options.offset_selinux, options.ps_cmd = kernel_version()

    # run the selected mode
    options.mode_function(options)
