if __name__ == '__main__':
    import os
    import sys

    # --config-dir DIR: isolate this instance's restim.ini / restim.log /
    # calibration.json (run a second instance alongside the first). Must be
    # translated to the env var BEFORE qt_ui imports read it.
    if '--config-dir' in sys.argv:
        i = sys.argv.index('--config-dir')
        try:
            os.environ['RESTIM_CONFIG_DIR'] = os.path.abspath(sys.argv[i + 1])
        except IndexError:
            sys.exit('--config-dir requires a directory argument')
        del sys.argv[i:i + 2]
    if os.environ.get('RESTIM_CONFIG_DIR'):
        os.makedirs(os.environ['RESTIM_CONFIG_DIR'], exist_ok=True)

    from qt_ui import mainwindow
    sys.exit(mainwindow.run())
