"""Screenshots of every installer page, driven through the real window against a simulated r1:
python -m haretic_installer.tests.shots <out-folder> [package.tar.xz]"""
import os
import sys
import tempfile

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
tmp = tempfile.mkdtemp(dir=os.environ.get('TMPDIR'))
os.environ['XDG_DATA_HOME'] = tmp

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from haretic_installer import gui, package, flow  # noqa: E402

out = sys.argv[1]
os.makedirs(out, exist_ok=True)
app = QApplication([])
w = gui.Window(demo=400e6)
w.show()
n = [0]


def shot(name):
    app.processEvents()
    n[0] += 1
    w.grab().save(os.path.join(out, f'{n[0]:02d}-{name}.png'))


def wait(cond, ms=2400000):
    import time
    t = time.time()
    while not cond():
        app.processEvents()
        time.sleep(0.02)
        if time.time() - t > ms / 1000:
            raise SystemExit('timed out at ' + str(n[0]))


# Confirm every question: the button that goes ahead, whatever order Qt lists them in
QMessageBox.exec = lambda self: setattr(self, '_clicked', next(
    (b for b in self.buttons() if self.buttonRole(b) in (QMessageBox.DestructiveRole, QMessageBox.AcceptRole,
                                                         QMessageBox.YesRole)), self.buttons()[0])) or 0
QMessageBox.clickedButton = lambda self: getattr(self, '_clicked', None)
w.asked_release = True
p = w.p_welcome
w.releases = [{'version': 'haretic-20261105-beta', 'name': package.file_name('haretic-20261105-beta', g, r),
               'size': 905000000 + 410000000 * g + 12000000 * r, 'prerelease': True, 'url': '', 'sha256': None,
               'notes': '', 'gapps': g, 'root': r} for g in (False, True) for r in (False, True)]
p.online.setText('Download haretic-20261105-beta (pre-release)')
for c in (p.online, p.gapps, p.root):
    c.setEnabled(True)
p.online.setChecked(True)
p.gapps.setChecked(True)
w.variant_text()
shot('welcome')
w.toggle_log()
shot('welcome-details')
w.toggle_log()
# a package file on disk
if len(sys.argv) > 2:
    w.pkg = package.Package(sys.argv[2])
    w.pkg_path = sys.argv[2]
    p.local.setChecked(True)
    w.welcome_ready()
w.verified = False
w.start_install()
wait(lambda: w.p_download.progress.bar.value() > 300 or w.pages.currentWidget() is not w.p_download)
shot('download-checking')
wait(lambda: w.pages.currentWidget() is w.p_connect)
shot('connect-waiting')
w.p_connect.help.show()
shot('connect-help')
wait(lambda: w.pages.currentWidget() is w.p_check)
shot('check')
w.p_check.go.click()
shot('backup')
w.p_backup.go.click()
wait(lambda: w.p_backup.progress.bar.value() > 150)
shot('backup-running')
wait(lambda: w.pages.currentWidget() is not w.p_backup)
wait(lambda: w.pages.currentWidget() is w.p_install or w.pages.currentWidget() is w.p_done)
shot('unlock-or-install')
wait(lambda: w.p_install.progress.bar.value() > 400)
shot('install-running')
wait(lambda: w.pages.currentWidget() is w.p_done)
shot('done')
# an error card: a cable pulled during the install
w.show_page(w.p_install, 5)
w.step_safe = False
from haretic_installer.device import DeviceError  # noqa: E402
w.failed(DeviceError('Writing to the r1 failed (USB timeout). Check the cable.'), w.p_install, 'Installing stopped')
shot('install-error')
w.p_install.error.hide()
w.failed(flow.r1.NotAnR1('Its "lk_a" partition is not where an r1 has it.'), w.p_check)
w.show_page(w.p_check, 2)
shot('not-an-r1')
# the same r1 again, now running haretic: the update choice, and no backup unless asked
w.session.info.lk['a'], w.session.info.slot = 'haretic', 'a'
w.show_page(w.p_check, 2)
w.show_check()
shot('check-haretic')
w.p_check.go.click()
shot('backup-haretic')
# a newer haretic on the r1: no update keeping data, until five presses on the reason
w.session.info.installed = {'version': 'haretic-20261201-nightly', 'aosp': False, 'gapps': True, 'root': True}
w.show_check()
shot('check-downgrade')
for _ in range(5):
    w.why_pressed(None)
shot('check-downgrade-allowed')
w.allow_downgrade = False
w.session.info.installed = {'version': 'haretic-20261101-nightly', 'aosp': True, 'gapps': False, 'root': False}
w.show_check()
shot('check-aosp')
w.session.info.installed = {'version': 'haretic-20261101-nightly', 'aosp': False, 'gapps': True, 'root': True}
w.show_check()
shot('check-variant')
w.session.info.installed = None
w.show_restore()
shot('restore')
w.show_setup('usbdk')
shot('setup-windows')
w.show_setup('udev')
shot('setup-linux')
print('shots in', out)
