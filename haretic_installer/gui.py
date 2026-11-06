"""The guided installer window: seven steps from download to the r1's first start, each explained,
with progress, time left, and errors that say what happened, whether the r1 is safe, and how to
go on."""
import datetime
import os
import sys
import time
import traceback

from PySide6.QtCore import QPointF, QRectF, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QIcon, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QFileDialog, QFrame, QHBoxLayout,
                               QLabel, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
                               QRadioButton, QScrollArea, QStackedWidget, QStyle, QStyleOptionButton,
                               QVBoxLayout, QWidget)

from . import drivers, flow, package, paths, r1
from .device import Cancelled, DeviceError, MtkDevice, SimDevice

ORANGE = '#EC6A37'
STEPS = ('Download', 'Connect', 'Check', 'Back up', 'Unlock', 'Install', 'Start')
RESTORE_STEPS = ('Restore file', 'Connect', 'Check', 'Restore', 'Start')

STYLE = f'''
QWidget {{ background: #141414; color: #e8e8e8; font-size: 14px; }}
QLabel#title {{ font-size: 24px; font-weight: 600; color: #ffffff; }}
QLabel#lead {{ color: #c8c8c8; }}
QLabel#small, QLabel#hint {{ color: #9a9a9a; font-size: 13px; }}
QLabel#status {{ font-size: 15px; color: #ffffff; }}
QFrame#card {{ background: #1e1e1e; border: 1px solid #2c2c2c; border-radius: 10px; }}
QFrame#card QLabel, QFrame#card QRadioButton, QFrame#card QCheckBox {{ background: transparent; }}
QScrollArea, QScrollArea > QWidget > QWidget {{ background: transparent; }}
QFrame#warn {{ background: #2a1d16; border: 1px solid #6b3a22; border-radius: 10px; }}
QFrame#warn QLabel {{ background: transparent; color: #f3d2c2; }}
QFrame#error {{ background: #2a1515; border: 1px solid #7a2c2c; border-radius: 10px; }}
QFrame#error QLabel {{ background: transparent; }}
QFrame#error[calm="true"] {{ background: #1e1e1e; border-color: #3a3a3a; }}
QFrame#error QLabel#status {{ font-weight: 600; }}
QFrame#side {{ background: #101010; border-right: 1px solid #242424; }}
QFrame#side QLabel {{ background: transparent; }}
QPushButton {{ background: #262626; border: 1px solid #3a3a3a; border-radius: 8px; padding: 9px 18px; }}
QPushButton:hover {{ background: #303030; }}
QPushButton:focus {{ border-color: {ORANGE}; }}
QPushButton:disabled {{ color: #666; border-color: #2a2a2a; }}
QPushButton#primary {{ background: {ORANGE}; color: #141414; border: 1px solid {ORANGE}; font-weight: 600; }}
QPushButton#primary:hover {{ background: #f27c4d; border-color: #f27c4d; }}
QPushButton#primary:focus, QPushButton#danger:focus {{ border-color: #ffffff; }}
QPushButton#primary:disabled {{ background: #4a2f24; border-color: #4a2f24; color: #a08578; }}
QPushButton#danger {{ background: #b3402a; color: #ffffff; border: 1px solid #b3402a; font-weight: 600; }}
QPushButton#danger:hover {{ background: #c44a33; border-color: #c44a33; }}
QPushButton#link {{ background: transparent; border: none; color: {ORANGE}; padding: 4px 0; text-align: left; }}
QPushButton#link:hover, QPushButton#link:focus {{ text-decoration: underline; }}
QProgressBar {{ background: #262626; border: none; border-radius: 6px; height: 12px; text-align: center; color: transparent; }}
QProgressBar::chunk {{ background: {ORANGE}; border-radius: 6px; }}
QPlainTextEdit {{ background: #0c0c0c; border: 1px solid #262626; border-radius: 8px; font-family: monospace; font-size: 12px; color: #b8b8b8; }}
QRadioButton, QCheckBox {{ spacing: 10px; padding: 4px 0; }}
QRadioButton::indicator, QCheckBox::indicator {{ width: 14px; height: 14px; border: 2px solid #6a6a6a; background: #141414; }}
QRadioButton::indicator {{ border-radius: 9px; }}
QCheckBox::indicator {{ border-radius: 4px; }}
QRadioButton::indicator:hover, QCheckBox::indicator:hover, QRadioButton::indicator:focus,
QCheckBox::indicator:focus {{ border-color: {ORANGE}; }}
QRadioButton::indicator:checked {{ border-color: {ORANGE}; background: qradialgradient(cx:0.5, cy:0.5, radius:0.5, fx:0.5, fy:0.5, stop:0 {ORANGE}, stop:0.45 {ORANGE}, stop:0.55 #141414, stop:1 #141414); }}
QCheckBox::indicator:checked {{ border-color: {ORANGE}; background: {ORANGE}; }}
QRadioButton::indicator:disabled, QCheckBox::indicator:disabled {{ border-color: #333; }}
QRadioButton:disabled, QCheckBox:disabled {{ color: #666; }}
QLabel#qt_msgbox_informativelabel {{ min-width: 420px; }}
QScrollBar {{ background: transparent; border: none; }}
QScrollBar:vertical {{ width: 10px; }}
QScrollBar:horizontal {{ height: 10px; }}
QScrollBar::handle {{ background: #3a3a3a; border-radius: 3px; margin: 2px; }}
QScrollBar::handle:vertical {{ min-height: 32px; }}
QScrollBar::handle:horizontal {{ min-width: 32px; }}
QScrollBar::handle:hover {{ background: #555555; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; border: none; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QToolTip {{ background: #262626; color: #e8e8e8; border: 1px solid #3a3a3a; padding: 6px; }}
'''


def size(n):
    return flow.fmt_size(n)


def minutes(seconds):
    if seconds is None:
        return ''
    if seconds < 60:
        return 'less than a minute left'
    m = round(seconds / 60)
    return f'about {m} min left' if m < 60 else f'about {m // 60} h {m % 60:02d} min left'


# -- background work

class Job(QThread, flow.Reporter):
    """Runs one step off the window's thread; reports through signals."""
    status_changed = Signal(str)
    progressed = Signal(object, object)
    logged = Signal(str)
    finished_ok = Signal(object)
    failed = Signal(object)

    def __init__(self, fn, parent=None):
        QThread.__init__(self, parent)
        self.fn = fn
        self.stop = False

    def status(self, text):
        self.status_changed.emit(text)

    def progress(self, done, total):
        self.progressed.emit(done, total)

    def log(self, line):
        self.logged.emit(line)

    def cancelled(self):
        return self.stop

    def run(self):
        try:
            self.finished_ok.emit(self.fn(self))
        except BaseException as e:  # noqa: B902 - every failure is shown, none crashes the window
            self.logged.emit(''.join(traceback.format_exception(e)))
            self.failed.emit(e)


# -- drawing

class R1Picture(QWidget):
    """The r1 cabled to a computer, the cable pulsing while the installer waits for it."""

    def __init__(self):
        super().__init__()
        self.setMinimumSize(260, 150)
        self.phase = 0.0
        self.state = 'off'
        self.timer = QTimer(self, interval=50, timeout=self.tick)

    def set_state(self, state):
        self.state = state
        if state == 'waiting':
            self.timer.start()
        else:
            self.timer.stop()
        self.update()

    def tick(self):
        self.phase = (self.phase + 0.02) % 1.0
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        s = min(w / 260, h / 150)
        p.translate((w - 260 * s) / 2, (h - 150 * s) / 2)
        p.scale(s, s)
        # the r1: orange body, black screen on the left, the wheel on the right
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(ORANGE))
        p.drawRoundedRect(QRectF(10, 20, 110, 110), 18, 18)
        p.setBrush(QColor('#0a0a0a'))
        p.drawRoundedRect(QRectF(20, 32, 72, 86), 6, 6)
        if self.state == 'connected':
            p.setBrush(QColor(ORANGE))
            p.drawEllipse(QPointF(56, 75), 6, 6)
        p.setBrush(QColor('#3a2418'))
        p.drawRoundedRect(QRectF(100, 66, 12, 34), 6, 6)
        # the computer
        p.setBrush(QColor('#2c2c2c'))
        p.drawRoundedRect(QRectF(170, 38, 80, 56), 5, 5)
        p.setBrush(QColor('#0f0f0f'))
        p.drawRect(QRectF(176, 44, 68, 44))
        p.setBrush(QColor('#2c2c2c'))
        p.drawRoundedRect(QRectF(160, 96, 100, 8), 3, 3)
        # the cable, from the r1's bottom edge to the computer
        path = QPainterPath(QPointF(65, 130))
        path.cubicTo(QPointF(65, 148), QPointF(150, 146), QPointF(200, 104))
        pen = QPen(QColor('#555' if self.state == 'off' else ORANGE), 3)
        pen.setCapStyle(Qt.RoundCap)
        if self.state == 'waiting':
            pen.setDashPattern([3, 4])
            pen.setDashOffset(-self.phase * 70)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)


class Stepper(QWidget):
    """The steps down the left side: done, current, to come, failed."""

    def __init__(self, steps):
        super().__init__()
        self.steps = list(steps)
        self.current = 0
        self.failed = False
        self.setMinimumWidth(170)
        self.setAccessibleName('Steps')
        self.describe()

    def set_steps(self, steps):
        self.steps = list(steps)
        self.current = 0
        self.describe()
        self.update()

    def set(self, index, failed=False):
        self.current, self.failed = index, failed
        self.describe()
        self.update()

    def describe(self):
        n = len(self.steps)
        self.setAccessibleDescription(
            'Not started' if self.current < 0 else 'All steps done' if self.current >= n else
            f'Step {self.current + 1} of {n}: {self.steps[self.current]}' + (', stopped' if self.failed else ''))

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        f = QFont(self.font())
        f.setPixelSize(14)
        p.setFont(f)
        y0, gap = 28, 46
        for i, name in enumerate(self.steps):
            y = y0 + i * gap
            if i < len(self.steps) - 1:
                p.setPen(QPen(QColor(ORANGE if i < self.current else '#3a3a3a'), 2))
                p.drawLine(QPointF(30, y + 12), QPointF(30, y + gap - 12))
            c = QPointF(30, y)
            if i < self.current:
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(ORANGE))
                p.drawEllipse(c, 10, 10)
                p.setPen(QPen(QColor('#141414'), 2.2))
                p.drawPolyline([QPointF(25, y), QPointF(29, y + 4), QPointF(35.5, y - 4)])
            elif i == self.current:
                colour = '#d9534f' if self.failed else ORANGE
                p.setPen(QPen(QColor(colour), 2))
                p.setBrush(QColor('#141414'))
                p.drawEllipse(c, 10, 10)
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(colour))
                p.drawEllipse(c, 4.5, 4.5)
            else:
                p.setPen(QPen(QColor('#4a4a4a'), 2))
                p.setBrush(Qt.NoBrush)
                p.drawEllipse(c, 10, 10)
            p.setPen(QColor('#ffffff' if i == self.current else '#bdbdbd' if i < self.current else '#8a8a8a'))
            f.setBold(i == self.current)
            p.setFont(f)
            p.drawText(QRectF(52, y - 12, 200, 24), Qt.AlignVCenter, name)


# -- building blocks

def label(text, name=None, wrap=True):
    w = QLabel(text)
    w.setWordWrap(wrap)
    w.setTextFormat(Qt.RichText)
    w.setOpenExternalLinks(True)
    if name:
        w.setObjectName(name)
    return w


def bullets(items):
    """A list with orange dashes and hanging lines."""
    return '<table cellspacing="0" cellpadding="1">' + ''.join(
        f'<tr><td valign="top" style="padding-right:6px"><span style="color:{ORANGE};font-weight:600">-</span></td>'
        f'<td>{t}</td></tr>'
        for t in items) + '</table>'


class Check(QCheckBox):
    """A checkbox with a drawn tick, not only an orange fill."""

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self.isChecked():
            return
        opt = QStyleOptionButton()
        self.initStyleOption(opt)
        r = QRectF(self.style().subElementRect(QStyle.SE_CheckBoxIndicator, opt, self))
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor('#141414'), 2.2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        x, y, w, h = r.x(), r.y(), r.width(), r.height()
        p.drawPolyline([QPointF(x + w * .26, y + h * .52), QPointF(x + w * .44, y + h * .70),
                        QPointF(x + w * .76, y + h * .32)])


def frame(name):
    f = QFrame()
    f.setObjectName(name)
    lay = QVBoxLayout(f)
    lay.setContentsMargins(16, 14, 16, 14)
    lay.setSpacing(6)
    return f, lay


def button(text, name=None):
    b = QPushButton(text)
    if name:
        b.setObjectName(name)
    b.setCursor(Qt.PointingHandCursor)
    return b


class Page(QWidget):
    def __init__(self, title, lead=''):
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        # The content keeps its own height and scrolls when the details take the room; the
        # buttons stay put below it
        content = QWidget()
        scroll = QScrollArea()
        scroll.setWidget(content)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        outer.addWidget(scroll, 1)
        self.lay = QVBoxLayout(content)
        self.lay.setContentsMargins(36, 30, 36, 12)
        self.lay.setSpacing(14)
        self.title = label(title, 'title')
        self.lay.addWidget(self.title)
        self.lead = label(lead, 'lead')
        self.lead.setVisible(bool(lead))
        self.lay.addWidget(self.lead)
        self.body = QVBoxLayout()
        self.body.setSpacing(12)
        self.lay.addLayout(self.body)
        self.lay.addStretch(1)
        self.buttons = QHBoxLayout()
        self.buttons.setContentsMargins(36, 12, 36, 24)
        self.buttons.setSpacing(10)
        self.buttons.addStretch(1)
        outer.addLayout(self.buttons)


class Progress(QWidget):
    """A status line, a bar, and done/total, speed and time left under it."""

    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        self.status = label('', 'status')
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.bar.setAccessibleName('Progress')
        self.detail = label('', 'small')
        lay.addWidget(self.status)
        lay.addWidget(self.bar)
        lay.addWidget(self.detail)
        self.rate = flow.Rate()
        self.unit_bytes = True

    def reset(self, text=''):
        self.status.setText(text)
        self.bar.setRange(0, 1000)
        self.bar.setValue(0)
        self.detail.setText('')
        self.rate = flow.Rate()

    def busy(self, text=None):
        """A moving bar for work that has no measure, such as a password prompt."""
        if text is not None:
            self.status.setText(text)
        self.bar.setRange(0, 0)
        self.detail.setText('')

    def set(self, done, total):
        if total <= 1 and done < total:
            return self.busy()  # one piece of work: nothing to count
        self.bar.setRange(0, 1000)
        self.bar.setValue(int(1000 * done / total) if total else 0)
        if self.rate.marks and done < self.rate.marks[-1][1]:
            self.rate = flow.Rate()  # the next phase starts from 0: its own speed
        self.rate.add(done)
        if not self.unit_bytes or total <= 1:
            self.detail.setText('')
            return
        bits = [f'{int(100 * done / total)}%', f'{size(done)} of {size(total)}']
        left = self.rate.left(done, total)
        if len(self.rate.marks) > 1:
            (t0, d0), (t1, d1) = self.rate.marks[0], self.rate.marks[-1]
            if t1 > t0 and d1 >= d0:
                bits.append(f'{size((d1 - d0) / (t1 - t0))}/s')
        if left is not None:
            bits.append(minutes(left))
        self.detail.setText(' - '.join(bits))


class ErrorCard(QFrame):
    """What went wrong, whether the r1 is safe, and the ways on."""

    def __init__(self, window):
        super().__init__()
        self.setObjectName('error')
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        self.head = label('', 'status')
        self.text = label('')
        self.safety = label('', 'small')
        self.hint = label('', 'small')
        for w in (self.head, self.text, self.safety, self.hint):
            lay.addWidget(w)
        row = QHBoxLayout()
        self.retry = button('Try again', 'primary')
        self.save = button('Save log...')
        self.details = button('Show details')
        row.addWidget(self.retry)
        row.addWidget(self.save)
        row.addWidget(self.details)
        row.addStretch(1)
        lay.addLayout(row)
        self.save.clicked.connect(window.save_log)
        self.details.clicked.connect(window.toggle_log)
        self.on_retry = None
        self.hide()

    def show_error(self, head, text, safe, hint=None, calm=False, retry='Try again'):
        """safe: True, False, a sentence of its own, or None where no r1 is involved. calm: the
        user stopped it, which is no error."""
        self.head.setText(head)
        self.text.setText(text)
        self.hint.setText(hint or '')
        self.hint.setVisible(bool(hint))
        self.safety.setText(
            'Nothing on the r1 was changed by this step. It is safe to unplug it.' if safe is True else
            'The step did not finish, so the r1 will not start right now. That is recoverable: the '
            f'r1 can always be reconnected, and {retry} finishes the job.' if safe is False else safe or '')
        self.safety.setVisible(bool(self.safety.text()))
        self.retry.setText(retry)
        self.setProperty('calm', calm)
        self.style().unpolish(self)
        self.style().polish(self)
        self.show()
        self.retry.setFocus()


# -- the window

class Window(QWidget):
    def __init__(self, demo=False):
        super().__init__()
        self.demo = demo
        self.setWindowTitle(paths.NAME)
        self.resize(980, 680)
        self.setMinimumSize(900, 620)
        self.setStyleSheet(STYLE)
        stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
        self.log_path = os.path.join(paths.logs(), f'installer-{stamp}.log')
        self.log_file = open(self.log_path, 'a', encoding='utf-8', buffering=1)
        self.job = None
        self.session = None
        self.device = None
        self.release = None
        self.pkg = None
        self.pkg_path = None
        self.verified = False
        self.mode = 'clean'
        self.problems = []  # what an update keeping data would change, flow.update_problems
        self.allow_downgrade = False
        self.why_clicks = 0
        self.restore_file = None
        self.backup_file = None
        self.cid = None
        self.resume = None  # the step to go on with after a reconnect

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        side = QFrame()
        side.setObjectName('side')
        side.setFixedWidth(210)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(14, 22, 8, 16)
        brand = label('<span style="font-size:20px;font-weight:600;color:#fff">haretic</span>'
                      f'<span style="font-size:20px;color:{ORANGE}"> installer</span>', wrap=False)
        sl.addWidget(brand)
        sl.addWidget(label(f'for the rabbit r1 - {paths.VERSION}', 'small'))
        sl.addSpacing(18)
        self.stepper = Stepper(STEPS)
        sl.addWidget(self.stepper, 1)
        self.log_toggle = button('Show details', 'link')
        self.log_toggle.clicked.connect(self.toggle_log)
        sl.addWidget(self.log_toggle)
        root.addWidget(side)

        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)
        self.pages = QStackedWidget()
        right.addWidget(self.pages, 1)
        self.log_view = QPlainTextEdit(readOnly=True)
        self.log_view.setAccessibleName('Installer log')
        self.log_view.setMaximumBlockCount(5000)
        self.log_view.setFixedHeight(170)
        self.log_view.hide()
        lw = QWidget()
        ll = QVBoxLayout(lw)
        ll.setContentsMargins(36, 0, 36, 16)
        ll.addWidget(self.log_view)
        right.addWidget(lw)
        root.addLayout(right, 1)

        self.build_pages()
        self.log(f'{paths.NAME} {paths.VERSION} on {sys.platform}; data in {paths.data_dir()}')
        need = None if demo else drivers.needed()
        if need:
            self.show_setup(need)
        else:
            self.show_welcome()

    # -- log
    def log(self, line):
        line = str(line).rstrip()
        if not line:
            return
        self.log_file.write(f'{time.strftime("%H:%M:%S")} {line}\n')
        self.log_view.appendPlainText(line)

    def toggle_log(self):
        self.log_view.setVisible(not self.log_view.isVisible())
        text = 'Hide details' if self.log_view.isVisible() else 'Show details'
        self.log_toggle.setText(text)
        for i in range(self.pages.count()):
            self.pages.widget(i).error.details.setText(text)

    def save_log(self):
        path, _ = QFileDialog.getSaveFileName(self, 'Save the log', os.path.join(
            os.path.expanduser('~'), os.path.basename(self.log_path)), 'Log (*.log *.txt)')
        if path:
            with open(self.log_path, encoding='utf-8') as src, open(path, 'w', encoding='utf-8') as out:
                out.write(src.read())

    # -- running steps
    def run(self, fn, ok, progress=None, status=None, error=None):
        self.job = Job(fn, self)
        self.job.logged.connect(self.log)
        if progress:
            self.job.progressed.connect(progress.set)
            self.job.status_changed.connect(progress.status.setText)
        if status:
            self.job.status_changed.connect(status)
        self.job.finished_ok.connect(ok)
        self.job.failed.connect(error or self.failed)
        self.job.start()

    def show_page(self, page, step=None):
        self.pages.setCurrentWidget(page)
        if step is not None:
            self.stepper.set(step)
        main = [b for b in page.findChildren(QPushButton)
                if b.objectName() == 'primary' and b.isVisibleTo(page) and b.isEnabled()]
        if main:
            main[-1].setFocus()

    def failed(self, e, page=None, head=None):
        """Show an exception on the current page's error card."""
        page = page or self.pages.currentWidget()
        card = page.error
        head = head or getattr(page, 'stopped', 'This step stopped')
        # Before the r1 is connected, nothing can have happened to it
        safe = None if page in (self.p_setup, self.p_welcome, self.p_download) else True
        hint, calm, again = None, False, 'Try again'
        retry = self.retry_step
        if isinstance(e, Cancelled):
            text, calm = 'You stopped it.', True
            again = 'Wait again' if page is self.p_connect else 'Continue'
        elif isinstance(e, r1.NotAnR1):
            head = 'This does not look like a rabbit r1'
            text = str(e)
            safe = 'Nothing on the device was changed. It is safe to unplug it.'
            hint = 'The installer only works with a rabbit r1. Unplug this device, connect an r1, then press Try again.'
            retry = self.start_connect
        elif isinstance(e, flow.StepError):
            text, safe, hint = str(e), e.safe, e.hint
        elif isinstance(e, DeviceError):
            text = str(e)
            safe = getattr(self, 'step_safe', True)
            hint = ('To go on, reconnect the r1: unplug it, hold its side button for about 10 seconds '
                    'until the screen stays dark, then plug it in again and press Try again.')
            retry = self.reconnect
        elif isinstance(e, (package.PackageError, OSError)):
            text = str(e)
        else:
            text = f'Something unexpected happened: {type(e).__name__}: {e}'
            hint = 'The details have the full error; please save the log and report it.'
        self.stepper.set(self.stepper.current, failed=not calm)
        if card.on_retry:
            card.retry.clicked.disconnect(card.on_retry)
        card.on_retry = lambda: (card.hide(), self.stepper.set(self.stepper.current), retry())
        card.retry.clicked.connect(card.on_retry)
        card.show_error(head, text, safe, hint, calm, again)
        self.log(f'{"stopped" if calm else "error"}: {head}: {text}')
        if hasattr(page, 'on_error'):
            page.on_error()

    def retry_step(self):
        if self.resume:
            self.resume()

    def reconnect(self):
        """After a USB failure: a fresh connection to the same r1, then the step that failed."""
        if self.device:
            self.device.close()
        self.device = None
        self.start_connect(after=self.resume)

    # -- pages
    def build_pages(self):
        self.p_setup = self.page_setup()
        self.p_welcome = self.page_welcome()
        self.p_download = self.page_download()
        self.p_connect = self.page_connect()
        self.p_check = self.page_check()
        self.p_backup = self.page_backup()
        self.p_unlock = self.page_unlock()
        self.p_install = self.page_install()
        self.p_done = self.page_done()
        self.p_restore = self.page_restore()
        for p in (self.p_setup, self.p_welcome, self.p_download, self.p_connect, self.p_check, self.p_backup,
                  self.p_unlock, self.p_install, self.p_done, self.p_restore):
            if not hasattr(p, 'error'):
                p.error = ErrorCard(self)
                p.body.addWidget(p.error)
            self.pages.addWidget(p)

    # 0. one-time USB setup
    def page_setup(self):
        p = Page('One-time setup: USB access')
        p.lead.show()
        p.ids = label('', 'small')
        p.body.addWidget(p.ids)
        p.skip_note = label('Skip only if this computer can already reach the r1 over USB; without '
                            'it, the installer cannot find the r1.', 'small')
        p.body.addWidget(p.skip_note)
        p.progress = Progress()
        p.progress.unit_bytes = True
        p.progress.hide()
        p.body.addWidget(p.progress)
        p.go = button('Set up USB access', 'primary')
        p.skip = button('Skip')
        p.buttons.addWidget(p.skip)
        p.buttons.addWidget(p.go)
        p.skip.clicked.connect(self.show_welcome)
        p.go.clicked.connect(self.setup_usb)
        return p

    def show_setup(self, need):
        p = self.p_setup
        self.need = need
        ids = ', '.join(f'{r1.VENDOR:04X}:{pid:04X} ({name})' for pid, name in r1.USB_IDS.items())
        if need == 'usbdk':
            p.lead.setText(
                'Before the first install, this computer needs <b>UsbDk</b>, a free, signed USB driver '
                'from the daynix project (Red Hat), to talk to the r1. The installer downloads UsbDk '
                '1.0.22 from its official release and checks it; Windows then asks you to allow the '
                'install. This is needed only once.')
            p.ids.setText('UsbDk covers the r1 in each of its USB modes: ' + ids + '.')
            p.go.setText('Install the USB driver')
        else:
            p.lead.setText(
                'Before the first install, Linux needs a small rule that lets you use the r1 over USB '
                'without administrator rights. The installer adds it to /etc/udev/rules.d and asks '
                'for your password. This is needed only once.')
            p.ids.setText('The rule covers the r1 in each of its USB modes (' + ids + ') and keeps '
                          'ModemManager from grabbing it.')
            p.go.setText('Allow USB access')
        self.stepper.set_steps(STEPS)
        self.show_page(p, -1)  # before the first step

    def setup_usb(self):
        p = self.p_setup
        p.go.setEnabled(False)
        p.skip.setEnabled(False)
        p.progress.show()
        p.progress.reset('Downloading UsbDk')
        if self.need != 'usbdk':
            p.progress.busy('Waiting for your password')

        def work(job):
            if self.need == 'usbdk':
                msi = drivers.download_usbdk(paths.downloads(), job.progress)
                job.status('Installing UsbDk - confirm the Windows prompt')
                return drivers.install_usbdk(msi)
            drivers.install_udev()
            return False

        def ok(restart):
            p.go.setEnabled(True)
            p.skip.setEnabled(True)
            if restart:
                QMessageBox.information(self, paths.NAME, 'The USB driver is installed. Windows asks '
                                        'for a restart; restart, then open the installer again.')
            self.log('USB access set up')
            self.show_welcome()

        def bad(e):
            p.go.setEnabled(True)
            p.skip.setEnabled(True)
            self.failed(e, p, 'USB access was not set up')
        self.resume = self.setup_usb
        self.run(work, ok, p.progress, status=lambda _: self.need == 'usbdk' and p.progress.busy(), error=bad)

    # 1. welcome
    def page_welcome(self):
        p = Page('Install haretic on your rabbit r1',
                 'haretic replaces rabbitOS with Android (LineageOS 24) on the r1. This guide goes '
                 'through seven steps and explains each one before it happens. It takes about '
                 '30 minutes.')
        card, lay = frame('card')
        lay.addWidget(label('<b>Before you start</b>'))
        lay.addWidget(label(bullets(['A USB-C cable that carries data (some cables only charge)',
                                     'About 15 GB free on this computer, for a backup of the r1',
                                     'The r1 charged to at least 30%'])))
        p.body.addWidget(card)
        warn, wl = frame('warn')
        wl.addWidget(label('<b>Installing erases everything on the r1.</b> The installer first backs up '
                           'rabbitOS into one restore file, which this app can put back later.'))
        p.body.addWidget(warn)
        src, sl = frame('card')
        sl.addWidget(label('<b>haretic to install</b>'))
        p.group = QButtonGroup(p)
        p.online = QRadioButton('Looking for the latest haretic release...')
        p.online.setEnabled(False)
        p.local = QRadioButton('An install package on this computer')
        p.group.addButton(p.online)
        p.group.addButton(p.local)
        row = QHBoxLayout()
        row.addWidget(p.online)
        p.again = button('Look again', 'link')
        p.again.hide()
        row.addWidget(p.again)
        row.addStretch(1)
        sl.addLayout(row)
        opts = QHBoxLayout()
        opts.setContentsMargins(28, 0, 0, 0)
        p.gapps = Check('Google apps (Play Store and Google services)')
        p.gapps.setToolTip('Adds the Play Store and Google services, which some apps need.')
        p.root = Check('Root (Magisk)')
        p.root.setToolTip('Adds Magisk, which lets apps you allow take full control of the r1. For advanced users.')
        for c in (p.gapps, p.root):
            c.setEnabled(False)
            opts.addWidget(c)
            c.toggled.connect(lambda _: self.variant_text())
        opts.addStretch(1)
        sl.addLayout(opts)
        p.variant = label('', 'small')
        p.variant.setContentsMargins(28, 0, 0, 4)
        sl.addWidget(p.variant)
        row = QHBoxLayout()
        row.addWidget(p.local)
        p.choose = button('Choose...')
        row.addWidget(p.choose)
        row.addStretch(1)
        sl.addLayout(row)
        p.picked = label('', 'small')
        p.picked.setContentsMargins(28, 0, 0, 0)
        p.picked.hide()
        sl.addWidget(p.picked)
        p.body.addWidget(src)
        p.restore = button('Put rabbitOS back from a backup instead', 'link')
        p.buttons.insertWidget(0, p.restore)
        p.start = button('Start', 'primary')
        p.start.setEnabled(False)
        p.buttons.addWidget(p.start)
        p.choose.clicked.connect(self.choose_package)
        p.group.buttonClicked.connect(lambda _: self.welcome_ready())
        p.start.clicked.connect(self.start_install)
        p.restore.clicked.connect(self.show_restore)
        p.again.clicked.connect(self.look_again)
        return p

    def look_again(self):
        p = self.p_welcome
        p.again.hide()
        p.online.setText('Looking for the latest haretic release...')
        self.release, self.asked_release = None, False
        self.show_welcome()

    def show_welcome(self):
        self.stepper.set_steps(STEPS)
        self.show_page(self.p_welcome, 0)
        p = self.p_welcome
        if self.release is None and not getattr(self, 'asked_release', False):
            self.asked_release = True

            def ok(found):
                self.releases = found
                if found:
                    rel = found[0]
                    pre = ' (pre-release)' if rel['prerelease'] else ''
                    p.online.setText(f'Download {rel["version"]}{pre}')
                    p.online.setEnabled(True)
                    p.gapps.setEnabled(any(r['gapps'] for r in found))
                    p.root.setEnabled(any(r['root'] for r in found))
                    if not p.local.isChecked():
                        p.online.setChecked(True)
                else:
                    p.online.setText('No haretic release is published yet - use a package file')
                    p.again.show()
                self.variant_text()

            def bad(e):
                self.release = False
                p.online.setText('Could not reach GitHub to find haretic releases - use a package file')
                p.again.show()
                self.log(f'release lookup: {e}')
            self.run(lambda job: package.latest(), ok, error=bad)

    def choose_package(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Choose a haretic install package', paths.downloads(),
                                              'haretic install package (*.tar.xz)')
        if not path:
            return
        try:
            pkg = package.Package(path)
        except package.PackageError as e:
            QMessageBox.warning(self, paths.NAME, str(e))
            return
        self.pkg, self.pkg_path, self.verified = pkg, path, False
        p = self.p_welcome
        p.local.setChecked(True)
        p.picked.setText(f'{pkg.version}, {package.describe(pkg.gapps, pkg.root, pkg.aosp)} - {os.path.basename(path)}')
        p.picked.show()
        self.welcome_ready()

    def variant_text(self):
        """The release package matching the boxes ticked, and what it is."""
        p = self.p_welcome
        found = getattr(self, 'releases', None) or []
        pick = [r for r in found if r['gapps'] == p.gapps.isChecked() and r['root'] == p.root.isChecked()]
        self.release = pick[0] if pick else (False if found else self.release)
        if self.release:
            r = self.release
            p.variant.setText(f'{size(r["size"])} download - {r["name"]}<br>Not sure? Tick Google apps for the '
                              'Play Store; leave Root off.')
        elif found:
            p.variant.setText('This release has no package with these options. Change the ticks, or use a '
                              'package file.')
        self.welcome_ready()

    def welcome_ready(self):
        p = self.p_welcome
        p.start.setEnabled((p.online.isChecked() and bool(self.release)) or (p.local.isChecked() and self.pkg is not None))

    # 2. download and check the package
    def page_download(self):
        p = Page('Get haretic', 'Downloading the haretic install package, then unpacking it and checking '
                 'every image in it, so nothing damaged reaches the r1. A stopped download continues '
                 'where it was.')
        p.progress = Progress()
        p.body.addWidget(p.progress)
        p.cancel = button('Stop')
        p.buttons.addWidget(p.cancel)
        p.cancel.clicked.connect(lambda: self.job and setattr(self.job, 'stop', True))
        p.stopped = 'Getting haretic stopped'
        p.on_error = lambda: p.cancel.setEnabled(False)
        return p

    def start_install(self):
        self.restore_file = None
        self.stepper.set_steps(STEPS)
        p = self.p_download
        self.show_page(p, 0)
        p.progress.reset('Preparing')
        p.error.hide()
        p.cancel.setEnabled(True)
        online = self.p_welcome.online.isChecked()

        def work(job):
            if online:
                job.status(f'Downloading {self.release["name"]}')
                path = package.download(self.release, paths.downloads(), job.progress, job.cancelled)
                if path is None:
                    raise Cancelled()
                self.pkg, self.pkg_path, self.verified = package.Package(path), path, False
            if not self.verified:
                job.status('Unpacking and checking the package')
                if not self.pkg.verify(job.progress, job.cancelled):
                    raise Cancelled()
                self.verified = True
            return self.pkg

        def ok(pkg):
            self.log(f'package ok: {pkg.version} ({self.pkg_path})')
            self.start_connect(after=self.show_check)
        self.resume = self.start_install
        self.run(work, ok, p.progress)

    # 3. connect
    def page_connect(self):
        p = Page('Connect the r1', 'The installer talks to the r1 while it is switched off, through its '
                 'MediaTek download mode, which starts for a moment when an r1 that is off is plugged in.')
        row = QHBoxLayout()
        steps = label('<table cellspacing="0" cellpadding="5">'
                      f'<tr><td style="color:{ORANGE};font-weight:600">1</td><td>Unplug the r1 if it is plugged in.</td></tr>'
                      f'<tr><td style="color:{ORANGE};font-weight:600">2</td><td>Turn it off: hold the side button for about '
                      '10 seconds, until the screen stays dark.</td></tr>'
                      f'<tr><td style="color:{ORANGE};font-weight:600">3</td><td>Plug it into this computer. Keep it '
                      'plugged in until the installer says you can unplug it.</td></tr></table>')
        row.addWidget(steps, 3)
        p.picture = R1Picture()
        row.addWidget(p.picture, 2)
        p.body.addLayout(row)
        p.state = label('', 'status')
        p.body.addWidget(p.state)
        p.help, hl = frame('card')
        hl.addWidget(label('<b>Not showing up?</b>'))
        hl.addWidget(label('- Try another cable: many only charge.<br>'
                           '- Use a port on the computer itself, not a hub or a monitor.<br>'
                           '- Make sure the r1 is really off: unplug, hold the side button 10 seconds, plug in again.'
                           + ('<br>- If Windows plays no sound when you plug it in, try another port.'
                              if sys.platform == 'win32' else '')))
        p.help.hide()
        p.body.addWidget(p.help)
        p.cancel = button('Stop waiting')
        p.buttons.addWidget(p.cancel)
        p.cancel.clicked.connect(lambda: self.job and setattr(self.job, 'stop', True))
        p.timer = QTimer(p, singleShot=True, interval=45000, timeout=p.help.show)
        return p

    def start_connect(self, after=None):
        p = self.p_connect
        self.show_page(p, 1)
        p.error.hide()
        p.help.hide()
        p.picture.set_state('waiting')
        p.state.setText('Waiting for the r1...')
        p.timer.start()
        p.cancel.setEnabled(True)
        after = after or self.show_check

        texts = {'waiting': 'Waiting for the r1...',
                 'found': 'Found the r1 - connecting...',
                 'agent': 'Starting the download agent on the r1...'}

        def work(job):
            if self.device is None:
                if self.demo:
                    self.device = SimDevice(os.path.join(paths.data_dir(), 'demo-r1'), rate=self.demo,
                                            log=job.log, slot=os.environ.get('HARETIC_DEMO_SLOT', 'b'))
                else:
                    self.device = MtkDevice(paths.work(), log=job.log)
            session = flow.Session(self.device, job)
            identity = session.connect(lambda s: job.status(texts.get(s, s)))
            if self.cid and identity['cid'] != self.cid:
                if self.restore_file:
                    raise flow.StepError('This r1 is not the one the restore file was made from.',
                                         hint='Connect the r1 the restore file came from, or choose its own '
                                         'restore file.')
                raise flow.StepError('This is a different r1 from the one the installer started with.',
                                     hint='Connect the first r1 to finish its install.')
            job.status('Reading the r1...')
            session.inspect(identity)
            return session

        def status(text):
            p.state.setText(text)
            if text != texts['waiting']:
                p.timer.stop()
                p.help.hide()

        def ok(session):
            self.session = session
            self.cid = session.info.identity['cid']
            p.picture.set_state('connected')
            p.timer.stop()
            after()

        def bad(e):
            p.picture.set_state('off')
            p.timer.stop()
            p.help.hide()
            p.cancel.setEnabled(False)
            if not isinstance(e, flow.StepError) and self.device:
                self.device.close()
                self.device = None
            self.failed(e, p, 'Connecting stopped' if not isinstance(e, Cancelled) else 'Stopped waiting')
        self.resume = lambda: self.start_connect(after)
        self.run(work, ok, status=status, error=bad)

    # 4. check
    def page_check(self):
        p = Page('This is your rabbit r1')
        p.facts = label('')
        card, lay = frame('card')
        lay.addWidget(p.facts)
        p.body.addWidget(card)
        p.choice, cl = frame('card')
        p.update = QRadioButton('Update haretic and keep my data')
        p.clean = QRadioButton('Clean install: erase everything and start fresh')
        p.update.setChecked(True)
        p.runs = label('')
        cl.addWidget(p.runs)
        cl.addWidget(p.update)
        cl.addWidget(p.clean)
        p.body.addWidget(p.choice)
        p.why, wl = frame('warn')
        p.why_text = label('')
        # Five presses on a refused downgrade allow it, as Android's build number does
        p.why_text.mousePressEvent = self.why_pressed
        wl.addWidget(p.why_text)
        p.body.addWidget(p.why)
        p.next = label('', 'lead')
        p.body.addWidget(p.next)
        p.go = button('Continue', 'primary')
        p.buttons.addWidget(p.go)
        p.update.toggled.connect(lambda _: self.check_text())
        p.go.clicked.connect(self.after_check)
        return p

    def show_check(self):
        if self.restore_file:
            return self.show_restore_confirm()
        p = self.p_check
        info = self.session.info
        p.error.hide()
        lk = {'haretic': 'haretic', 'stock': 'rabbitOS (v0.8.293)', 'other': 'rabbitOS'}
        haretic = info.system == 'haretic' and info.slot == 'a'
        running = lk[info.lk[info.slot]]
        if haretic and info.installed:
            i = info.installed
            running = ', '.join([i['version'] or 'haretic'] + ['plain LineageOS (aosp)'] * bool(i['aosp'])
                                + ['with Google apps'] * bool(i['gapps']) + ['rooted (Magisk)'] * bool(i['root']))
        rows = [('Storage', f'{info.identity["user_size"] / 1e9:.0f} GB eMMC'),
                ('Running', f'{running} on slot {info.slot.upper()}'),
                ('Bootloader', 'unlocked' if info.lock == 'unlocked' else 'locked - the installer unlocks it'),
                ('Storage ID', info.identity['cid'][:8] + '...')]
        p.facts.setText('<table cellpadding="4">' + ''.join(
            f'<tr><td style="color:#9a9a9a;padding-right:18px">{k}</td><td>{v}</td></tr>' for k, v in rows)
            + '</table>')
        self.problems = flow.update_problems(info.installed, self.pkg) if haretic and self.pkg else []
        self.why_clicks = 0
        blocked = self.update_blocked()
        p.choice.setVisible(haretic)
        aosp = bool(info.installed and info.installed['aosp'])
        p.runs.setText(f'<b>This r1 already runs {"plain LineageOS (aosp)" if aosp else "haretic"}</b>')
        p.update.setEnabled(not blocked)
        (p.update if haretic and not blocked else p.clean).setChecked(True)
        self.check_text()
        self.show_page(p, 2)

    def update_blocked(self):
        return any(k == 'refuse' or k == 'downgrade' and not self.allow_downgrade for k, _ in self.problems)

    def update_warnings(self):
        return [t for k, t in self.problems if k == 'warn' or k == 'downgrade' and self.allow_downgrade]

    def why_pressed(self, _event):
        kinds = {k for k, _ in self.problems}
        if self.allow_downgrade or 'downgrade' not in kinds or 'refuse' in kinds:
            return
        self.why_clicks += 1
        if self.why_clicks >= 5:
            self.allow_downgrade = True
            self.log('downgrade keeping data allowed')
            self.show_check()
            self.p_check.update.setChecked(True)

    def check_text(self):
        p = self.p_check
        self.mode = 'update' if not p.choice.isHidden() and p.update.isChecked() else 'clean'
        if self.update_blocked():
            p.why_text.setText('<b>Updating with your data kept is not possible</b><br>'
                               + '<br>'.join(t for k, t in self.problems if k != 'warn'))
        elif self.mode == 'update' and self.update_warnings():
            p.why_text.setText('<b>This update changes what the r1 has</b><br>' + '<br>'.join(self.update_warnings()))
        p.why.setVisible(self.update_blocked() or self.mode == 'update' and bool(self.update_warnings()))
        if self.mode == 'update':
            p.next.setText('Nothing has been changed so far. Next, the installer writes the new haretic '
                           'over the old one. Your apps, files and settings stay. A backup first is '
                           'optional.')
        elif not p.choice.isHidden():
            p.next.setText('Nothing has been changed so far. Next, the installer erases the r1 and installs '
                           'haretic fresh. A backup first is optional.')
        else:
            slot_b = self.session.info.slot == 'b'
            p.next.setText('Nothing has been changed so far. Next, the installer backs up the r1, then '
                           'unlocks it and installs haretic. ' + (
                               'The r1 runs from its second slot (B); haretic installs to slot A with '
                               'the firmware the r1 runs now copied over. ' if slot_b else ''))

    def after_check(self):
        self.show_backup()

    # 5. back up
    def page_backup(self):
        p = Page('Back up the r1', '')
        p.lead.show()
        card, lay = frame('card')
        p.existing = QRadioButton('')
        p.fresh = QRadioButton('Make a new backup')
        p.skip = QRadioButton('Continue without a backup')
        p.group = QButtonGroup(p)
        for b in (p.existing, p.skip, p.fresh):
            p.group.addButton(b)
            lay.addWidget(b)
        row = QHBoxLayout()
        p.folder = label('', 'small')
        row.addWidget(p.folder, 1)
        p.change = button('Change folder...')
        row.addWidget(p.change)
        lay.addLayout(row)
        p.userdata = Check('Also back up personal data (userdata) - about 116 GB more, and slow')
        lay.addWidget(p.userdata)
        p.options = card
        p.body.addWidget(card)
        p.stock, sl = frame('warn')
        p.stock_text = label('')
        sl.addWidget(p.stock_text)
        p.body.addWidget(p.stock)
        p.progress = Progress()
        p.progress.hide()
        p.body.addWidget(p.progress)
        p.cancel = button('Stop')
        p.cancel.hide()
        p.go = button('Back up', 'primary')
        p.buttons.addWidget(p.cancel)
        p.buttons.addWidget(p.go)
        p.root = paths.backups()
        p.change.clicked.connect(self.change_backup_folder)
        p.go.clicked.connect(self.start_backup)
        p.cancel.clicked.connect(lambda: self.job and setattr(self.job, 'stop', True))
        p.on_error = lambda: (p.cancel.hide(), p.go.setEnabled(True), p.options.setEnabled(True))
        p.stopped = 'The backup stopped'
        p.group.buttonToggled.connect(lambda *_: self.backup_button())
        p.userdata.toggled.connect(lambda _: self.backup_room())
        return p

    def show_backup(self):
        p = self.p_backup
        p.error.hide()
        p.progress.hide()
        p.go.setEnabled(True)
        p.cancel.hide()
        p.options.setEnabled(True)
        self.backup_choices()
        self.show_page(p, 3)

    def backup_choices(self):
        """rabbitOS is always backed up once, into the restore file that is the only way back; an
        r1 already running haretic is backed up only when asked."""
        p = self.p_backup
        haretic = self.session.info.system == 'haretic'
        stock = flow.backups(p.root, self.cid, 'rabbitOS')
        p.found = stock[0][0] if stock and not haretic else None
        p.skip.setVisible(haretic)
        p.existing.setVisible(bool(p.found))
        p.fresh.setVisible(haretic or bool(p.found))  # no lone choice
        p.fresh.setText('Back up haretic as it is now' if haretic else 'Make a new backup')
        if haretic:
            p.lead.setText('This r1 already runs haretic, so nothing needs backing up. A backup of '
                           'haretic as it is now is optional.')
            p.skip.setChecked(True)
            if stock:
                p.stock_text.setText(f'Your rabbitOS restore file is <b>{os.path.basename(stock[0][0])}</b>, in '
                                     f'{os.path.dirname(stock[0][0])}. Keep a copy somewhere safe: it is the '
                                     'only way to put rabbitOS back.')
            else:
                p.stock_text.setText(f'There is no rabbitOS restore file of this r1 in {p.root}. '
                                     'Without it, the r1 cannot go back to rabbitOS. If you saved it '
                                     'elsewhere, keep it safe.')
        else:
            p.lead.setText('rabbitOS is backed up into one restore file, the only way to put it back '
                           'later with "Put rabbitOS back from a backup". Everything is in it except '
                           'your personal data.')
            if p.found:
                when = stock[0][1]['created'].replace('T', ' ')[:16]
                p.existing.setText(f'Use the rabbitOS restore file from {when}')
                p.existing.setChecked(True)
            else:
                p.fresh.setChecked(True)
        p.stock.setVisible(haretic)
        self.backup_button()

    def backup_button(self):
        p = self.p_backup
        p.go.setText('Back up' if p.fresh.isChecked() else 'Continue')
        p.userdata.setEnabled(p.fresh.isChecked())
        self.backup_room()

    def backup_room(self):
        """The folder and its free space, flagged when it is less than the backup's own check asks."""
        p = self.p_backup
        if self.session is None:
            return
        free = flow.shutil.disk_usage(p.root).free
        need = min(sum(n for *_, n in self.session.backup_plan(p.userdata.isChecked())) // 2, flow.BACKUP_FREE)
        if p.fresh.isChecked() and free < need:
            p.folder.setText(f'Saved in {p.root} - <span style="color:#ff9e7a">only {size(free)} free; the '
                             f'backup needs about {size(need)}. Choose another folder or free up space.</span>')
        else:
            p.folder.setText(f'Saved in {p.root} - {size(free)} free')

    def change_backup_folder(self):
        p = self.p_backup
        folder = QFileDialog.getExistingDirectory(self, 'Where to keep the backup', p.root)
        if folder:
            p.root = folder
            self.backup_choices()

    def start_backup(self):
        p = self.p_backup
        p.error.hide()
        if p.skip.isVisible() and p.skip.isChecked():
            self.backup_file = None
            self.log('no backup: the r1 runs haretic')
            return self.confirm_writes()
        if p.existing.isVisible() and p.existing.isChecked():
            self.backup_file = p.found
            self.log(f'using restore file {p.found}')
            return self.confirm_writes()
        p.go.setEnabled(False)
        p.options.setEnabled(False)
        p.cancel.show()
        p.progress.show()
        p.progress.reset('Starting the backup')
        self.step_safe = True
        userdata = p.userdata.isChecked()

        def ok(path):
            p.cancel.hide()
            p.progress.status.setText(f'Saved {path}')
            # The new file is now a choice; Continue goes on from here if the next question is put off
            p.options.setEnabled(True)
            p.go.setEnabled(True)
            self.backup_choices()
            if self.session.info.system == 'haretic':
                self.backup_file = None
                self.confirm_writes()
                return
            self.backup_file = path
            self.keep_safe(path)
            self.confirm_writes()
        self.resume = self.start_backup
        self.run(lambda job: self.with_reporter(job, self.session.backup, p.root, userdata), ok, p.progress)

    def ask(self, title, text, go, danger=False, cancel='Not now'):
        """A question before a step that changes the r1: True to go ahead. The safe answer is the
        default, so Enter never erases anything."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle(title)
        box.setText(f'<b>{title}</b>')
        box.setInformativeText(text)
        box.setTextInteractionFlags(Qt.TextSelectableByMouse)
        yes = button(go, 'danger' if danger else 'primary')
        box.addButton(yes, QMessageBox.DestructiveRole if danger else QMessageBox.AcceptRole)
        box.setDefaultButton(box.addButton(cancel, QMessageBox.RejectRole))
        box.exec()
        return box.clickedButton() is yes

    def keep_safe(self, path):
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Information)
        box.setWindowTitle('Keep the restore file safe')
        box.setText('<b>Keep the restore file safe</b>')
        box.setInformativeText(f'rabbitOS is backed up in <b>{os.path.basename(path)}</b>, in '
                               f'{os.path.dirname(path)}.<br><br>This file is the only way to put rabbitOS '
                               'back on this r1. Copy it somewhere safe, such as a USB stick or cloud '
                               'storage, not only this computer.')
        box.setTextInteractionFlags(Qt.TextSelectableByMouse)
        ok = button('I will keep it safe', 'primary')
        box.addButton(ok, QMessageBox.AcceptRole)
        show = box.addButton('Show the file', QMessageBox.ActionRole)
        box.setDefaultButton(ok)
        box.exec()
        if box.clickedButton() is show:
            self.show_file(path)

    def show_file(self, path):
        QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(path)))

    def with_reporter(self, job, fn, *args):
        self.session.rep = job
        return fn(*args)

    def confirm_writes(self):
        if self.mode == 'update' and not self.update_warnings():
            return self.start_install_writes()
        if self.mode == 'update':
            if self.ask('Update and change what the r1 has?', bullets(self.update_warnings())
                        + '<p>Your apps, files and settings stay.</p>', 'Update anyway'):
                return self.start_install_writes()
            return self.log('update not confirmed')
        unlock = 'unlocks the bootloader, ' if self.session.info.lock != 'unlocked' else ''
        if self.ask('Erase the r1 and install haretic?', f'The installer {unlock}erases everything on the '
                    'r1 (apps, files and settings) and installs haretic.' + (
                        f'<br><br>Your restore file <b>{os.path.basename(self.backup_file)}</b> can put '
                        'rabbitOS back later.' if self.backup_file else ''), 'Erase and install', danger=True):
            self.start_unlock()
        else:
            self.log('install not confirmed')

    # 6. unlock
    def page_unlock(self):
        p = Page('Unlock the bootloader', 'The r1 only starts software rabbit has signed. Unlocking lets '
                 'it start haretic. The installer does it with mtkclient, which changes one small '
                 'record (seccfg); your backup keeps the original.')
        p.progress = Progress()
        p.progress.unit_bytes = False
        p.body.addWidget(p.progress)
        p.stopped = 'Unlocking stopped'
        return p

    def start_unlock(self):
        p = self.p_unlock
        self.show_page(p, 4)
        p.error.hide()
        p.progress.reset()
        p.progress.busy('Unlocking the bootloader')
        self.step_safe = 'If the r1 was unplugged during this, it may not start until the unlock is ' \
                         'finished. Reconnect it and press Try again.'

        def ok(changed):
            p.progress.set(1, 1)
            p.progress.status.setText('Unlocked' if changed else 'The bootloader was already unlocked')
            QTimer.singleShot(1200, self.start_install_writes)
        self.resume = self.start_unlock
        self.run(lambda job: self.with_reporter(job, self.session.unlock), ok, p.progress)

    # 7. install
    def page_install(self):
        p = Page('Install haretic', 'Writing haretic to the r1. Every piece is read back and checked. '
                 'Keep the r1 plugged in; if the cable comes out, plug it back in and press Try again - '
                 'the r1 can always be reconnected.')
        p.progress = Progress()
        p.body.addWidget(p.progress)
        p.items = label('', 'small')
        p.body.addWidget(p.items)
        p.stopped = 'Installing stopped'
        return p

    def start_install_writes(self):
        p = self.p_install
        self.show_page(p, 5)
        p.error.hide()
        p.progress.reset('Preparing')
        self.step_safe = False
        p.done = []

        def items(finished=False):
            shown = p.done[-6:]
            p.items.setText('<br>'.join(
                f'<span style="color:{ORANGE}">&#8594;</span> <span style="color:#e8e8e8">{t}</span>'
                if i == len(shown) - 1 and not finished else f'<span style="color:{ORANGE}">&#10003;</span> {t}'
                for i, t in enumerate(shown)))

        def status(text):
            if text.startswith(('Writing ', 'Erasing ')):
                p.done.append(text)
                items()

        def ok(_):
            p.progress.set(1, 1)
            p.progress.status.setText('Installed')
            items(finished=True)
            self.start_boot()
        self.resume = self.start_install_writes
        self.run(lambda job: self.with_reporter(job, self.session.install, self.pkg, self.mode, self.allow_downgrade),
                 ok, p.progress, status=status)

    # 8. start
    def page_done(self):
        p = Page('haretic is starting')
        p.keep, kl = frame('warn')
        kl.addWidget(label('<b>Keep your rabbitOS restore file safe</b>'))
        kl.addWidget(label('Copy it off this computer too, such as to a USB stick or cloud storage. It is the '
                           'only way to put rabbitOS back: open this installer and choose "Put rabbitOS back '
                           'from a backup".'))
        p.keep_path = label('', 'small')
        p.keep_path.setTextInteractionFlags(Qt.TextSelectableByMouse)
        kl.addWidget(p.keep_path)
        row = QHBoxLayout()
        p.open = button('Show the restore file')
        row.addWidget(p.open)
        row.addStretch(1)
        kl.addLayout(row)
        p.body.addWidget(p.keep)
        p.close = button('Done', 'primary')
        p.buttons.addWidget(p.close)
        p.open.clicked.connect(lambda: self.backup_file and self.show_file(self.backup_file))
        p.close.clicked.connect(self.close)
        return p

    def start_boot(self):
        p = self.p_done
        steps = STEPS if not self.restore_file else RESTORE_STEPS

        def ok(_):
            self.device = None
            self.stepper.set(len(steps))
            p.lead.show()
            if self.restore_file:
                what = self.restore_system
                p.title.setText(f'{what} is back')
                p.lead.setText(f'The r1 restarts into {what} now. You can unplug it.')
                p.keep.hide()
            else:
                p.title.setText('haretic is starting')
                logo = 'LineageOS' if self.pkg and self.pkg.aosp else 'haret1c'
                p.lead.setText(
                    'The r1 restarts with the new haretic now; your apps, files and settings are kept. The '
                    'start can take a few minutes. You can unplug the r1.' if self.mode == 'update' else
                    'The r1 restarts now. The first start takes a few minutes: haretic sets up its storage '
                    f'while the screen shows the {logo} logo. You can unplug the r1.')
                p.keep_path.setText(self.backup_file or '')
                p.keep.setVisible(bool(self.backup_file))
            self.show_page(p)
        self.resume = self.start_boot
        self.run(lambda job: self.with_reporter(job, self.session.boot), ok)

    # restore
    def page_restore(self):
        p = Page('Put rabbitOS back', 'Writes a restore file made by this installer back to the r1 it '
                 'came from. Everything the installer changed returns to how it was; haretic and its '
                 'data are erased. The whole file is checked before anything is written.')
        card, lay = frame('card')
        lay.addWidget(label('<b>Restore file</b>'))
        p.chosen = label('')
        lay.addWidget(p.chosen)
        p.path = label('', 'small')
        p.path.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(p.path)
        row = QHBoxLayout()
        p.pick = button('Choose a restore file...')
        row.addWidget(p.pick)
        row.addStretch(1)
        lay.addLayout(row)
        p.body.addWidget(card)
        p.progress = Progress()
        p.progress.hide()
        p.body.addWidget(p.progress)
        p.back = button('Back')
        p.go = button('Continue', 'primary')
        p.go.setEnabled(False)
        p.buttons.addWidget(p.back)
        p.buttons.addWidget(p.go)
        p.stopped = 'Restoring stopped'
        p.on_error = lambda: (p.back.setEnabled(True), p.pick.setEnabled(True))
        p.pick.clicked.connect(self.pick_backup)
        p.back.clicked.connect(self.show_welcome)
        p.go.clicked.connect(self.restore_continue)
        return p

    def restore_continue(self):
        """Connect the r1, or, when it still is from a question put off, ask again."""
        if self.device and self.session and self.session.info.identity['cid'] == self.cid:
            return self.show_restore_confirm()
        self.start_connect(after=self.show_check)

    def show_restore(self):
        self.stepper.set_steps(RESTORE_STEPS)
        p = self.p_restore
        found = flow.backups(paths.backups(), system='rabbitOS') or flow.backups(paths.backups())
        if found and not self.restore_file:
            self.set_restore(found[0][0], found[0][1])
        elif not self.restore_file:
            p.chosen.setText('No restore file found in the installer\'s backups folder.')
            p.path.setText('A restore file is named like r1-rabbitOS-restore-<i>date</i>.tar.xz. If you '
                           'copied it elsewhere, choose it.')
        self.show_page(p, 0)

    def pick_backup(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Choose the restore file', paths.backups(),
                                              'r1 restore files (*.tar.xz)')
        if not path:
            return
        m = flow.archive_manifest(path)
        if not m:
            QMessageBox.warning(self, paths.NAME, 'That is not an r1 restore file made by this installer.')
            return
        self.set_restore(path, m)

    def set_restore(self, path, m):
        p = self.p_restore
        self.restore_file = path
        self.cid = m['device']['cid']
        what = self.restore_system = 'haretic' if m['device']['system'] == 'haretic' else 'rabbitOS'
        p.title.setText(f'Put {what} back')
        p.chosen.setText(f'{what}, backed up {m["created"].replace("T", " ")[:16]} from the r1 with storage '
                         f'ID {self.cid[:8]}...')
        p.path.setText(path)
        p.pick.setText('Choose another restore file...')
        p.go.setEnabled(True)

    def show_restore_confirm(self):
        p = self.p_restore
        self.show_page(p, 2)
        what = self.restore_system
        if not self.ask(f'Put {what} back on this r1?', 'Everything on the r1 now (apps, files and settings) '
                        f'is erased, and the r1 returns to {what} as it was when the backup was made.',
                        'Erase and restore', danger=True):
            return self.log('restore not confirmed')
        self.start_restore()

    def start_restore(self):
        p = self.p_restore
        self.show_page(p, 3)
        p.error.hide()
        p.progress.show()
        p.progress.reset('Restoring')
        p.go.setEnabled(False)
        p.back.setEnabled(False)
        p.pick.setEnabled(False)
        self.step_safe = False
        self.resume = self.start_restore
        self.run(lambda job: self.with_reporter(job, self.session.restore, self.restore_file),
                 lambda _: self.start_boot(), p.progress)

    def closeEvent(self, event):
        busy = self.job is not None and self.job.isRunning()
        if busy:
            if not self.ask('Stop and close?', 'The installer is still working; closing stops it. If it is '
                            'writing to the r1, the r1 may not start until you open the installer again '
                            'and run the step again.', 'Stop and close', danger=True, cancel='Keep working'):
                event.ignore()
                return
            self.job.stop = True
            self.job.wait(15000)
        if self.device:
            self.device.close()
        event.accept()


def main(argv=None):
    argv = sys.argv if argv is None else argv
    from .device import frozen_stdio
    frozen_stdio(os.path.join(paths.logs(), 'console.log'))
    app = QApplication(argv)
    app.setApplicationName(paths.NAME)
    app.setDesktopFileName('io.github.haretic.installer')
    for icon in (paths.resource('io.github.haretic.installer.png'),
                 paths.resource('packaging', 'io.github.haretic.installer.png')):
        if os.path.exists(icon):
            app.setWindowIcon(QIcon(icon))
            break
    demo = None
    for a in argv[1:]:
        if a.startswith('--demo'):
            demo = float(a.split('=', 1)[1]) * 1e6 if '=' in a else 60e6
    w = Window(demo=demo)
    w.show()
    return app.exec()
