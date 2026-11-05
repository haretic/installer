"""haretic installer: the window, or with a command (check, backup, install, restore) the terminal."""
import sys

if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] in ('check', 'backup', 'install', 'restore', '-h', '--help', '-v'):
        from haretic_installer.cli import main
        sys.exit(main())
    from haretic_installer.gui import main
    sys.exit(main())
