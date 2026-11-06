"""The installer without a window: the same steps, in a terminal.

  python -m haretic_installer.cli check
  python -m haretic_installer.cli backup  [--backups DIR] [--userdata]
  python -m haretic_installer.cli install PACKAGE [--backups DIR] [--update] [--backup] [--yes]
  python -m haretic_installer.cli restore RESTORE_FILE [--yes]

check and backup only read the r1. A backup is one checked .tar.xz, the restore file. install
backs up an r1 that runs rabbitOS first (or uses its restore file from before), unlocks it,
installs and starts it; an r1 already running haretic is only backed up with --backup. Every
write is read back."""
import argparse
import os
import sys
import time

from . import flow, package, paths, r1
from .device import Cancelled, DeviceError, MtkDevice


class Terminal(flow.Reporter):
    def __init__(self, verbose):
        self.verbose = verbose
        self.last = 0
        self.rate = flow.Rate()
        self.text = ''

    def status(self, text):
        if text != self.text:
            self.text = text
            print(f'\n{text}', flush=True)

    def progress(self, done, total):
        self.rate.add(done)
        now = time.monotonic()
        if now - self.last < 2 and done < total:
            return
        self.last = now
        left = self.rate.left(done, total)
        tail = f' - about {left / 60:.0f} min left' if left else ''
        print(f'  {flow.fmt_size(done)} of {flow.fmt_size(total)} ({100 * done / max(total, 1):.0f}%){tail}',
              flush=True)

    def log(self, line):
        if self.verbose:
            print('  | ' + line, flush=True)


def confirm(text, yes):
    if yes:
        return True
    return input(f'{text} Type "yes" to go on: ').strip().lower() == 'yes'


def keep_safe(path):
    print(f'\nrabbitOS restore file: {path}\nKeep a copy somewhere safe, off this computer too: it is '
          'the only way to put rabbitOS back on this r1.', flush=True)


def main(argv=None):
    ap = argparse.ArgumentParser(prog='haretic-installer', description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('-v', '--verbose', action='store_true', help="show mtkclient's log")
    sub = ap.add_subparsers(dest='cmd', required=True)
    c = sub.add_parser('check')
    c.add_argument('--no-boot', action='store_true', help='leave the r1 in download mode')
    b = sub.add_parser('backup')
    b.add_argument('--no-boot', action='store_true', help='leave the r1 in download mode')
    b.add_argument('--backups', default=paths.backups())
    b.add_argument('--userdata', action='store_true')
    i = sub.add_parser('install')
    i.add_argument('package')
    i.add_argument('--backups', default=paths.backups())
    i.add_argument('--update', action='store_true', help='keep data (the r1 must run haretic)')
    i.add_argument('--backup', action='store_true', help='back up an r1 that already runs haretic')
    i.add_argument('--yes', action='store_true')
    i.add_argument('--allow-downgrade', action='store_true', help=argparse.SUPPRESS)
    i.add_argument('--no-boot', action='store_true', help='leave the r1 in download mode')
    r = sub.add_parser('restore')
    r.add_argument('file')
    r.add_argument('--yes', action='store_true')
    args = ap.parse_args(argv)

    rep = Terminal(args.verbose)
    pkg = None
    if args.cmd == 'install':
        pkg = package.Package(args.package, paths.downloads())
        rep.status(f'Unpacking and checking {os.path.basename(args.package)}')
        pkg.verify(rep.progress)
    dev = MtkDevice(paths.work(), log=rep.log)
    s = flow.Session(dev, rep)
    try:
        print('Turn the r1 off (hold the side button about 10 seconds), then plug it in.', flush=True)
        info = s.inspect(s.connect(lambda st: rep.status({'waiting': 'Waiting for the r1...',
                                                          'found': 'Found the r1, connecting...',
                                                          'agent': 'Starting the download agent...'}.get(st, st))))
        print(f'\nrabbit r1: {info.identity["user_size"] / 1e9:.0f} GB, runs {info.system} on slot '
              f'{info.slot.upper()}, bootloader {info.lock}, loaders A {info.lk["a"]} / B {info.lk["b"]}'
              + (f', unlockable ({info.seccfg_type})' if info.seccfg_type else ''))
        if info.installed:
            print('installed: ' + ', '.join(f'{k} {v}' for k, v in info.installed.items() if v is not None))
        if args.cmd == 'backup':
            keep_safe(s.backup(args.backups, args.userdata))
        elif args.cmd == 'restore':
            if not confirm('This writes the backup back over everything haretic changed.', args.yes):
                return 1
            s.restore(args.file)
        elif args.cmd == 'install':
            mode = 'update' if args.update else 'clean'
            if mode == 'update' and info.system != 'haretic':
                raise flow.StepError('This r1 does not run haretic, so it needs a clean install. Nothing '
                                     'was changed.', hint='Run install again without --update.')
            if mode == 'update':
                problems = flow.update_problems(info.installed, pkg)
                for kind, text in problems:
                    if kind == 'refuse' or kind == 'downgrade' and not args.allow_downgrade:
                        raise flow.StepError(text + ' Nothing was changed.')
                warnings = [t for k, t in problems if k != 'refuse']
                if warnings and not confirm(' '.join(warnings), args.yes):
                    return 1
            stock = flow.backups(args.backups, info.identity['cid'], 'rabbitOS')
            if info.system != 'haretic':
                keep_safe(stock[0][0] if stock else s.backup(args.backups))
            elif args.backup:
                print('Backup of haretic saved in', s.backup(args.backups))
            if info.system == 'haretic':
                print(f'rabbitOS restore file: {stock[0][0]}' if stock else
                      f'No rabbitOS restore file of this r1 in {args.backups}. Without it, this r1 '
                      'cannot go back to rabbitOS.')
            if mode == 'clean' and not confirm('Everything on the r1 is erased.', args.yes):
                return 1
            s.unlock()
            s.install(pkg, mode, args.allow_downgrade)
        if getattr(args, 'no_boot', False):
            print('\nDone; the r1 stays in download mode.')
            return 0
        s.boot()
        print('\nDone. The r1 is starting' + ('; the first start takes a few minutes.'
                                              if args.cmd == 'install' else '.'))
        return 0
    except (flow.StepError, DeviceError, r1.NotAnR1, package.PackageError) as e:
        print(f'\nStopped: {e}', file=sys.stderr)
        if getattr(e, 'hint', None):
            print(e.hint, file=sys.stderr)
        if isinstance(e, flow.StepError) and not e.safe:
            print('The r1 will not start until the step is finished; run it again.', file=sys.stderr)
        if isinstance(e, DeviceError):
            print('Unplug the r1, hold its side button about 10 seconds until the screen stays dark, plug '
                  'it in and run the same command again. A backup goes on where it stopped; an install '
                  'or restore cut short leaves the r1 unable to start until it is run again.', file=sys.stderr)
        return 2
    except (Cancelled, KeyboardInterrupt):
        print('\nStopped.', file=sys.stderr)
        return 130
    finally:
        dev.close()


if __name__ == '__main__':
    sys.exit(main())
