"""The r1 over USB, through mtkclient's download agent, and a simulated r1 for tests and demos.

Both offer: identity, read(area, offset, length), write(offset, data) on the user area, reboot()
and close(). Reads and writes are bounded pieces so callers can show progress and stop between
them."""
import logging
import os
import struct
import sys
import threading
import time

from . import r1

PIECE = 8 << 20
SECTOR = 512
# TOPRGU WDT_SWRST: the r1 resets into a normal boot. The DA's own shutdown hangs or powers the
# r1 off instead.
WDT_SWRST, WDT_KEY = 0x10007014, 0x1209


class Cancelled(Exception):
    pass


class DeviceError(Exception):
    """A USB or download-agent failure, worded for the user."""


def usb_scan(backend=None):
    """The r1 USB functions plugged in now: [(pid, name)]."""
    import usb.core
    found = []
    for dev in usb.core.find(find_all=True, idVendor=r1.VENDOR, backend=backend) or ():
        if dev.idProduct in r1.USB_IDS:
            found.append((dev.idProduct, r1.USB_IDS[dev.idProduct]))
    return found


class _Log(logging.Handler):
    def __init__(self, sink):
        super().__init__(logging.DEBUG)
        self.sink = sink

    def emit(self, record):
        try:
            self.sink(self.format(record))
        except Exception:
            pass


class _Sectors:
    """Reads and writes of any span over storage that only moves whole sectors: a read
    widens to the sectors around it, a partial write reads them, changes its bytes and
    writes them back."""

    def read(self, area, offset, length):
        start = offset - offset % SECTOR
        end = -(-(offset + length) // SECTOR) * SECTOR
        data = self._read(area, start, end - start)
        return data[offset - start:offset - start + length]

    def write(self, offset, data):
        start = offset - offset % SECTOR
        end = -(-(offset + len(data)) // SECTOR) * SECTOR
        if start != offset or end != offset + len(data):
            block = bytearray(self._read('user', start, end - start))
            block[offset - start:offset - start + len(data)] = data
            data = bytes(block)
        self._write(start, data)


class MtkDevice(_Sectors):
    """A real r1 in preloader or boot ROM mode, driven by mtkclient's XFlash download agent."""

    def __init__(self, workdir, log=lambda line: None):
        self.workdir = workdir
        self.log = log
        self.mtk = None
        self.identity = None
        handler = _Log(log)
        handler.setFormatter(logging.Formatter('%(name)s: %(message)s'))
        logging.getLogger().addHandler(handler)
        logging.getLogger().setLevel(logging.INFO)

    def connect(self, status, cancelled):
        """Wait for the r1 and start the download agent on it. status(text) reports what happens."""
        from mtkclient.config.mtk_config import MtkConfig
        from mtkclient.Library.mtk_class import Mtk
        from mtkclient.Library.DA.mtk_da_handler import DaHandler
        from mtkclient.Library.Partitions.gpt import GptSettings
        os.makedirs(self.workdir, exist_ok=True)
        config = MtkConfig(loglevel=logging.INFO, gui=None, guiprogress=None)
        config.hwparam_path = self.workdir
        config.reconnect = False
        config.write_preloader_to_file = False
        # mtkclient's seccfg unlock reads the GPT with these; zeros, as its own command line
        # defaults to, mean the sizes in the GPT header
        config.gpt_settings = GptSettings(0, 0, 0)
        mtk = Mtk(config=config, loglevel=logging.INFO)
        cdc = mtk.port.cdc
        connect = cdc.connect
        seen = [False]

        # mtkclient polls for the r1 for a long time; make each poll a chance to stop
        def polled(*a, **k):
            if cancelled():
                raise Cancelled()
            ok = connect(*a, **k)
            if ok and not seen[0]:
                seen[0] = True
                status('found')
            return ok
        cdc.connect = polled
        old = os.getcwd()
        try:
            os.chdir(self.workdir)  # mtkclient writes its state files into the working directory
            handler = DaHandler(mtk, logging.INFO)
            status('waiting')
            mtk = handler.connect(mtk, self.workdir)
            if mtk is None:
                raise DeviceError('The r1 answered but did not finish connecting. Unplug it, '
                                  'turn it off and try again.')
            if mtk.config.hwcode != r1.HW_CODE:
                raise r1.NotAnR1(f'This device has a different chip ({mtk.config.hwcode:#x}); '
                                 'the r1 is an MT6765.')
            status('agent')
            try:
                mtk = handler.configure_da(mtk)
            except SystemExit:
                mtk = None
            if mtk is None:
                raise DeviceError('The r1 did not accept the download agent. Unplug it, turn it '
                                  'off and try again.')
        finally:
            cdc.connect = connect
            os.chdir(old)
        self.mtk = mtk
        emmc = mtk.daloader.da.get_emmc_info(display=False)
        if emmc is None or emmc.block_size != 512:
            raise DeviceError('The r1 did not report its storage.')
        self.identity = {'hwcode': mtk.config.hwcode, 'cid': bytes(emmc.cid).hex(),
                         'user_size': emmc.user_size, 'boot1_size': emmc.boot1_size,
                         'boot2_size': emmc.boot2_size}
        return self.identity

    def _read(self, area, offset, length):
        try:
            data = self.mtk.daloader.readflash(addr=offset, length=length, filename='',
                                               parttype=area, display=False)
        except Exception as e:
            raise DeviceError(f'Reading the r1 failed ({e}). Check the cable.') from e
        if isinstance(data, tuple):
            data = data[0]
        if not isinstance(data, (bytes, bytearray)) or len(data) != length:
            raise DeviceError('Reading the r1 failed: it returned too little. Check the cable.')
        return bytes(data)

    def _write(self, offset, data):
        try:
            ok = self.mtk.daloader.writeflash(addr=offset, length=len(data), filename='',
                                              wdata=data, parttype='user', display=False)
        except Exception as e:
            raise DeviceError(f'Writing to the r1 failed ({e}). Check the cable.') from e
        if not ok:
            raise DeviceError('Writing to the r1 failed. Check the cable.')

    def unlock(self):
        """mtkclient's seccfg unlock; True once written."""
        ok, message = self.mtk.daloader.seccfg('unlock')
        self.log('seccfg: ' + str(message))
        return ok, message

    def seccfg_check(self, data):
        """Whether mtkclient can unlock this r1: its seccfg's hash checks with the r1's hardware
        key, and the unlocked seccfg it would write checks again. No write. Returns the
        hash type found, or None."""
        from mtkclient.Library.Hardware.seccfg import SecCfgV4
        xft = self.mtk.daloader.da.xft
        hwc = xft.cryptosetup()
        sc = SecCfgV4(hwc, self.mtk, xft.custom_sej_hw)
        if not sc.parse(data):
            return None
        sc.lock_state = 1  # create() refuses an unlocked one; the check wants its output anyway
        ok, out = sc.create(lockflag='unlock')
        again = SecCfgV4(hwc, self.mtk, xft.custom_sej_hw)
        if not ok or not again.parse(bytes(out)) or again.lock_state != 3:
            return None
        return sc.hwtype

    def reboot(self):
        try:
            self.mtk.daloader.da.xft.custom_writeregister(WDT_SWRST, WDT_KEY)
        except Exception as e:  # the reset can cut the link before the agent answers
            self.log(f'reset: {e!r}')
        self.close()

    def close(self):
        if self.mtk is not None:
            try:
                self.mtk.port.close(reset=False)
            except Exception:
                pass
            self.mtk = None


class SimDevice(_Sectors):
    """A simulated r1: its storage in sparse files. Built from the real r1 GPT and stock-like
    contents; for tests, demos and screenshots. Speed and failures can be injected."""

    def __init__(self, folder, rate=None, slot='a', locked=True, haretic=False, fail_at=None,
                 log=lambda line: None):
        self.folder = folder
        self.rate = rate
        self.fail_at = fail_at
        self.log = log
        self.identity = None
        self.lock = threading.Lock()
        os.makedirs(folder, exist_ok=True)
        self.files = {a: os.path.join(folder, a + '.img') for a in ('user', 'boot1', 'boot2')}
        self.size = {'user': 0x1d1ec00000, 'boot1': r1.BOOT_AREA, 'boot2': r1.BOOT_AREA}
        if not os.path.exists(self.files['user']):
            self._make(slot, locked, haretic)
        self.written = 0

    def _make(self, slot, locked, haretic):
        from . import simdata
        for area, path in self.files.items():
            with open(path, 'wb') as f:
                f.truncate(self.size[area])
        with open(self.files['user'], 'r+b') as f:
            for offset, data in simdata.contents(slot, locked, haretic):
                f.seek(offset)
                f.write(data)
        for area in ('boot1', 'boot2'):
            with open(self.files[area], 'r+b') as f:
                f.write(b'MMM\x01' + area.encode() * 64)

    def connect(self, status, cancelled):
        status('waiting')
        for _ in range(10):
            if cancelled():
                raise Cancelled()
            time.sleep(0.1 if self.rate else 0)
        status('found')
        status('agent')
        self.identity = {'hwcode': r1.HW_CODE, 'cid': '15010044563644414202a117d39c0000',
                         'user_size': self.size['user'], 'boot1_size': r1.BOOT_AREA,
                         'boot2_size': r1.BOOT_AREA}
        return self.identity

    def _pace(self, n):
        self.written += n
        if self.fail_at is not None and self.written > self.fail_at:
            self.fail_at = None
            raise DeviceError('Reading the r1 failed (simulated cable pull). Check the cable.')
        if self.rate:
            time.sleep(n / self.rate)

    def _read(self, area, offset, length):
        # As strict as the r1's download agent: whole sectors only
        assert offset % SECTOR == 0 and length % SECTOR == 0, (offset, length)
        self._pace(length)
        with open(self.files[area], 'rb') as f:
            f.seek(offset)
            data = f.read(length)
        return data + bytes(length - len(data))

    def _write(self, offset, data):
        assert offset % SECTOR == 0 and len(data) % SECTOR == 0, (offset, len(data))
        self._pace(len(data))
        with open(self.files['user'], 'r+b') as f:
            f.seek(offset)
            f.write(data)

    def unlock(self):
        seccfg = dict((n, o) for n, o, _ in r1.LAYOUT)['seccfg']
        data = bytearray(self.read('user', seccfg, 64))
        struct.pack_into('<I', data, 12, 3)
        self.write(seccfg, bytes(data))
        return True, 'Successfully wrote seccfg.'

    def seccfg_check(self, data):
        return 'SW' if data[:4] == b'MMMM' else None

    def reboot(self):
        self.log('simulated reboot')

    def close(self):
        pass


def frozen_stdio(path):
    """A windowed build has no console: mtkclient prints, so give it somewhere to print."""
    if sys.stdout is None or sys.stderr is None:
        stream = open(path, 'a', buffering=1, encoding='utf-8', errors='replace')
        sys.stdout = sys.stdout or stream
        sys.stderr = sys.stderr or stream
